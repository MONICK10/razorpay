"""Orchestrates L0-L5 per payment, in chronological order, then re-queues
unresolved AMBIG-N/NO-MATCH exceptions until a fixed point. Nothing is
written to the allocations ledger until a payment's outcome -- resolved or
permanently parked -- is final, so the append-only table never needs
superseding rows for the same decision.

Every call to resolve_payment() also builds a decision trace -- the same
checks the branches below already make, just narrated into a list instead
of being thrown away. Batch mode (run(), used by run_engine.py/scoring)
ignores Resolution.trace entirely; the simulator (simulator/state.py) is
the only consumer. This is instrumentation, not a second matching
implementation: every trace entry is recorded at a decision point that
already existed.
"""
from __future__ import annotations

import difflib
import sqlite3
from dataclasses import dataclass, field

from engine import l0_identity, l1_composite, l2_tolerance, l3_ai, l4_policy
from engine.confidence import ConfidenceSignals
from engine.reason_codes import EntryType, ReasonCode

MAX_REQUEUE_PASSES = 10


def payer_name_matches(customer_name: str, payer_name: str | None) -> bool:
    if not payer_name:
        return False
    a = customer_name.strip().upper()
    b = payer_name.strip().upper()
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


@dataclass
class Context:
    customers_by_va: dict
    invoices: dict  # invoice_id -> invoice dict (static fields)
    balance: dict  # invoice_id -> current remaining paise (mutated)
    invoice_ids_by_customer: dict  # customer_id -> list[invoice_id]

    def open_invoices(self, customer_id: str) -> list[dict]:
        out = []
        for inv_id in self.invoice_ids_by_customer.get(customer_id, []):
            bal = self.balance[inv_id]
            if bal > 0:
                inv = self.invoices[inv_id]
                out.append({
                    "invoice_id": inv_id,
                    "amount_paise": inv["amount_paise"],
                    "balance": bal,
                    "issue_date": inv["issue_date"],
                    "due_date": inv["due_date"],
                    "disputed": bool(inv["disputed"]),
                })
        return out

    def apply(self, invoice_id: str, amount_paise: int) -> None:
        self.balance[invoice_id] -= amount_paise


@dataclass
class Resolution:
    resolved: bool
    allocations: list[dict] = field(default_factory=list)
    pending_reason: str | None = None
    pending_rationale: str = ""
    trace: list[dict] = field(default_factory=list)


def _step(trace: list[dict], layer: str, input_: dict, checks: list[dict],
          note: str) -> None:
    """Append one decision-trace entry. outcome is derived from checks:
    PASS if every check passed, FAIL if any check ran and failed, SKIP if
    there were no checks to run (e.g. no candidates to consider)."""
    if not checks:
        outcome = "SKIP"
    elif all(c["passed"] for c in checks):
        outcome = "PASS"
    else:
        outcome = "FAIL"
    trace.append({"layer": layer, "input": input_, "checks": checks,
                   "outcome": outcome, "note": note})


def _row(payment_id, invoice_id, amount_paise, method, reason_code,
         confidence, rationale, entry_type: EntryType = EntryType.CASH,
         resolved_on_requeue: bool = False) -> dict:
    return {
        "payment_id": payment_id,
        "invoice_id": invoice_id,
        "amount_paise": amount_paise,
        "method": method,
        "reason_code": reason_code,
        "confidence": confidence,
        "rationale": rationale,
        "entry_type": entry_type,
        "resolved_on_requeue": resolved_on_requeue,
    }


def _resolve_gap(payment: dict, inv: dict, ctx: Context,
                  reference_exact: bool, reference_repaired: bool,
                  payer_match: bool, trace: list[dict]) -> list[dict]:
    """Shared shortfall/overpayment logic for a payment matched (exactly or
    via repaired reference) to a single invoice."""
    pid, invoice_id = payment["payment_id"], inv["invoice_id"]
    method = payment["method"]
    bal = inv["balance"]
    amt = payment["amount_paise"]
    gap = bal - amt

    if gap == 0:
        conf = ConfidenceSignals(reference_exact=reference_exact,
                                  reference_repaired=reference_repaired,
                                  amount_exact=True, candidate_count=1,
                                  payer_name_match=payer_match).score()
        code = ReasonCode.EXACT if reference_exact else ReasonCode.REF_FUZZY
        _step(trace, "L2_TOLERANCE", {"invoice_id": invoice_id, "gap_paise": 0},
              [{"name": "amount matches invoice balance exactly",
                "passed": True, "detail": f"balance={bal}p, payment={amt}p"}],
              "no shortfall or overpayment -- clean settlement")
        ctx.apply(invoice_id, amt)
        return [_row(pid, invoice_id, amt, method, code, conf,
                      "reference and amount agree" if reference_exact
                      else "reference repaired, amount agrees")]

    if gap > 0:  # shortfall
        if l2_tolerance.within_tolerance(gap, bal):
            conf = ConfidenceSignals(reference_exact=reference_exact,
                                      reference_repaired=reference_repaired,
                                      amount_in_tolerance=True, candidate_count=1,
                                      payer_name_match=payer_match).score()
            _step(trace, "L2_TOLERANCE", {"invoice_id": invoice_id, "gap_paise": gap},
                  [{"name": "shortfall within tolerance band", "passed": True,
                    "detail": f"gap={gap}p <= tolerance"}],
                  "auto-clear, difference written off to bank charges")
            ctx.apply(invoice_id, bal)
            return [
                _row(pid, invoice_id, amt, method, ReasonCode.TOL_FEE, conf,
                     f"shortfall {gap}p within tolerance"),
                _row(pid, invoice_id, gap, "bank_charge", ReasonCode.TOL_FEE, conf,
                     "tolerance write-off to bank charges",
                     entry_type=EntryType.ADJUSTMENT),
            ]
        _step(trace, "L2_TOLERANCE", {"invoice_id": invoice_id, "gap_paise": gap},
              [{"name": "shortfall within tolerance band", "passed": False,
                "detail": f"gap={gap}p exceeds tolerance"}],
              "shortfall too large to auto-clear as a fee")
        if l2_tolerance.looks_like_deduction(payment.get("narration", "")):
            conf = ConfidenceSignals(reference_exact=reference_exact,
                                      reference_repaired=reference_repaired,
                                      candidate_count=1,
                                      payer_name_match=payer_match).score()
            _step(trace, "L2_TOLERANCE", {"invoice_id": invoice_id},
                  [{"name": "narration suggests a deduction (TDS/withholding)",
                    "passed": True, "detail": payment.get("narration", "")}],
                  "shortfall recorded as residual deduction, invoice cleared")
            ctx.apply(invoice_id, bal)
            return [
                _row(pid, invoice_id, amt, method, ReasonCode.RESID_DED, conf,
                     f"shortfall {gap}p treated as deduction"),
                _row(pid, invoice_id, gap, "deduction", ReasonCode.RESID_DED, conf,
                     "deduction recorded as residual, invoice cleared",
                     entry_type=EntryType.ADJUSTMENT),
            ]
        conf = ConfidenceSignals(reference_exact=reference_exact,
                                  reference_repaired=reference_repaired,
                                  candidate_count=1,
                                  payer_name_match=payer_match).score()
        _step(trace, "L2_TOLERANCE", {"invoice_id": invoice_id},
              [{"name": "narration suggests a deduction (TDS/withholding)",
                "passed": False, "detail": payment.get("narration", "")}],
              "no deduction keyword -- treated as partial payment, invoice stays open")
        ctx.apply(invoice_id, amt)
        return [_row(pid, invoice_id, amt, method, ReasonCode.PART_EXP, conf,
                      "partial payment, more expected, invoice stays open")]

    # gap < 0: overpayment. Close the invoice, park the excess on-account.
    excess = -gap
    conf = ConfidenceSignals(reference_exact=reference_exact,
                              reference_repaired=reference_repaired,
                              amount_exact=False, candidate_count=1,
                              payer_name_match=payer_match).score()
    _step(trace, "L2_TOLERANCE", {"invoice_id": invoice_id, "excess_paise": excess},
          [{"name": "payment exceeds invoice balance", "passed": True,
            "detail": f"excess={excess}p"}],
          "invoice cleared, excess parked on account")
    ctx.apply(invoice_id, bal)
    code = ReasonCode.EXACT if reference_exact else ReasonCode.REF_FUZZY
    # Same code on both rows, matching the TOL_FEE/RESID_DED convention
    # above: a payment exits with exactly one reason code even when its
    # cash splits across multiple allocation rows. The excess is still
    # real money that arrived (entry_type=CASH, the _row default) --
    # parked on account rather than tied to an invoice, not a write-off.
    return [
        _row(pid, invoice_id, bal, method, code, conf, "invoice cleared"),
        _row(pid, None, excess, method, code, conf,
             "overpayment beyond invoice amount, parked on account"),
    ]


def resolve_payment(payment: dict, ctx: Context) -> Resolution:
    trace: list[dict] = []
    customer = l0_identity.resolve_customer(payment, ctx.customers_by_va)
    va = payment.get("virtual_account")
    _step(trace, "L0_IDENTITY", {"virtual_account": va},
          [{"name": "virtual account maps to a known customer",
            "passed": customer is not None,
            "detail": (f"{va} -> {customer['name']}" if customer
                       else f"{va} does not match any customer record")}],
          "identity resolved" if customer else "payer cannot be identified")
    if customer is None:
        conf = ConfidenceSignals(candidate_count=0).score()
        return Resolution(True, [_row(
            payment["payment_id"], None, payment["amount_paise"],
            payment["method"], ReasonCode.SUSPENSE, conf,
            "virtual account does not map to any known customer")], trace=trace)

    narration = payment.get("narration", "")
    amt = payment["amount_paise"]
    payer_match = payer_name_matches(customer["name"], payment.get("payer_name"))

    # A non-positive amount (e.g. a refund) is outside what this engine is
    # designed to allocate. Left unguarded, it would flow into
    # _resolve_gap and *increase* an invoice's balance via ctx.apply
    # (subtracting a negative) -- silent ledger corruption dressed up as
    # a partial payment. Refuse to guess instead, same as any other
    # payment nothing in the vocabulary explains.
    if amt <= 0:
        _step(trace, "L1_COMPOSITE", {"amount_paise": amt},
              [{"name": "payment amount is positive", "passed": False,
                "detail": "non-positive amount (e.g. a refund) is outside "
                          "supported scope"}],
              "refusing to guess on a non-positive amount")
        return Resolution(False, pending_reason=ReasonCode.NO_MATCH,
                           pending_rationale="non-positive payment amount is "
                                             "outside supported scope; refusing "
                                             "to auto-match",
                           trace=trace)

    # -- duplicate check: exact reference to an invoice already fully paid --
    all_ids = ctx.invoice_ids_by_customer.get(customer["customer_id"], [])
    found = l1_composite.reference.find_reference_candidates(narration, all_ids)
    if len(found) == 1:
        inv_id = found[0]
        inv = ctx.invoices[inv_id]
        already_settled = ctx.balance[inv_id] == 0 and amt == inv["amount_paise"]
        _step(trace, "L1_COMPOSITE", {"referenced_invoice": inv_id},
              [{"name": "reference points to an already fully-settled invoice",
                "passed": already_settled,
                "detail": f"balance={ctx.balance[inv_id]}p, payment={amt}p, "
                          f"invoice amount={inv['amount_paise']}p"}],
              "duplicate of a settled payment" if already_settled
              else "invoice still open -- not a duplicate")
        if already_settled:
            conf = ConfidenceSignals(reference_exact=True, amount_exact=True,
                                      candidate_count=1,
                                      payer_name_match=payer_match).score()
            return Resolution(True, [_row(
                payment["payment_id"], None, amt, payment["method"],
                ReasonCode.DUP_ONACC, conf,
                f"duplicate: {inv_id} already fully allocated")], trace=trace)

    open_invoices = ctx.open_invoices(customer["customer_id"])

    # -- L1: exact single reference --
    match = l1_composite.match_single_reference(narration, open_invoices)
    _step(trace, "L1_COMPOSITE", {"narration": narration},
          [{"name": "exactly one open invoice referenced verbatim",
            "passed": match is not None,
            "detail": (f"matched {match['invoice_id']}" if match
                       else "no single unambiguous reference found")}],
          "single exact reference match" if match else "no exact single-reference match")
    if match:
        rows = _resolve_gap(payment, match, ctx, reference_exact=True,
                             reference_repaired=False, payer_match=payer_match,
                             trace=trace)
        return Resolution(True, rows, trace=trace)

    # -- L1: explicit multi-reference bulk --
    bulk = l1_composite.match_explicit_bulk(narration, open_invoices, amt)
    _step(trace, "L1_COMPOSITE", {"narration": narration},
          [{"name": "2+ open invoices referenced verbatim, balances sum to payment",
            "passed": bulk is not None,
            "detail": (f"{[i['invoice_id'] for i in bulk]} sum to {amt}p" if bulk
                       else "no exact multi-reference sum match")}],
          "explicit bulk settlement" if bulk else "no explicit multi-reference match")
    if bulk:
        conf = ConfidenceSignals(reference_exact=True, amount_exact=True,
                                  candidate_count=len(bulk),
                                  payer_name_match=payer_match).score()
        rows = []
        for inv in bulk:
            ctx.apply(inv["invoice_id"], inv["balance"])
            rows.append(_row(payment["payment_id"], inv["invoice_id"],
                              inv["balance"], payment["method"],
                              ReasonCode.BULK_N, conf,
                              "explicit multi-invoice reference"))
        return Resolution(True, rows, trace=trace)

    # -- L3: fuzzy-repair any unresolved reference-shaped token --
    tokens = l1_composite.extract_unresolved_tokens(narration, open_invoices)
    if not tokens:
        _step(trace, "L3_AI", {"narration": narration}, [],
              "no reference-shaped token to repair")
    for token in tokens:
        repaired = l3_ai.repair_reference(token, open_invoices)
        _step(trace, "L3_AI", {"token": token},
              [{"name": "fuzzy repair resolves to a unique candidate",
                "passed": repaired.invoice_id is not None,
                "detail": repaired.rationale}],
              f"repaired to {repaired.invoice_id}" if repaired.invoice_id
              else "declined to guess")
        if repaired.invoice_id:
            inv = next(i for i in open_invoices if i["invoice_id"] == repaired.invoice_id)
            rows = _resolve_gap(payment, inv, ctx, reference_exact=False,
                                 reference_repaired=True, payer_match=payer_match,
                                 trace=trace)
            return Resolution(True, rows, trace=trace)

    # -- amount-only single/tied candidates --
    amount_matches = l4_policy.find_amount_matches(open_invoices, amt)
    _step(trace, "L4_POLICY", {"amount_paise": amt, "stage": "amount_match"},
          [{"name": "open invoices whose balance equals the payment exactly",
            "passed": len(amount_matches) > 0,
            "detail": f"{len(amount_matches)} candidate(s): "
                      f"{[i['invoice_id'] for i in amount_matches]}"}],
          "amount-only candidate search")
    if len(amount_matches) == 1:
        inv = amount_matches[0]
        conf = ConfidenceSignals(amount_exact=True, candidate_count=1,
                                  payer_name_match=payer_match).score()
        ctx.apply(inv["invoice_id"], amt)
        return Resolution(True, [_row(
            payment["payment_id"], inv["invoice_id"], amt, payment["method"],
            ReasonCode.EXACT, conf, "amount-only unique match, no reference needed")],
            trace=trace)

    if len(amount_matches) >= 2:
        neutral = l4_policy.all_neutral(amount_matches)
        _step(trace, "L4_POLICY", {"stage": "tie_break"},
              [{"name": "all tied candidates undisputed and financially identical",
                "passed": neutral,
                "detail": f"{len(amount_matches)} candidates"}],
              "safe to FIFO" if neutral else "a disputed candidate is tied -- needs judgment")
        if neutral:
            chosen = l4_policy.fifo_pick(amount_matches)
            conf = ConfidenceSignals(amount_exact=True,
                                      candidate_count=len(amount_matches),
                                      payer_name_match=payer_match).score()
            ctx.apply(chosen["invoice_id"], amt)
            return Resolution(True, [_row(
                payment["payment_id"], chosen["invoice_id"], amt, payment["method"],
                ReasonCode.FIFO_TIE, conf,
                f"{len(amount_matches)} financially-identical candidates, "
                "FIFO policy -> oldest")], trace=trace)
        choice = l3_ai.choose_candidate(payment, amount_matches)
        _step(trace, "L3_AI", {"stage": "shortlist_choice"},
              [{"name": "AI shortlist choice, unless a dispute is tied",
                "passed": choice.invoice_id is not None,
                "detail": choice.rationale}],
              f"chose {choice.invoice_id}" if choice.invoice_id else "declined to guess")
        if choice.invoice_id:
            conf = ConfidenceSignals(amount_exact=True,
                                      candidate_count=len(amount_matches),
                                      payer_name_match=payer_match).score()
            ctx.apply(choice.invoice_id, amt)
            return Resolution(True, [_row(
                payment["payment_id"], choice.invoice_id, amt, payment["method"],
                ReasonCode.FIFO_TIE, conf,
                f"AI-assisted tie-break despite disputed candidate: {choice.rationale}")],
                trace=trace)
        _step(trace, "L5_EXCEPTION", {}, [], "genuinely ambiguous -- parked for review")
        return Resolution(False, pending_reason=ReasonCode.AMBIG_N,
                           pending_rationale=f"{len(amount_matches)} candidates, "
                                             f"a disputed invoice is tied: {choice.rationale}",
                           trace=trace)

    # -- amount-only subset-sum across multiple invoices --
    # A unique covering combination is BULK-N regardless of whether it
    # happens to be every open invoice the customer has, and regardless of
    # whether those invoices share an amount: every invoice in the
    # combination reaches zero balance either way, so there is no
    # attribution left to assume. (This was previously special-cased as
    # PROVISIONAL when it covered the full balance across 3+ invoices --
    # that was wrong. PROVISIONAL is a customer-level state for when
    # *no* combination can be proven at all; see engine/customer_status.py.)
    combos = l4_policy.find_subset_sum_matches(open_invoices, amt)
    _step(trace, "L4_POLICY", {"amount_paise": amt, "stage": "subset_sum"},
          [{"name": "combination of open invoices sums exactly to the payment",
            "passed": len(combos) > 0,
            "detail": f"{len(combos)} combination(s) found"}],
          "subset-sum search")
    if len(combos) == 1:
        combo = combos[0]
        conf = ConfidenceSignals(amount_exact=True, candidate_count=1,
                                  payer_name_match=payer_match).score()
        rows = []
        for inv in combo:
            ctx.apply(inv["invoice_id"], inv["balance"])
            rows.append(_row(payment["payment_id"], inv["invoice_id"],
                              inv["balance"], payment["method"],
                              ReasonCode.BULK_N, conf,
                              "amount-only subset-sum, unique combination"))
        return Resolution(True, rows, trace=trace)

    if len(combos) >= 2:
        _step(trace, "L5_EXCEPTION", {}, [], "non-unique combinations -- parked for review")
        return Resolution(False, pending_reason=ReasonCode.AMBIG_N,
                           pending_rationale=f"{len(combos)} non-unique invoice "
                                             "combinations sum to this amount",
                           trace=trace)

    _step(trace, "L5_EXCEPTION", {}, [], "nothing fits -- parked for review")
    return Resolution(False, pending_reason=ReasonCode.NO_MATCH,
                       pending_rationale="no reference, amount, or combination "
                                         "of open invoices matches this payment",
                       trace=trace)


def run(payments: list[dict], ctx: Context) -> list[dict]:
    """Process payments in chronological order, then re-queue unresolved
    exceptions until a fixed point. Returns the final list of allocation
    rows to persist (one final disposition per payment)."""
    ordered = sorted(payments, key=lambda p: (p["created_at"], p["payment_id"]))
    final_rows: list[dict] = []
    pending: dict[str, tuple[dict, Resolution]] = {}  # payment_id -> (payment, last Resolution)

    for payment in ordered:
        res = resolve_payment(payment, ctx)
        if res.resolved:
            final_rows.extend(res.allocations)
        else:
            pending[payment["payment_id"]] = (payment, res)

    for _ in range(MAX_REQUEUE_PASSES):
        if not pending:
            break
        resolved_ids = []
        for pid, (payment, _prev) in pending.items():
            res = resolve_payment(payment, ctx)
            if res.resolved:
                for row in res.allocations:
                    row["resolved_on_requeue"] = True
                final_rows.extend(res.allocations)
                resolved_ids.append(pid)
            else:
                pending[pid] = (payment, res)
        if not resolved_ids:
            break
        for pid in resolved_ids:
            del pending[pid]

    for payment, res in pending.values():
        conf = ConfidenceSignals(candidate_count=99 if res.pending_reason ==
                                  ReasonCode.AMBIG_N else 0).score()
        final_rows.append(_row(
            payment["payment_id"], None, payment["amount_paise"],
            payment["method"], res.pending_reason, conf, res.pending_rationale))

    return final_rows


def load_context(conn: sqlite3.Connection) -> tuple[Context, list[dict]]:
    customers = [dict(r) for r in conn.execute("SELECT * FROM customers")]
    invoices = [dict(r) for r in conn.execute("SELECT * FROM invoices")]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments")]

    customers_by_va = {c["virtual_account"]: c for c in customers}
    invoices_by_id = {i["invoice_id"]: i for i in invoices}
    balance = {i["invoice_id"]: i["amount_paise"] for i in invoices}
    ids_by_customer: dict[str, list[str]] = {}
    for inv in invoices:
        ids_by_customer.setdefault(inv["customer_id"], []).append(inv["invoice_id"])

    ctx = Context(customers_by_va=customers_by_va, invoices=invoices_by_id,
                   balance=balance, invoice_ids_by_customer=ids_by_customer)
    return ctx, payments


def persist_allocations(conn: sqlite3.Connection, rows: list[dict]) -> None:
    def code_value(code):
        return code.value if isinstance(code, ReasonCode) else code

    def entry_type_value(entry_type):
        return entry_type.value if isinstance(entry_type, EntryType) else entry_type

    conn.executemany(
        "INSERT INTO allocations (payment_id, invoice_id, amount_paise, "
        "method, reason_code, entry_type, confidence, rationale, "
        "resolved_on_requeue) VALUES "
        "(:payment_id, :invoice_id, :amount_paise, :method, :reason_code, "
        ":entry_type, :confidence, :rationale, :resolved_on_requeue)",
        [{**r, "reason_code": code_value(r["reason_code"]),
          "entry_type": entry_type_value(r["entry_type"]),
          "resolved_on_requeue": int(r["resolved_on_requeue"])} for r in rows])
    conn.commit()
