"""In-memory session state for the simulator. Every decision is made by
calling engine.pipeline.resolve_payment() directly -- the same function
run_engine.py uses in batch -- so the simulator can never drift from the
real matching logic. This module adds bookkeeping (pending exceptions,
the live ledger, the confidence dial), on-account credit consumption, and
the L6 action layer that turns a reason code into a proposed next step.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from engine import customer_status, l4_policy, l6_action
from engine.confidence import DEFAULT_AUTO_POST_THRESHOLD
from engine.l5_exceptions import bucket_payments
from engine.pipeline import MAX_REQUEUE_PASSES, Context, resolve_payment
from engine.reason_codes import EXCEPTIONS, ActionType, EntryType, ReasonCode
from simulator import seed

EXCEPTION_CODE_VALUES = {c.value for c in EXCEPTIONS}
DATA_DIR = Path(__file__).parent.parent / "data"

# "Without the agent" comparison strip -- see engine/l6_action.py docstring
# for why an action is faster to clear than raw investigation: a clerk is
# approving a drafted, reasoned decision, not researching one from scratch.
MINUTES_PER_MANUAL_ROW = 3
MINUTES_PER_ACTION_REVIEW = 1


def _code_value(code) -> str:
    return code.value if hasattr(code, "value") else code


def _fmt_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"


class SessionState:
    def __init__(self) -> None:
        self.reset()

    # -- world loading ------------------------------------------------

    def _load_world(self, customers: list[dict], invoices: list[dict]) -> None:
        customers_by_va = {c["virtual_account"]: c for c in customers}
        invoices_by_id = {i["invoice_id"]: dict(i) for i in invoices}
        balance = {i["invoice_id"]: i["amount_paise"] for i in invoices}
        ids_by_customer: dict[str, list[str]] = {}
        for inv in invoices:
            ids_by_customer.setdefault(inv["customer_id"], []).append(inv["invoice_id"])

        self.ctx = Context(customers_by_va=customers_by_va, invoices=invoices_by_id,
                            balance=balance, invoice_ids_by_customer=ids_by_customer)
        self._customers_by_id = {c["customer_id"]: c for c in customers}
        self.pending: dict[str, tuple[dict, object]] = {}
        self.ledger: list[dict] = []
        self.threshold = DEFAULT_AUTO_POST_THRESHOLD
        self._pay_seq = 0
        self.resolved_on_requeue_total = 0
        self.payment_log: list[dict] = []       # every payment ever submitted, raw
        self.customer_credit: dict[str, int] = {}   # unapplied on-account credit
        self.credit_events: dict[str, dict] = {}    # payment_id -> credit outcome
        self.action_sent: set[str] = set()
        self.action_drafts: dict[str, str] = {}

    def reset(self) -> dict:
        """The small hand-built demo world (simulator/seed.py) -- for the
        single-payment form and the scenario library. Every invoice starts
        open; scenario 6 (paid twice) settles its own bill as the first of
        its two scripted payments rather than relying on a pre-settled
        bootstrap, so scenarios stay correct in any order."""
        self._load_world(seed.CUSTOMERS, seed.INVOICES)
        self.world_name = "demo"
        return self.snapshot()

    def reset_batch(self) -> dict:
        """The real submission dataset (engine/generate_data.py) -- for
        Play mode. Reseeds the generator's RNG before building so repeated
        calls within one long-lived server process stay identical to the
        one committed to out/finance.db, not drift as the RNG advances."""
        from engine import generate_data

        generate_data.RNG.seed(42)
        w = generate_data.build_world()
        self._load_world(w.customers, w.invoices)
        self.world_name = "batch"

        ordered_payments = sorted(w.payments, key=lambda p: (p["created_at"], p["payment_id"]))
        self._export_payments_csv(ordered_payments)
        return {"world": "batch", "payments": ordered_payments, "state": self.snapshot()}

    def _export_payments_csv(self, payments: list[dict]) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        fieldnames = ["payment_id", "amount_paise", "virtual_account",
                      "payer_name", "narration", "method", "created_at"]
        with open(DATA_DIR / "payments.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for p in payments:
                writer.writerow({k: p.get(k, "") for k in fieldnames})

    # -- payments -------------------------------------------------------

    def submit_payment(self, fields: dict, payment_id: str | None = None,
                        created_at: str | None = None) -> dict:
        if payment_id is None:
            self._pay_seq += 1
            pid = f"PAY-SIM-{self._pay_seq:04d}"
        else:
            pid = payment_id
        payment = {
            "payment_id": pid,
            "amount_paise": fields["amount_paise"],
            "method": fields.get("method") or "NEFT",
            "virtual_account": fields.get("virtual_account") or None,
            "payer_name": fields.get("payer_name") or None,
            "narration": fields.get("narration", "") or "",
            "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.payment_log.append(payment)

        res = resolve_payment(payment, self.ctx)
        if res.resolved:
            self.ledger.extend(res.allocations)
            self._maybe_apply_credit(payment, res.allocations)
        else:
            self.pending[pid] = (payment, res)

        # Re-queue pass: mirrors engine.pipeline.run()'s fixed-point loop,
        # just driven by this one submission instead of a whole batch.
        resolved_on_requeue: list[str] = []
        for _ in range(MAX_REQUEUE_PASSES):
            if not self.pending:
                break
            newly_resolved = []
            for ppid, (ppayment, _prev) in list(self.pending.items()):
                pres = resolve_payment(ppayment, self.ctx)
                if pres.resolved:
                    self.ledger.extend(pres.allocations)
                    self._maybe_apply_credit(ppayment, pres.allocations)
                    newly_resolved.append(ppid)
                    if ppid != pid:
                        resolved_on_requeue.append(ppid)
                else:
                    self.pending[ppid] = (ppayment, pres)
            if not newly_resolved:
                break
            for ppid in newly_resolved:
                del self.pending[ppid]

        self.resolved_on_requeue_total += len(resolved_on_requeue)

        return {
            "payment_id": pid,
            "resolved": res.resolved,
            "allocation": res.allocations if res.resolved else None,
            "pending_reason": None if res.resolved else _code_value(res.pending_reason),
            "trace": res.trace,
            "resolved_on_requeue": resolved_on_requeue,
            "state": self.snapshot(),
        }

    def apply_dial(self, threshold: int) -> dict:
        old = self.threshold
        self.threshold = threshold
        moved = []
        for row in self.ledger:
            code = _code_value(row["reason_code"])
            if code in EXCEPTION_CODE_VALUES:
                continue
            was_auto = row["confidence"] >= old
            now_auto = row["confidence"] >= threshold
            if was_auto != now_auto:
                moved.append(row["payment_id"])
        return {"threshold": threshold, "moved_payment_ids": moved, "state": self.snapshot()}

    # -- L6 action layer --------------------------------------------------

    def _maybe_apply_credit(self, payment: dict, rows: list[dict]) -> None:
        """APPLY_CREDIT executes automatically (per the brief): a
        duplicate/overpayment credit is consumed by the customer's oldest
        currently-open invoice the moment it exists. The credit-application
        row is tagged ADJUSTMENT, not CASH -- the cash was already counted
        once, on the original on-account row; re-attributing it to an
        invoice moves no new money, so counting it again would double the
        CASH total against SUM(payments).

        Demo world only. engine/pipeline.py's batch run() has no
        equivalent step -- a DUP-ONACC payment there just parks as credit
        and never touches an invoice balance. Auto-applying it in batch
        world too silently closes/shrinks invoices the real submission
        dataset's later payments (built against engine/pipeline.py's
        behavior) still expect to be open, producing wrong matches for
        payments completely unrelated to the duplicate itself -- observed
        for 7 of the 64 submission payments before this guard was added.
        Skipping it here is what makes batch-world numbers reproduce
        scoring/score.py's results.json exactly."""
        if self.world_name == "batch":
            return
        cust = self.ctx.customers_by_va.get(payment.get("virtual_account"))
        if cust is None:
            return
        for row in rows:
            if row["reason_code"] == ReasonCode.DUP_ONACC and row["invoice_id"] is None:
                self._apply_credit(row["payment_id"], cust["customer_id"],
                                    row["amount_paise"], row["confidence"])

    def _apply_credit(self, payment_id: str, customer_id: str,
                       credit_amt: int, confidence: int) -> None:
        open_invoices = self.ctx.open_invoices(customer_id)
        applied_total = 0
        if open_invoices and credit_amt > 0:
            for split in l4_policy.waterfall_split(open_invoices, credit_amt):
                self.ctx.apply(split["invoice_id"], split["amount_paise"])
                applied_total += split["amount_paise"]
                self.ledger.append({
                    "payment_id": payment_id, "invoice_id": split["invoice_id"],
                    "amount_paise": split["amount_paise"], "method": "credit_applied",
                    "reason_code": ReasonCode.DUP_ONACC, "confidence": confidence,
                    "rationale": "on-account credit auto-applied to next unpaid invoice",
                    "entry_type": EntryType.ADJUSTMENT,
                })
        remainder = credit_amt - applied_total
        if remainder > 0:
            self.customer_credit[customer_id] = self.customer_credit.get(customer_id, 0) + remainder
        self.credit_events[payment_id] = {
            "customer_id": customer_id, "amount_paise": credit_amt,
            "applied_paise": applied_total, "remainder_paise": remainder,
        }

    def _get_or_draft(self, action_id: str, factory) -> str:
        if action_id not in self.action_drafts:
            self.action_drafts[action_id] = factory()
        return self.action_drafts[action_id]

    def send_action(self, action_id: str) -> dict:
        self.action_sent.add(action_id)
        return self.snapshot()

    def _build_actions(self, ledger_rows: list[dict], customers: list[dict],
                        pending_unmatched_by_customer: dict[str, int]) -> list[dict]:
        actions = []

        for row in ledger_rows:
            if row["reason_code"] == ReasonCode.SUSPENSE.value:
                action_id = f"hold-{row['payment_id']}"
                actions.append({
                    "action_id": action_id, "action_type": ActionType.HOLD_AND_ESCALATE.value,
                    "kind": "payment", "payment_id": row["payment_id"], "customer_id": None,
                    "target": row["payment_id"], "amount_paise": row["amount_paise"],
                    "draft": None, "status": "holding",
                    "note": "Unidentified funds -- held, no customer to query.",
                })
            elif row["reason_code"] == ReasonCode.DUP_ONACC.value and row["invoice_id"] is None:
                ev = self.credit_events.get(row["payment_id"], {})
                applied = ev.get("applied_paise", 0)
                remainder = ev.get("remainder_paise", row["amount_paise"])
                if remainder == 0:
                    status, note = "executed", f"Applied {applied/100:,.2f} to next open invoice(s)."
                elif applied > 0:
                    status = "partially executed"
                    note = f"Applied {applied/100:,.2f}; {remainder/100:,.2f} held, no open invoice left."
                elif self.world_name == "batch":
                    status, note = "holding", "Credit held on account (batch world does not auto-apply)."
                else:
                    status, note = "holding", "No open invoice yet -- credit held on account."
                actions.append({
                    "action_id": f"credit-{row['payment_id']}",
                    "action_type": ActionType.APPLY_CREDIT.value,
                    "kind": "payment", "payment_id": row["payment_id"], "customer_id": None,
                    "target": row["payment_id"], "amount_paise": row["amount_paise"],
                    "draft": None, "status": status, "note": note,
                })

        for pid, (payment, res) in self.pending.items():
            cust = self.ctx.customers_by_va.get(payment.get("virtual_account"))
            customer_name = cust["name"] if cust else "the customer"
            action_id = f"query-{pid}"
            draft = self._get_or_draft(action_id, lambda p=payment, n=customer_name, r=res:
                                        l6_action.draft_customer_query(p, n, r.pending_rationale))
            actions.append({
                "action_id": action_id, "action_type": ActionType.QUERY_CUSTOMER.value,
                "kind": "payment", "payment_id": pid,
                "customer_id": cust["customer_id"] if cust else None,
                "target": customer_name, "amount_paise": payment["amount_paise"],
                "draft": draft, "status": "sent" if action_id in self.action_sent else "draft",
                "note": res.pending_rationale,
            })

        for c in customers:
            if c["status"] != "PROVISIONAL":
                continue
            cid = c["customer_id"]
            action_id = f"stmt-{cid}"
            inv_ids = self.ctx.invoice_ids_by_customer.get(cid, [])
            total_invoiced = sum(self.ctx.invoices[i]["amount_paise"] for i in inv_ids)
            unmatched = pending_unmatched_by_customer.get(cid, 0)
            draft = self._get_or_draft(action_id, lambda c=c, ti=total_invoiced, u=unmatched:
                                        l6_action.draft_statement(
                                            c["name"], ti, c["total_outstanding_paise"], u,
                                            c["certain_balance_paise"]))
            actions.append({
                "action_id": action_id, "action_type": ActionType.SEND_STATEMENT.value,
                "kind": "customer", "payment_id": None, "customer_id": cid,
                "target": c["name"], "amount_paise": c["certain_balance_paise"],
                "draft": draft, "status": "sent" if action_id in self.action_sent else "draft",
                "note": f"{c['open_invoice_count']} open invoice(s), balance provisional",
            })

        actions.sort(key=lambda a: -a["amount_paise"])
        return actions

    # -- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict:
        invoices = []
        for inv_id, inv in self.ctx.invoices.items():
            cust = self._customers_by_id[inv["customer_id"]]
            invoices.append({
                "invoice_id": inv_id, "customer_id": inv["customer_id"],
                "customer_name": cust["name"], "virtual_account": cust["virtual_account"],
                "amount_paise": inv["amount_paise"],
                "balance_paise": self.ctx.balance[inv_id],
                "disputed": bool(inv["disputed"]),
            })
        invoices.sort(key=lambda i: i["invoice_id"])

        pending_unmatched_by_customer: dict[str, int] = {}
        for pid, (payment, res) in self.pending.items():
            cust = self.ctx.customers_by_va.get(payment.get("virtual_account"))
            if cust:
                cid = cust["customer_id"]
                pending_unmatched_by_customer[cid] = (
                    pending_unmatched_by_customer.get(cid, 0) + payment["amount_paise"])

        customers = []
        for cid, cust in self._customers_by_id.items():
            inv_ids = self.ctx.invoice_ids_by_customer.get(cid, [])
            balances = [self.ctx.balance[iid] for iid in inv_ids]
            total_invoiced = sum(self.ctx.invoices[i]["amount_paise"] for i in inv_ids)
            open_ledger = sum(balances)
            r = customer_status.rollup(cid, total_invoiced, open_ledger,
                                        pending_unmatched_by_customer.get(cid, 0))
            customers.append({
                "customer_id": cid, "name": cust["name"],
                "virtual_account": cust["virtual_account"],
                "open_invoice_count": sum(1 for b in balances if b > 0),
                "total_outstanding_paise": open_ledger,
                "status": r.status, "certain_balance_paise": r.balance_paise,
                "unapplied_credit_paise": self.customer_credit.get(cid, 0),
            })
        customers.sort(key=lambda c: c["name"])

        ledger_rows = []
        for row in self.ledger:
            code = _code_value(row["reason_code"])
            is_exception = code in EXCEPTION_CODE_VALUES
            ledger_rows.append({
                "payment_id": row["payment_id"], "invoice_id": row["invoice_id"],
                "amount_paise": row["amount_paise"], "method": row["method"],
                "reason_code": code, "entry_type": _code_value(row["entry_type"]),
                "confidence": row["confidence"],
                "rationale": row["rationale"], "is_exception": is_exception,
                "auto_posted": (not is_exception) and row["confidence"] >= self.threshold,
                "pending": False,
            })

        exception_queue = [r for r in ledger_rows if r["is_exception"]]
        for pid, (payment, res) in self.pending.items():
            exception_queue.append({
                "payment_id": pid, "invoice_id": None,
                "amount_paise": payment["amount_paise"], "method": payment["method"],
                "reason_code": _code_value(res.pending_reason), "confidence": None,
                "rationale": res.pending_rationale, "is_exception": True,
                "auto_posted": False, "pending": True,
            })
        exception_queue.sort(key=lambda r: -r["amount_paise"])

        actions = self._build_actions(ledger_rows, customers, pending_unmatched_by_customer)
        exception_value = sum(r["amount_paise"] for r in exception_queue)

        # Payment-level bucketing (shared with scoring/score.py's batch
        # report via engine.l5_exceptions.bucket_payments): one entry per
        # unique payment_id, NOT per ledger row. A single payment can
        # produce several ledger rows -- BULK-N splits across every
        # invoice it covers, TOL-FEE/RESID-DED split into a CASH row and
        # an ADJUSTMENT row -- and counting rows instead of payments let
        # auto_matched + human_touch exceed the actual payment count
        # (e.g. 43 + 51 = 94 against 60 payments). Confidence and
        # reason_code are uniform across one payment's rows by
        # construction (engine/pipeline.py), so grouping by payment_id and
        # taking any row's values is safe.
        payment_va = {p["payment_id"]: p.get("virtual_account") for p in self.payment_log}
        rows_by_payment: dict[str, list[dict]] = {}
        for r in ledger_rows:
            rows_by_payment.setdefault(r["payment_id"], []).append(r)
        # SUSPENSE is terminal -- unlike AMBIG-N/NO-MATCH it's never
        # retried, so it's written straight to self.ledger rather than
        # left in self.pending (see engine/pipeline.py). Its rows still
        # carry is_exception=True, so a payment's ledger rows are only
        # "resolved" for bucketing purposes when none of them are.
        bucket_entries = [
            {"payment_id": pid, "resolved": not rows[0]["is_exception"],
             "confidence": min(r["confidence"] for r in rows),
             "reason_code": rows[0]["reason_code"],
             "virtual_account": payment_va.get(pid)}
            for pid, rows in rows_by_payment.items()
        ]
        bucket_entries.extend(
            {"payment_id": pid, "resolved": False, "confidence": None,
             "reason_code": _code_value(res.pending_reason),
             "virtual_account": payment.get("virtual_account")}
            for pid, (payment, res) in self.pending.items()
        )
        buckets = bucket_payments(bucket_entries, self.threshold)

        open_statement_actions = [
            a for a in actions
            if a["action_type"] == ActionType.SEND_STATEMENT.value and a["status"] == "draft"]

        # Single definition of "a human decision is needed here," shared by
        # the header's Human touch KPI and the strip's Decisions needed --
        # both are now buckets["exceptions_grouped"] (several exceptions
        # for the same customer collapse into one sitting, same grouping
        # scoring/score.py uses) + buckets["needs_confirmation"]
        # (below-dial resolved payments, not grouped further) + open
        # statement drafts, which the batch report has no equivalent of.
        human_touch = (buckets["exceptions_grouped"] + buckets["needs_confirmation"]
                        + len(open_statement_actions))

        # "Without the agent": every payment is an opaque bank-statement
        # row needing its own decision, so rows_to_read == decisions_needed
        # == the total payment count -- no grouping, nothing pre-resolved.
        without_rows = len(self.payment_log)
        assert without_rows == buckets["total"], (
            f"payment_log ({without_rows}) and bucket total ({buckets['total']}) "
            "disagree on how many payments exist")
        with_rows_to_read = (buckets["exceptions"] + buckets["needs_confirmation"]
                              + len(open_statement_actions))
        comparison = {
            "without": {
                "rows_to_read": without_rows, "decisions_needed": without_rows,
                "estimated_minutes": without_rows * MINUTES_PER_MANUAL_ROW,
                "estimated_label": _fmt_minutes(without_rows * MINUTES_PER_MANUAL_ROW),
            },
            "with": {
                "rows_to_read": with_rows_to_read, "decisions_needed": human_touch,
                "estimated_minutes": human_touch * MINUTES_PER_ACTION_REVIEW,
                "estimated_label": _fmt_minutes(human_touch * MINUTES_PER_ACTION_REVIEW),
            },
        }

        return {
            "world": self.world_name,
            "invoices": invoices,
            "customers": customers,
            "ledger": ledger_rows,
            "exception_queue": exception_queue,
            "actions": actions,
            "payment_log": self.payment_log,
            "comparison": comparison,
            "buckets": buckets,
            "kpis": {
                "threshold": self.threshold,
                "auto_matched": buckets["auto_posted"],
                "human_touch": human_touch,
                "exception_value_paise": exception_value,
                "resolved_on_requeue_total": self.resolved_on_requeue_total,
            },
        }
