"""Phase 4 -- scoring harness. Computes the measured match rate against
ground truth, the reported metrics from PRD section 8, and the confidence
threshold trade-off curve. Never cut this phase: it is the entire argument
of the submission.

Run: python -m scoring.score
Produces: out/results.json
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from engine.confidence import DEFAULT_AUTO_POST_THRESHOLD
from engine.customer_status import compute_from_db
from engine.db import connect
from engine.reason_codes import EXCEPTIONS
from engine.l5_exceptions import build_queue, bucket_payments, human_touch_groups

OUT_DIR = Path(__file__).parent.parent / "out"

EXCEPTION_CODES = {c.value for c in EXCEPTIONS}
THRESHOLD_STEPS = list(range(0, 101, 5))


def load_ground_truth(gt_path: Path) -> dict[str, dict]:
    with open(gt_path, newline="") as f:
        rows = list(csv.DictReader(f))
    gt = {}
    for row in rows:
        gt[row["payment_id"]] = {
            "expected_reason_code": row["expected_reason_code"],
            "expected_invoice_ids": set(
                filter(None, row["expected_invoice_ids"].split(";"))),
            "notes": row["notes"],
        }
    return gt


def load_ground_truth_customers(gt_customers_path: Path) -> dict[str, dict]:
    with open(gt_customers_path, newline="") as f:
        rows = list(csv.DictReader(f))
    return {
        row["customer_id"]: {
            "expected_customer_status": row["expected_customer_status"],
            "expected_balance_paise": int(row["expected_balance_paise"]),
        }
        for row in rows
    }


def score_customer_status(gt_customers: dict, conn) -> dict:
    """The two-tier report the PRD asks for, checked against ground truth:
    is the customer-level balance provably correct, separately from
    whether every individual payment landed on the right invoice? A
    PROVISIONAL customer should score 100% here even though its own
    payments are, correctly, individual NO-MATCH exceptions."""
    actual = compute_from_db(conn)
    assert set(gt_customers) == set(actual), (
        f"customer set mismatch: missing={set(gt_customers)-set(actual)} "
        f"extra={set(actual)-set(gt_customers)}")

    status_correct = balance_correct = 0
    by_status: dict[str, dict] = defaultdict(lambda: {"count": 0, "balance_correct": 0})
    mismatches = []
    for cid, g in gt_customers.items():
        a = actual[cid]
        s_ok = a.status == g["expected_customer_status"]
        b_ok = a.balance_paise == g["expected_balance_paise"]
        by_status[g["expected_customer_status"]]["count"] += 1
        if s_ok:
            status_correct += 1
        if b_ok:
            balance_correct += 1
            by_status[g["expected_customer_status"]]["balance_correct"] += 1
        if not (s_ok and b_ok):
            mismatches.append({
                "customer_id": cid,
                "expected_status": g["expected_customer_status"],
                "actual_status": a.status,
                "expected_balance_paise": g["expected_balance_paise"],
                "actual_balance_paise": a.balance_paise,
            })

    total = len(gt_customers)
    return {
        "totals": {"customers": total},
        "status_accuracy": round(status_correct / total, 4),
        "balance_accuracy": round(balance_correct / total, 4),
        "balance_accuracy_by_status": {
            status: round(d["balance_correct"] / d["count"], 4)
            for status, d in sorted(by_status.items())
        },
        "mismatches": mismatches,
    }


def load_actuals(conn) -> dict[str, dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT payment_id, invoice_id, amount_paise, reason_code, "
        "confidence, rationale, method, entry_type, resolved_on_requeue "
        "FROM allocations ORDER BY payment_id, allocation_id")]
    payments = [dict(r) for r in conn.execute(
        "SELECT payment_id, amount_paise, virtual_account, payer_name "
        "FROM payments")]
    payment_amt = {p["payment_id"]: p["amount_paise"] for p in payments}
    payment_meta = {p["payment_id"]: p for p in payments}

    by_payment: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_payment[r["payment_id"]].append(r)

    actuals = {}
    for pid, rs in by_payment.items():
        codes = {r["reason_code"] for r in rs}
        assert len(codes) == 1, f"{pid} has mixed reason codes: {codes}"
        code = codes.pop()
        invoice_ids = {r["invoice_id"] for r in rs if r["invoice_id"]}
        # confidence is uniform across a payment's rows by construction;
        # take the min defensively rather than assume it.
        confidence = min(r["confidence"] for r in rs)
        cash_total = sum(r["amount_paise"] for r in rs
                          if r["entry_type"] == "CASH")
        # resolved_on_requeue is uniform across a payment's rows by
        # construction (see pipeline.run()) -- any() is defensive, same
        # posture as the confidence/cash_total aggregation above.
        resolved_on_requeue = any(r["resolved_on_requeue"] for r in rs)
        actuals[pid] = {
            "reason_code": code,
            "invoice_ids": invoice_ids,
            "confidence": confidence,
            "amount_paise": payment_amt[pid],
            "cash_total_paise": cash_total,
            "resolved_on_requeue": resolved_on_requeue,
            "rationale": "; ".join(r["rationale"] for r in rs),
            "virtual_account": payment_meta[pid]["virtual_account"],
            "payer_name": payment_meta[pid]["payer_name"],
        }
    return actuals


def score(gt: dict, actual: dict) -> dict:
    total = len(gt)
    assert set(gt) == set(actual), (
        f"payment set mismatch: missing={set(gt)-set(actual)} "
        f"extra={set(actual)-set(gt)}")

    correct = 0
    reason_breakdown: dict[str, dict] = defaultdict(lambda: {"expected": 0, "correct": 0})
    mismatches = []

    # Resolution-level confusion counts -- threshold-independent, distinct from
    # precision_at_default_threshold below (which is gated by the confidence
    # dial). Positive = the engine resolved the payment (a non-exception
    # reason code) rather than sending it to the exception queue; ground-truth
    # positive = the spec expected a resolution at all. TP requires not just
    # "resolved" but "resolved correctly" (same code_ok/invoices_ok check as
    # the match-rate loop below) -- a wrong invoice on an otherwise-resolved
    # payment is a miss, not a hit.
    #
    # gt_positive is checked BEFORE pred_positive below: a payment ground
    # truth says should have resolved, but that the engine either refused
    # OR resolved with the wrong code/invoice, is a false_negative either
    # way -- "correct allocations that existed but we missed (refused, or
    # given the wrong reason code)". false_positive is reserved for the
    # other kind of wrong: the engine resolved a payment ground truth says
    # should have stayed an exception -- an allocation invented where none
    # should exist.
    true_positives = false_positives = false_negatives = true_negatives = 0

    for pid, g in gt.items():
        a = actual[pid]
        reason_breakdown[g["expected_reason_code"]]["expected"] += 1
        code_ok = a["reason_code"] == g["expected_reason_code"]
        # invoice set only checked when ground truth names specific invoices
        invoices_ok = (not g["expected_invoice_ids"]
                        or a["invoice_ids"] == g["expected_invoice_ids"])
        is_correct = code_ok and invoices_ok
        if is_correct:
            correct += 1
            reason_breakdown[g["expected_reason_code"]]["correct"] += 1
        else:
            mismatches.append({
                "payment_id": pid,
                "expected_reason_code": g["expected_reason_code"],
                "actual_reason_code": a["reason_code"],
                "expected_invoice_ids": sorted(g["expected_invoice_ids"]),
                "actual_invoice_ids": sorted(a["invoice_ids"]),
                "notes": g["notes"],
            })

        pred_positive = a["reason_code"] not in EXCEPTION_CODES
        gt_positive = g["expected_reason_code"] not in EXCEPTION_CODES
        if pred_positive and gt_positive and is_correct:
            true_positives += 1
        elif gt_positive:
            false_negatives += 1
        elif pred_positive:
            false_positives += 1
        else:
            true_negatives += 1

    precision = (round(true_positives / (true_positives + false_positives), 4)
                 if (true_positives + false_positives) else None)
    recall = (round(true_positives / (true_positives + false_negatives), 4)
              if (true_positives + false_negatives) else None)
    f1 = (round(2 * precision * recall / (precision + recall), 4)
          if precision is not None and recall is not None and (precision + recall) > 0
          else None)

    for code, d in reason_breakdown.items():
        d["accuracy"] = round(d["correct"] / d["expected"], 4) if d["expected"] else None

    resolved = {pid: a for pid, a in actual.items() if a["reason_code"] not in EXCEPTION_CODES}
    exceptions = {pid: a for pid, a in actual.items() if a["reason_code"] in EXCEPTION_CODES}

    auto_resolution_rate = round(len(resolved) / total, 4)
    value_requiring_review = sum(a["amount_paise"] for a in exceptions.values())

    touch_rows = [{"payment_id": pid, "reason_code": a["reason_code"],
                   "virtual_account": a["virtual_account"]}
                  for pid, a in exceptions.items()]
    human_touches = len(human_touch_groups(touch_rows))

    curve = []
    for threshold in THRESHOLD_STEPS:
        auto_matched = wrong = 0
        for pid, a in resolved.items():
            if a["confidence"] >= threshold:
                auto_matched += 1
                g = gt[pid]
                code_ok = a["reason_code"] == g["expected_reason_code"]
                invoices_ok = (not g["expected_invoice_ids"]
                                or a["invoice_ids"] == g["expected_invoice_ids"])
                if not (code_ok and invoices_ok):
                    wrong += 1
        below_threshold = len(resolved) - auto_matched
        curve.append({
            "threshold": threshold,
            "auto_matched": auto_matched,
            "human_touches": human_touches + below_threshold,
            "wrong_matches": wrong,
            "precision": round((auto_matched - wrong) / auto_matched, 4) if auto_matched else None,
        })

    default_row = next(r for r in curve if r["threshold"] == DEFAULT_AUTO_POST_THRESHOLD)

    bucket_entries = [
        {"payment_id": pid, "resolved": a["reason_code"] not in EXCEPTION_CODES,
         "confidence": a["confidence"], "reason_code": a["reason_code"],
         "virtual_account": a["virtual_account"]}
        for pid, a in actual.items()
    ]
    buckets_at_default_threshold = bucket_payments(bucket_entries, DEFAULT_AUTO_POST_THRESHOLD)

    exception_rows = [
        {
            "payment_id": pid,
            "reason_code": a["reason_code"],
            "amount_paise": a["amount_paise"],
            "rationale": a["rationale"],
            "virtual_account": a["virtual_account"],
            "payer_name": a["payer_name"],
        }
        for pid, a in exceptions.items()
    ]

    resolved_on_requeue_count = sum(1 for a in actual.values() if a["resolved_on_requeue"])

    return {
        "totals": {"payments": total},
        "overall_match_rate": round(correct / total, 4),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "true_negatives": true_negatives,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "reason_code_breakdown": dict(sorted(reason_breakdown.items())),
        "mismatches": mismatches,
        "auto_resolution_rate": auto_resolution_rate,
        "human_touches": human_touches,
        "raw_exception_count": len(exceptions),
        "value_requiring_review_paise": value_requiring_review,
        "default_threshold": DEFAULT_AUTO_POST_THRESHOLD,
        "precision_at_default_threshold": default_row["precision"],
        "resolved_on_requeue_count": resolved_on_requeue_count,
        "threshold_curve": curve,
        "buckets_at_default_threshold": buckets_at_default_threshold,
        "exception_queue": build_queue(exception_rows),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="",
                         help="e.g. _holdout -- reads finance_holdout.db / "
                              "ground_truth_holdout.csv, writes results_holdout.json")
    args = parser.parse_args()
    suffix = args.suffix

    db_path = OUT_DIR / f"finance{suffix}.db"
    gt_path = OUT_DIR / f"ground_truth{suffix}.csv"
    gt_customers_path = OUT_DIR / f"ground_truth_customers{suffix}.csv"

    conn = connect(db_path)
    gt = load_ground_truth(gt_path)
    actual = load_actuals(conn)
    result = score(gt, actual)

    gt_customers = load_ground_truth_customers(gt_customers_path)
    result["customer_status"] = score_customer_status(gt_customers, conn)

    with open(OUT_DIR / f"results{suffix}.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"overall_match_rate: {result['overall_match_rate']*100:.1f}%")
    print(f"confusion: TP={result['true_positives']} FP={result['false_positives']} "
          f"FN={result['false_negatives']} TN={result['true_negatives']}")
    def _pctstr(x):
        return "n/a" if x is None else f"{x*100:.1f}%"
    print(f"precision: {_pctstr(result['precision'])}  "
          f"recall: {_pctstr(result['recall'])}  f1: {_pctstr(result['f1'])}")
    print(f"auto_resolution_rate: {result['auto_resolution_rate']*100:.1f}%")
    print(f"human_touches: {result['human_touches']} "
          f"(raw exceptions: {result['raw_exception_count']})")
    print(f"value_requiring_review: Rs {result['value_requiring_review_paise']/100:,.2f}")
    print(f"precision @ default threshold ({DEFAULT_AUTO_POST_THRESHOLD}): "
          f"{result['precision_at_default_threshold']}")
    print(f"resolved_on_requeue_count: {result['resolved_on_requeue_count']}")
    b = result["buckets_at_default_threshold"]
    print(f"buckets @ default threshold ({DEFAULT_AUTO_POST_THRESHOLD}): "
          f"payments={b['total']} auto_posted={b['auto_posted']} "
          f"needs_confirmation={b['needs_confirmation']} "
          f"exceptions={b['exceptions']} (exceptions_grouped={b['exceptions_grouped']})  "
          f"[{b['auto_posted']}+{b['needs_confirmation']}+{b['exceptions']}={b['total']}]")
    cs = result["customer_status"]
    print(f"customer status_accuracy: {cs['status_accuracy']*100:.1f}%  "
          f"balance_accuracy: {cs['balance_accuracy']*100:.1f}%")
    print("  balance_accuracy by status: " + ", ".join(
        f"{k}={v*100:.1f}%" for k, v in cs["balance_accuracy_by_status"].items()))
    if result["mismatches"]:
        print(f"\n{len(result['mismatches'])} payment mismatches vs ground truth:")
        for m in result["mismatches"]:
            print(f"  {m['payment_id']}: expected {m['expected_reason_code']} "
                  f"got {m['actual_reason_code']}  ({m['notes']})")
    if cs["mismatches"]:
        print(f"\n{len(cs['mismatches'])} customer_status mismatches vs ground truth:")
        for m in cs["mismatches"]:
            print(f"  {m['customer_id']}: expected {m['expected_status']}/"
                  f"{m['expected_balance_paise']}p got "
                  f"{m['actual_status']}/{m['actual_balance_paise']}p")

    conn.close()


if __name__ == "__main__":
    main()
