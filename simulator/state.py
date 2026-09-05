"""In-memory session state for the simulator. Every decision is made by
calling engine.pipeline.resolve_payment() directly -- the same function
run_engine.py uses in batch -- so the simulator can never drift from the
real matching logic. This module adds bookkeeping (pending exceptions,
the live ledger, the confidence dial), on-account credit consumption, and
the L6 action layer that turns a reason code into a proposed next step.
"""
from __future__ import annotations

import copy
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from engine import customer_status, l4_policy, l6_action
from engine.confidence import DEFAULT_AUTO_POST_THRESHOLD
from engine.l5_exceptions import bucket_payments
from engine.pipeline import MAX_REQUEUE_PASSES, Context, resolve_payment
from engine.reason_codes import EXCEPTIONS, ActionType, EntryType, ReasonCode
from simulator import ask_ledger, batch_seed, live_seed, seed, story_seed

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
        self._init_live_world()
        self._init_story_world()
        self._init_process_world()

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

    def dial_whatif(self, threshold: int) -> dict:
        """Read-only counterpart to apply_dial(): reports what WOULD change
        at a candidate threshold without touching self.threshold or any
        other state. Used by simulator/ask_ledger.py so "what would change
        if I set the dial to 95?" can be answered with real numbers instead
        of asking the model to reason about arithmetic it can't see."""
        moved = []
        for row in self.ledger:
            code = _code_value(row["reason_code"])
            if code in EXCEPTION_CODE_VALUES:
                continue
            was_auto = row["confidence"] >= self.threshold
            now_auto = row["confidence"] >= threshold
            if was_auto != now_auto:
                moved.append(row["payment_id"])
        return {
            "current_threshold": self.threshold,
            "candidate_threshold": threshold,
            "moved_payment_ids": moved,
            "buckets_now": bucket_payments(self._bucket_entries(), self.threshold),
            "buckets_at_candidate": bucket_payments(self._bucket_entries(), threshold),
        }

    # -- explain (Section D) -----------------------------------------------

    def explain_context(self, payment_id: str) -> dict | None:
        """Read-only material for simulator/ask_ledger.explain_refusal():
        the payment's full decision trace plus the candidate invoices it
        was weighed against. Returns None if payment_id is unknown or
        isn't actually an exception (the "why couldn't you solve this?"
        button only ever appears on exception rows).

        Replays resolve_payment() to get a fresh trace rather than storing
        one from submission time -- verified side-effect-free here: an
        unresolved AMBIG-N/NO-MATCH payment (still in self.pending) never
        called ctx.apply() the first time either, and a terminal SUSPENSE
        payment's branch in engine/pipeline.py never touches ctx.apply()
        at all (no customer is even identified). The replayed Resolution
        is used for its trace/rationale only and is never written back to
        self.ledger or self.pending.
        """
        payment = None
        if payment_id in self.pending:
            payment, _ = self.pending[payment_id]
        else:
            payment = next((p for p in self.payment_log if p["payment_id"] == payment_id), None)
        if payment is None:
            return None

        res = resolve_payment(payment, self.ctx)
        if res.resolved:
            reason_code = _code_value(res.allocations[0]["reason_code"])
            rationale = "; ".join(r["rationale"] for r in res.allocations)
        else:
            reason_code = _code_value(res.pending_reason)
            rationale = res.pending_rationale
        if reason_code not in EXCEPTION_CODE_VALUES:
            return None

        cust = self.ctx.customers_by_va.get(payment.get("virtual_account"))
        candidates = self.ctx.open_invoices(cust["customer_id"]) if cust else []
        return {
            "payment": payment,
            "customer_name": cust["name"] if cust else None,
            "trace": res.trace,
            "reason_code": reason_code,
            "rationale": rationale,
            "candidates": candidates,
        }

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

    # -- bucketing ----------------------------------------------------------

    def _bucket_entries(self) -> list[dict]:
        """One entry per unique payment_id -- {"payment_id", "resolved",
        "confidence", "reason_code", "virtual_account"} -- for
        engine.l5_exceptions.bucket_payments(). Shared by snapshot() (at the
        live threshold) and dial_whatif() (at a hypothetical one) so both
        always bucket the exact same payments the exact same way; see
        bucket_payments's own docstring for why grouping by payment_id
        (not ledger row) matters -- BULK-N splits across every invoice it
        covers, TOL-FEE/RESID-DED split into a CASH row and an ADJUSTMENT
        row, and counting rows instead of payments lets auto_matched +
        human_touch exceed the actual payment count.

        SUSPENSE is terminal -- unlike AMBIG-N/NO-MATCH it's never
        retried, so it's written straight to self.ledger rather than left
        in self.pending (see engine/pipeline.py). Its rows still count as
        an exception, so a payment's ledger rows are only "resolved" for
        bucketing purposes when none of them are.
        """
        payment_va = {p["payment_id"]: p.get("virtual_account") for p in self.payment_log}
        rows_by_payment: dict[str, list[dict]] = {}
        for row in self.ledger:
            code = _code_value(row["reason_code"])
            rows_by_payment.setdefault(row["payment_id"], []).append(
                {"confidence": row["confidence"], "reason_code": code,
                 "is_exception": code in EXCEPTION_CODE_VALUES})
        entries = [
            {"payment_id": pid, "resolved": not rows[0]["is_exception"],
             "confidence": min(r["confidence"] for r in rows),
             "reason_code": rows[0]["reason_code"],
             "virtual_account": payment_va.get(pid)}
            for pid, rows in rows_by_payment.items()
        ]
        entries.extend(
            {"payment_id": pid, "resolved": False, "confidence": None,
             "reason_code": _code_value(res.pending_reason),
             "virtual_account": payment.get("virtual_account")}
            for pid, (payment, res) in self.pending.items()
        )
        return entries

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

        buckets = bucket_payments(self._bucket_entries(), self.threshold)

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

    # -- Live world (Section 3) --------------------------------------------
    #
    # Independent of Demo/Batch above -- not swapped into self.ctx, never
    # touched by reset()/reset_batch()/submit_payment(). A Razorpay webhook
    # can arrive at any time regardless of what the UI is currently
    # showing, so Live gets its own always-on bundle of state, seeded once
    # here and never reset. This duplicates a small amount of the
    # resolve+requeue shape from submit_payment() above rather than
    # generalizing that method to take a world argument -- deliberately,
    # to guarantee zero behavior change to Demo/Batch (see
    # _maybe_apply_credit's world_name-gated special case above, which a
    # careless generalization could easily perturb).

    def _init_live_world(self) -> None:
        customers_by_va = {c["virtual_account"]: c for c in live_seed.CUSTOMERS}
        invoices_by_id = {i["invoice_id"]: dict(i) for i in live_seed.INVOICES}
        balance = {i["invoice_id"]: i["amount_paise"] for i in live_seed.INVOICES}
        ids_by_customer: dict[str, list[str]] = {}
        for inv in live_seed.INVOICES:
            ids_by_customer.setdefault(inv["customer_id"], []).append(inv["invoice_id"])

        self.live_ctx = Context(customers_by_va=customers_by_va, invoices=invoices_by_id,
                                 balance=balance, invoice_ids_by_customer=ids_by_customer)
        self._live_customers_by_id = {c["customer_id"]: c for c in live_seed.CUSTOMERS}
        self.live_ledger: list[dict] = []
        self.live_pending: dict[str, tuple[dict, object]] = {}
        self.live_payment_log: list[dict] = []
        self.live_processed_ids: set[str] = set()
        self.live_outcomes: dict[str, dict] = {}
        # One entry per payment ever received, oldest first -- the /live
        # page's equivalent of /story's story_events, except payments
        # arrive one at a time over real wall-clock time instead of being
        # played from a fixed script, so this grows live instead of being
        # precomputed. Updated in place when a later payment's requeue
        # pass resolves an earlier one (same pattern as _proc_advance).
        self.live_events: list[dict] = []
        self.live_actions_sent: set[str] = set()

    def submit_live_payment(self, payment: dict) -> dict:
        """Idempotent on payment_id -- Razorpay retries webhook deliveries
        (non-2xx or timeout), so a repeat delivery must return the same
        outcome without reprocessing or double-allocating."""
        pid = payment["payment_id"]
        if pid in self.live_processed_ids:
            return {**self.live_outcomes[pid], "duplicate": True}

        self.live_payment_log.append(payment)
        res = resolve_payment(payment, self.live_ctx)
        if res.resolved:
            self.live_ledger.extend(res.allocations)
            first_code = _code_value(res.allocations[0]["reason_code"])
        else:
            self.live_pending[pid] = (payment, res)
            first_code = _code_value(res.pending_reason)

        cust = self.live_ctx.customers_by_va.get(payment.get("virtual_account"))
        event = {
            "payment_id": pid, "amount_paise": payment["amount_paise"],
            "virtual_account": payment.get("virtual_account"),
            "customer_name": cust["name"] if cust else None,
            "narration": payment.get("narration", ""),
            "received_at": payment.get("created_at"),
            "final_code": first_code, "resolved_on_requeue": False,
            "confidence": None, "bills": [], "real_paise": 0, "writeoff_paise": 0,
            "rationale": res.pending_rationale if not res.resolved else None,
            "trace": res.trace,
        }
        if res.resolved:
            self._live_fill_result(event, res)
        self.live_events.append(event)

        resolved_on_requeue: list[str] = []
        for _ in range(MAX_REQUEUE_PASSES):
            if not self.live_pending:
                break
            newly_resolved = []
            for ppid, (ppayment, _prev) in list(self.live_pending.items()):
                pres = resolve_payment(ppayment, self.live_ctx)
                if pres.resolved:
                    self.live_ledger.extend(pres.allocations)
                    newly_resolved.append((ppid, pres))
                else:
                    self.live_pending[ppid] = (ppayment, pres)
            if not newly_resolved:
                break
            for ppid, pres in newly_resolved:
                del self.live_pending[ppid]
                if ppid != pid:
                    resolved_on_requeue.append(ppid)
                for ev in self.live_events:
                    if ev["payment_id"] != ppid:
                        continue
                    self._live_fill_result(ev, pres)
                    ev["resolved_on_requeue"] = ppid != pid

        outcome = {
            "payment_id": pid,
            "resolved": res.resolved,
            "allocation": res.allocations if res.resolved else None,
            "pending_reason": None if res.resolved else _code_value(res.pending_reason),
            "trace": res.trace,
            "resolved_on_requeue": resolved_on_requeue,
            "duplicate": False,
        }
        self.live_processed_ids.add(pid)
        self.live_outcomes[pid] = outcome
        return outcome

    def _live_fill_result(self, ev: dict, res) -> None:
        allocs = res.allocations
        ev["final_code"] = _code_value(allocs[0]["reason_code"])
        ev["confidence"] = min(r["confidence"] for r in allocs)
        ev["bills"] = self._story_bills(allocs)
        ev["real_paise"], ev["writeoff_paise"] = self._story_money(allocs)
        ev["rationale"] = allocs[0]["rationale"]

    def _live_setup(self) -> list[dict]:
        setup = []
        for cid, cust in self._live_customers_by_id.items():
            inv_ids = self.live_ctx.invoice_ids_by_customer.get(cid, [])
            setup.append({
                "customer_id": cid, "name": cust["name"],
                "account_number": cust["virtual_account"],
                "invoices": [{
                    "invoice_id": iid,
                    "amount_paise": self.live_ctx.invoices[iid]["amount_paise"],
                    "balance_paise": self.live_ctx.balance[iid],
                    "disputed": bool(self.live_ctx.invoices[iid]["disputed"]),
                } for iid in inv_ids],
            })
        return setup

    def _live_bucket_of(self, ev: dict) -> str:
        if ev["final_code"] in EXCEPTION_CODE_VALUES:
            return "held"
        if ev["confidence"] is not None and ev["confidence"] >= DEFAULT_AUTO_POST_THRESHOLD:
            return "done"
        return "check"

    def _live_sorted_out(self) -> list[dict]:
        rows = []
        for i, ev in enumerate(self.live_events, start=1):
            bucket = self._live_bucket_of(ev)
            if bucket == "held":
                continue
            rows.append({
                "i": i, "payment_id": ev["payment_id"], "amount_paise": ev["amount_paise"],
                "bills_label": self._story_bills_label(
                    {"final_code": ev["final_code"], "bills": ev["bills"]}),
                "code": ev["final_code"], "code_plain": story_seed.CODE_PLAIN.get(ev["final_code"], ""),
                "how_sure": "Done automatically" if bucket == "done" else "Needs a quick check",
                "sure_ok": bucket == "done",
                "real_paise": ev["real_paise"], "writeoff_paise": ev["writeoff_paise"],
                "sorted_later": ev["resolved_on_requeue"],
            })
        rows.sort(key=lambda r: -r["i"])
        return rows

    def _live_could_not_sort(self) -> list[dict]:
        rows = []
        for i, ev in enumerate(self.live_events, start=1):
            if self._live_bucket_of(ev) != "held":
                continue
            name = ev["customer_name"] or "Unknown sender"
            cust = self.live_ctx.customers_by_va.get(ev["virtual_account"])
            rows.append({
                "i": i, "payment_id": ev["payment_id"], "amount_paise": ev["amount_paise"],
                "customer": name, "customer_id": cust["customer_id"] if cust else None,
                "code": ev["final_code"], "code_plain": story_seed.CODE_PLAIN.get(ev["final_code"], ""),
                "reason": ev["rationale"] or "",
            })
        rows.sort(key=lambda r: -r["i"])
        return rows

    def _live_doing_about_it(self, could_not_sort: list[dict]) -> list[dict]:
        actions: list[dict] = []
        by_customer: dict[str, list[dict]] = {}
        suspense: list[dict] = []
        for row in could_not_sort:
            if row["code"] == "SUSPENSE":
                suspense.append(row)
            else:
                by_customer.setdefault(row["customer"], []).append(row)
        for name, rows in by_customer.items():
            amts = ", ".join(f"Rs {r['amount_paise'] // 100:,}" for r in sorted(rows, key=lambda r: r["i"]))
            action_id = f"query-{rows[0].get('customer_id') or name}"
            actions.append({
                "action_id": action_id,
                "text": (f"Ask {name} which bill their {amts} payment is for." if len(rows) == 1
                         else f"Ask {name} which bills {len(rows)} payments are for - {amts}."),
                "done": False, "draft": None,
                "sent": action_id in self.live_actions_sent,
            })
        for row in suspense:
            actions.append({
                "action_id": f"hold-{row['payment_id']}",
                "text": (f"Hold the Rs {row['amount_paise'] // 100:,} payment. Someone needs to "
                         "check who sent it."),
                "done": False, "draft": None, "sent": False,
            })
        return actions

    def send_live_action(self, action_id: str) -> None:
        self.live_actions_sent.add(action_id)

    def live_snapshot(self) -> dict:
        invoices = []
        for inv_id, inv in self.live_ctx.invoices.items():
            cust = self._live_customers_by_id[inv["customer_id"]]
            invoices.append({
                "invoice_id": inv_id, "customer_id": inv["customer_id"],
                "customer_name": cust["name"], "virtual_account": cust["virtual_account"],
                "amount_paise": inv["amount_paise"],
                "balance_paise": self.live_ctx.balance[inv_id],
                "disputed": bool(inv["disputed"]),
            })
        invoices.sort(key=lambda i: i["invoice_id"])

        customers = []
        for cid, cust in self._live_customers_by_id.items():
            inv_ids = self.live_ctx.invoice_ids_by_customer.get(cid, [])
            balances = [self.live_ctx.balance[iid] for iid in inv_ids]
            customers.append({
                "customer_id": cid, "name": cust["name"],
                "virtual_account": cust["virtual_account"],
                "open_invoice_count": sum(1 for b in balances if b > 0),
                "total_outstanding_paise": sum(balances),
            })
        customers.sort(key=lambda c: c["name"])

        ledger_rows = []
        for row in self.live_ledger:
            code = _code_value(row["reason_code"])
            is_exception = code in EXCEPTION_CODE_VALUES
            ledger_rows.append({
                "payment_id": row["payment_id"], "invoice_id": row["invoice_id"],
                "amount_paise": row["amount_paise"], "method": row["method"],
                "reason_code": code, "entry_type": _code_value(row["entry_type"]),
                "confidence": row["confidence"], "rationale": row["rationale"],
                "is_exception": is_exception, "pending": False,
            })

        exception_queue = [r for r in ledger_rows if r["is_exception"]]
        for pid, (payment, res) in self.live_pending.items():
            exception_queue.append({
                "payment_id": pid, "invoice_id": None,
                "amount_paise": payment["amount_paise"], "method": payment["method"],
                "reason_code": _code_value(res.pending_reason), "confidence": None,
                "rationale": res.pending_rationale, "is_exception": True, "pending": True,
            })
        exception_queue.sort(key=lambda r: -r["amount_paise"])

        auto_matched = sum(1 for r in ledger_rows if not r["is_exception"])
        could_not_sort = self._live_could_not_sort()
        return {
            "world": "live",
            "invoices": invoices,
            "customers": customers,
            "ledger": ledger_rows,
            "exception_queue": exception_queue,
            "payment_log": self.live_payment_log,
            "kpis": {
                "auto_matched": auto_matched,
                "human_touch": len(exception_queue),
            },
            # -- /live page additions: same shape as /story/process so the
            # page can reuse the same layout (shops panel, decision trace,
            # three result panels). --
            "setup": self._live_setup(),
            "code_plain": story_seed.CODE_PLAIN,
            "code_badge": story_seed.CODE_BADGE,
            "events": self.live_events,
            "sorted_out": self._live_sorted_out(),
            "could_not_sort": could_not_sort,
            "doing_about_it": self._live_doing_about_it(could_not_sort),
        }

    # -- Story world (/story) ---------------------------------------------
    #
    # A fixed 13-payment script (simulator/story_seed.py) played one step
    # at a time. Same shape as the Live world above -- its own always-on
    # Context, its own resolve+requeue loop -- so nothing here can perturb
    # Demo/Batch/Live. Every payment still goes through the one real
    # engine.pipeline.resolve_payment(); this world only scripts the order
    # and narrates the result.

    def _init_story_world(self) -> None:
        customers = story_seed.customers()
        invoices = story_seed.invoices()
        balance = {i["invoice_id"]: i["amount_paise"] for i in invoices}
        ids_by_customer: dict[str, list[str]] = {}
        for inv in invoices:
            ids_by_customer.setdefault(inv["customer_id"], []).append(inv["invoice_id"])
        self.story_ctx = Context(
            customers_by_va={c["virtual_account"]: c for c in customers},
            invoices={i["invoice_id"]: dict(i) for i in invoices},
            balance=balance, invoice_ids_by_customer=ids_by_customer)
        self._story_customers_by_id = {c["customer_id"]: c for c in customers}
        self.story_scripted = story_seed.payments()
        self.story_step_index = 0
        self.story_ledger: list[dict] = []
        self.story_pending: dict[str, tuple[dict, object]] = {}
        self.story_events: list[dict] = []
        self.story_resolved_on_requeue_total = 0
        self.story_threshold = DEFAULT_AUTO_POST_THRESHOLD
        self.story_actions_sent: set[str] = set()   # draft ids a person clicked Send on
        self._story_full_day = self._story_dry_run()
        # One frozen snapshot per step: story_history[k] is the whole
        # picture right after payment k settled (story_history[0] is the
        # opening state, before any payment). The entire day is played
        # here, up front, so navigation on the page (Previous / click a
        # payment / arrow keys) is pure history playback -- it never
        # re-runs the engine -- and a viewer always sees the panels as
        # they were AT THAT POINT, not the final state.
        self.story_history: list[dict] = [self._story_frozen_snapshot()]
        while self.story_step_index < len(self.story_scripted):
            self._story_advance()

    def _story_frozen_snapshot(self) -> dict:
        return copy.deepcopy(self.story_snapshot())

    def story_view(self) -> dict:
        """What the /story page loads: every frozen frame of the day, plus
        which drafted messages a person has marked sent. The page navigates
        these frames entirely on its own."""
        return {
            "step": self.story_step_index,
            "total_steps": len(self.story_scripted),
            "done": self.story_step_index >= len(self.story_scripted),
            "history": self.story_history,
            "sent": sorted(self.story_actions_sent),
        }

    def _story_dry_run(self) -> dict:
        """Play the whole fixed 13-payment script once in a throwaway
        Context, so the Without/With comparison in the header can show the
        full day's numbers from the first frame -- not creep up as you
        step. No effect on the real story_ctx."""
        customers = story_seed.customers()
        invoices = story_seed.invoices()
        ids: dict[str, list[str]] = {}
        for inv in invoices:
            ids.setdefault(inv["customer_id"], []).append(inv["invoice_id"])
        ctx = Context(
            customers_by_va={c["virtual_account"]: c for c in customers},
            invoices={i["invoice_id"]: dict(i) for i in invoices},
            balance={i["invoice_id"]: i["amount_paise"] for i in invoices},
            invoice_ids_by_customer=ids)
        outcomes: dict[str, dict] = {}
        pending: dict[str, tuple[dict, object]] = {}

        def record(pid, r):
            if r.resolved:
                outcomes[pid] = {"code": _code_value(r.allocations[0]["reason_code"]),
                                  "conf": min(a["confidence"] for a in r.allocations)}
            else:
                outcomes[pid] = {"code": _code_value(r.pending_reason), "conf": None}

        for sp in self.story_scripted:
            ep = {k: sp[k] for k in ("payment_id", "amount_paise", "method",
                                      "virtual_account", "payer_name", "narration",
                                      "created_at")}
            r = resolve_payment(ep, ctx)
            record(sp["payment_id"], r)
            if not r.resolved:
                pending[sp["payment_id"]] = (ep, r)
            for _ in range(MAX_REQUEUE_PASSES):
                if not pending:
                    break
                newly = []
                for ppid, (pp, _prev) in list(pending.items()):
                    pr = resolve_payment(pp, ctx)
                    if pr.resolved:
                        record(ppid, pr)
                        newly.append(ppid)
                    else:
                        pending[ppid] = (pp, pr)
                if not newly:
                    break
                for ppid in newly:
                    del pending[ppid]

        va_by_pid = {sp["payment_id"]: sp["virtual_account"] for sp in self.story_scripted}
        checks = held_named = held_suspense = 0
        held_customers = set()
        for pid, o in outcomes.items():
            if o["code"] in {"AMBIG-N", "NO-MATCH"}:
                held_customers.add(va_by_pid[pid])
            elif o["code"] == "SUSPENSE":
                held_suspense += 1
            elif o["conf"] is not None and o["conf"] < self.story_threshold:
                checks += 1
        held_named = sum(1 for o in outcomes.values()
                          if o["code"] in {"AMBIG-N", "NO-MATCH"})
        with_rows = checks + held_named + held_suspense
        with_decisions = checks + len(held_customers) + held_suspense
        total = len(self.story_scripted)
        return {
            "without": {"rows_to_read": total, "decisions": total,
                        "minutes": total * MINUTES_PER_MANUAL_ROW},
            "with": {"rows_to_read": with_rows, "decisions": with_decisions,
                     "minutes": with_decisions * MINUTES_PER_ACTION_REVIEW},
        }

    def reset_story(self) -> dict:
        """The frozen frames never change (the day is fully deterministic),
        so 'Start over' only has to forget which drafts were marked sent --
        the page just navigates back to the opening frame itself."""
        self.story_actions_sent = set()
        return self.story_view()

    def send_story_action(self, action_id: str) -> dict:
        """Mark a drafted message as sent -- on screen only. Nothing is
        actually sent anywhere. The page overlays this set onto the frozen
        frames, so no frame needs rebuilding."""
        self.story_actions_sent.add(action_id)
        return self.story_view()

    def _story_customer_name(self, va: str | None) -> str | None:
        cust = self.story_ctx.customers_by_va.get(va) if va else None
        return cust["name"] if cust else None

    @staticmethod
    def _story_bills(allocations: list[dict]) -> list[str]:
        seen: list[str] = []
        for r in allocations:
            iid = r["invoice_id"]
            if iid and iid not in seen:
                seen.append(iid)
        return seen

    @staticmethod
    def _story_money(allocations: list[dict]) -> tuple[int, int]:
        """(real money in, amount written off) for one payment's rows."""
        real = sum(r["amount_paise"] for r in allocations
                   if _code_value(r["entry_type"]) == "CASH")
        writeoff = sum(r["amount_paise"] for r in allocations
                       if _code_value(r["entry_type"]) == "ADJUSTMENT")
        return real, writeoff

    def _story_fill_result(self, ev: dict, res) -> None:
        """Copy the engine's outcome for a resolved payment onto its event."""
        allocs = res.allocations
        ev["final_code"] = _code_value(allocs[0]["reason_code"])
        ev["confidence"] = min(r["confidence"] for r in allocs)
        ev["bills"] = self._story_bills(allocs)
        ev["real_paise"], ev["writeoff_paise"] = self._story_money(allocs)

    def story_step(self) -> dict:
        """Play the next scripted payment, if any. The whole day is already
        played at construction time (see _init_story_world), so on the live
        page this is a no-op -- navigation is pure history playback. Kept as
        an endpoint for tests and manual stepping."""
        if self.story_step_index < len(self.story_scripted):
            self._story_advance()
        return self.story_view()

    def _story_advance(self) -> None:
        """Play the next scripted payment through the real engine, run the
        re-check pass, record the plain-English narration, and freeze the
        resulting frame into story_history."""
        scripted = self.story_scripted[self.story_step_index]
        self.story_step_index += 1
        pid = scripted["payment_id"]
        engine_payment = {k: scripted[k] for k in (
            "payment_id", "amount_paise", "method", "virtual_account",
            "payer_name", "narration", "created_at")}

        res = resolve_payment(engine_payment, self.story_ctx)
        if res.resolved:
            self.story_ledger.extend(res.allocations)
            first_code = _code_value(res.allocations[0]["reason_code"])
        else:
            self.story_pending[pid] = (engine_payment, res)
            first_code = _code_value(res.pending_reason)

        event = {
            "n": scripted["n"],
            "payment_id": pid,
            "amount_paise": scripted["amount_paise"],
            "virtual_account": scripted["virtual_account"],
            "customer_name": self._story_customer_name(scripted["virtual_account"]),
            "narration": scripted["narration"],
            "story_line": scripted["story_line"],
            "how_it_decided": scripted["how_it_decided"],
            "what_changed": scripted["what_changed"],
            "why_held": scripted["why_held"],
            "first_code": first_code,
            "final_code": first_code,
            "resolved_on_requeue": False,
            "confidence": None,
            "bills": [],
            "real_paise": 0,
            "writeoff_paise": 0,
        }
        if res.resolved:
            self._story_fill_result(event, res)
        self.story_events.append(event)

        # re-check pass: an earlier held payment can become sortable once a
        # later one changes what is still open (mirrors engine.pipeline.run)
        resolved_on_requeue: list[str] = []
        for _ in range(MAX_REQUEUE_PASSES):
            if not self.story_pending:
                break
            newly: list[tuple[str, object]] = []
            for ppid, (pp, _prev) in list(self.story_pending.items()):
                pr = resolve_payment(pp, self.story_ctx)
                if pr.resolved:
                    self.story_ledger.extend(pr.allocations)
                    newly.append((ppid, pr))
                else:
                    self.story_pending[ppid] = (pp, pr)
            if not newly:
                break
            for ppid, pr in newly:
                del self.story_pending[ppid]
                if ppid != pid:
                    resolved_on_requeue.append(ppid)
                for ev in self.story_events:
                    if ev["payment_id"] != ppid:
                        continue
                    self._story_fill_result(ev, pr)
                    ev["resolved_on_requeue"] = ppid != pid
                    bill = ev["bills"][0] if ev["bills"] else "a bill"
                    ev["what_changed"] = (
                        f"Sorted out on its own once payment {scripted['n']} "
                        f"settled the disputed bill - this Rs "
                        f"{ev['amount_paise'] // 100:,} paid bill {bill}.")
        self.story_resolved_on_requeue_total += len(resolved_on_requeue)

        # freeze this step's whole picture for the replay history
        self.story_history.append(self._story_frozen_snapshot())

    # -- narration helpers ------------------------------------------------

    def _story_bucket(self, ev: dict) -> str:
        """done = sorted with no check needed; check = sorted but a person
        should glance at it; held = we could not sort it."""
        if ev["final_code"] in {"AMBIG-N", "NO-MATCH", "SUSPENSE"}:
            return "held"
        if ev["confidence"] is not None and ev["confidence"] >= self.story_threshold:
            return "done"
        return "check"

    def _story_bills_label(self, ev: dict) -> str:
        if ev["final_code"] == "DUP-ONACC":
            return "kept aside as credit"
        if ev["final_code"] == "SUSPENSE":
            return "set aside, sender unknown"
        bills = ev["bills"]
        if not bills:
            return "-"
        if len(bills) == 1:
            return f"bill {bills[0]}"
        return "bills " + " + ".join(bills)

    def _story_could_not_sort(self) -> list[dict]:
        rows = []
        for ev in self.story_events:
            if self._story_bucket(ev) != "held":
                continue
            name = ev["customer_name"] or "Unknown sender"
            amt = ev["amount_paise"] // 100
            if ev["final_code"] == "SUSPENSE":
                reason = "No account and no name - we cannot tell who paid."
            elif ev["final_code"] == "AMBIG-N":
                reason = (f"Rs {amt:,} fits more than one of {name}'s bills, "
                          "and one of them is under dispute.")
            else:  # NO-MATCH
                reason = f"Nothing {name} owes matches Rs {amt:,}."
            scripted = next((p for p in self.story_scripted
                             if p["payment_id"] == ev["payment_id"]), {})
            cust = self.story_ctx.customers_by_va.get(ev["virtual_account"])
            rows.append({
                "n": ev["n"], "payment_id": ev["payment_id"],
                "amount_paise": ev["amount_paise"], "customer": name,
                "customer_id": cust["customer_id"] if cust else None,
                "date_long": scripted.get("date_long", ""),
                "code": ev["final_code"],
                "code_plain": story_seed.CODE_PLAIN.get(ev["final_code"], ""),
                "reason": reason, "why": ev["why_held"],
            })
        rows.sort(key=lambda r: -r["n"])
        return rows

    @staticmethod
    def _join_and(items: list[str]) -> str:
        if len(items) <= 1:
            return items[0] if items else ""
        return ", ".join(items[:-1]) + " and " + items[-1]

    def _story_open_bills_text(self, cid: str) -> list[str]:
        out = []
        for inv in self.story_ctx.open_invoices(cid):
            bal, amt = inv["balance"], inv["amount_paise"]
            if bal == amt:
                out.append(f"{inv['invoice_id']} (Rs {amt // 100:,})")
            else:
                out.append(f"{inv['invoice_id']} (Rs {bal // 100:,} remaining)")
        return out

    def _story_draft_message(self, name: str, rows: list[dict],
                              certain_balance_paise: int, cid: str | None) -> str:
        """The concrete customer message a person can copy and send. Built
        here (not engine/l6_action.py, which is single-payment and jargon-y)
        so every name, amount and bill number in it is real and current."""
        rows = sorted(rows, key=lambda r: r["n"])
        n = len(rows)
        count = {1: "one payment", 2: "two payments", 3: "three payments"}.get(n, f"{n} payments")
        subject = ("A payment we could not match to a bill" if n == 1
                   else f"{count.capitalize()} we could not match to a bill")
        date = rows[0]["date_long"] or "this month"
        amount_lines = "\n".join(f"    Rs {r['amount_paise'] // 100:,}" for r in rows)
        total = sum(r["amount_paise"] for r in rows) // 100
        open_bills = self._story_open_bills_text(cid) if cid else []
        bills_txt = self._join_and(open_bills) if open_bills else "not on file"

        body = (
            f"To: {name}\n"
            f"Subject: {subject}\n\n"
            "Hi,\n\n"
            f"We received {count} from you on {date} that we could not match "
            "to a bill:\n\n"
            f"{amount_lines}\n\n"
        )
        if n > 1:
            body += f"That is Rs {total:,} in total. "
        body += f"Your outstanding balance is Rs {certain_balance_paise // 100:,}.\n\n"
        this_these = "these" if n > 1 else "this"
        was_were = "were" if n > 1 else "was"
        body += (f"Could you tell us which bill{'s' if n > 1 else ''} {this_these} "
                 f"{was_were} meant for? Your open bills are {bills_txt}.")
        disputed = [inv["invoice_id"] for inv in self.story_ctx.open_invoices(cid or "")
                    if inv["disputed"]]
        if disputed:
            body += (f" One of these, {self._join_and(disputed)}, is currently "
                     "under dispute.")
        body += "\n\nThanks,\nKFG Snacks Store"
        return body

    def _story_doing_about_it(self, could_not_sort: list[dict],
                               credit_by_customer: dict[str, int],
                               certain_by_customer: dict[str, int]) -> list[dict]:
        actions: list[dict] = []
        # group the held payments into the questions we need to ask
        by_customer: dict[str, list[dict]] = {}
        suspense: list[dict] = []
        for row in could_not_sort:
            if row["code"] == "SUSPENSE":
                suspense.append(row)
            else:
                by_customer.setdefault(row["customer"], []).append(row)
        for name, rows in by_customer.items():
            amts = ", ".join(f"Rs {r['amount_paise'] // 100:,}"
                             for r in sorted(rows, key=lambda r: r["n"]))
            total = sum(r["amount_paise"] for r in rows) // 100
            if len(rows) == 1:
                text = f"Ask {name} which bill their {amts} payment is for."
            else:
                text = (f"Ask {name} which bills {len(rows)} payments are for "
                        f"- {amts} (Rs {total:,} in total).")
            cid = rows[0].get("customer_id")
            action_id = f"query-{cid or name}"
            actions.append({
                "action_id": action_id, "text": text, "done": False,
                "draft": self._story_draft_message(
                    name, rows, certain_by_customer.get(cid, 0), cid),
                "sent": action_id in self.story_actions_sent,
            })
        for row in suspense:
            actions.append({
                "action_id": f"hold-{row['payment_id']}",
                "text": (f"Hold the Rs {row['amount_paise'] // 100:,} cash deposit. "
                         "Someone needs to check the bank record for who sent it."),
                "done": False, "draft": None, "sent": False})
        for cid, amt in credit_by_customer.items():
            if amt <= 0:
                continue
            name = self._story_customers_by_id[cid]["name"]
            actions.append({
                "action_id": f"credit-{cid}",
                "text": (f"Done: the Rs {amt // 100:,} that {name} paid twice is now "
                         "credit toward their next bill."),
                "done": True, "draft": None, "sent": False})
        return actions

    def _story_sorted_out(self) -> list[dict]:
        rows = []
        for ev in self.story_events:
            bucket = self._story_bucket(ev)
            if bucket == "held":
                continue
            rows.append({
                "n": ev["n"], "amount_paise": ev["amount_paise"],
                "bills_label": self._story_bills_label(ev),
                "code": ev["final_code"],
                "code_plain": story_seed.CODE_PLAIN.get(ev["final_code"], ""),
                "how_sure": "Done automatically" if bucket == "done"
                            else "Needs a quick check",
                "sure_ok": bucket == "done",
                "real_paise": ev["real_paise"], "writeoff_paise": ev["writeoff_paise"],
                "sorted_later": ev["resolved_on_requeue"],
            })
        rows.sort(key=lambda r: -r["n"])
        return rows

    def _story_raw_statement(self) -> list[dict]:
        rows = []
        for p in self.story_scripted:
            cust = self._story_customer_name(p["virtual_account"])
            rows.append({
                "n": p["n"], "time": p["raw_time"], "amount_paise": p["amount_paise"],
                "payer": p["raw_payer"], "note": p["narration"] or "(nothing written)",
                "customer": cust or "Cash deposit",
            })
        return rows

    def story_snapshot(self) -> dict:
        setup = []
        for cid, cust in self._story_customers_by_id.items():
            inv_ids = self.story_ctx.invoice_ids_by_customer.get(cid, [])
            setup.append({
                "customer_id": cid, "name": cust["name"],
                "account_number": cust["virtual_account"],
                "invoices": [{
                    "invoice_id": iid,
                    "amount_paise": self.story_ctx.invoices[iid]["amount_paise"],
                    "balance_paise": self.story_ctx.balance[iid],
                    "disputed": bool(self.story_ctx.invoices[iid]["disputed"]),
                } for iid in inv_ids],
            })

        unmatched_by_customer: dict[str, int] = {}
        for ppid, (pp, pres) in self.story_pending.items():
            cust = self.story_ctx.customers_by_va.get(pp.get("virtual_account"))
            if cust and _code_value(pres.pending_reason) in {"AMBIG-N", "NO-MATCH"}:
                unmatched_by_customer[cust["customer_id"]] = (
                    unmatched_by_customer.get(cust["customer_id"], 0) + pp["amount_paise"])

        credit_by_customer: dict[str, int] = {}
        for row in self.story_ledger:
            if _code_value(row["reason_code"]) == "DUP-ONACC" and row["invoice_id"] is None:
                pay = next((p for p in self.story_scripted
                            if p["payment_id"] == row["payment_id"]), None)
                cust = (self.story_ctx.customers_by_va.get(pay["virtual_account"])
                        if pay else None)
                if cust:
                    credit_by_customer[cust["customer_id"]] = (
                        credit_by_customer.get(cust["customer_id"], 0) + row["amount_paise"])

        customers = []
        for cid, cust in self._story_customers_by_id.items():
            inv_ids = self.story_ctx.invoice_ids_by_customer.get(cid, [])
            open_ledger = sum(self.story_ctx.balance[i] for i in inv_ids)
            total_invoiced = sum(self.story_ctx.invoices[i]["amount_paise"] for i in inv_ids)
            r = customer_status.rollup(cid, total_invoiced, open_ledger,
                                        unmatched_by_customer.get(cid, 0))
            open_invoices = self.story_ctx.open_invoices(cid)
            bal_by_id = {i["invoice_id"]: i["balance"] for i in open_invoices}

            # BUG FIX (a): only keep a guessed slice when it finishes a bill.
            # A tiny leftover against a big bill (Rs 100 on a Rs 20,000 bill)
            # reads as a mistake -- show it as money not yet placed instead.
            best_guess = []
            not_yet_placed = 0
            if r.status == "PROVISIONAL":
                for s in l4_policy.waterfall_split(open_invoices, r.unmatched_cash_paise):
                    if s["amount_paise"] == bal_by_id.get(s["invoice_id"]):
                        best_guess.append({"invoice_id": s["invoice_id"],
                                            "amount_paise": s["amount_paise"]})
                    else:
                        not_yet_placed += s["amount_paise"]

            ending = story_seed.ENDING.get(cid, {})
            customers.append({
                "customer_id": cid, "name": cust["name"],
                "status": r.status,
                "status_plain": story_seed.STATUS_PLAIN.get(r.status, ""),
                "bills_unclear": r.status == "PROVISIONAL",
                "certain_balance_paise": r.balance_paise,
                "open_ledger_paise": open_ledger,
                "unmatched_cash_paise": unmatched_by_customer.get(cid, 0),
                "credit_held_paise": credit_by_customer.get(cid, 0),
                "best_guess": best_guess,
                "not_yet_placed_paise": not_yet_placed,
                "ending_line": ending.get("line", ""),
            })

        buckets = [self._story_bucket(ev) for ev in self.story_events]
        could_not_sort = self._story_could_not_sort()
        certain_by_customer = {c["customer_id"]: c["certain_balance_paise"] for c in customers}

        return {
            "world": "story",
            "step": self.story_step_index,
            "total_steps": len(self.story_scripted),
            "done": self.story_step_index >= len(self.story_scripted),
            "threshold": self.story_threshold,
            "code_plain": story_seed.CODE_PLAIN,
            "code_badge": story_seed.CODE_BADGE,
            "setup": setup,
            "events": self.story_events,
            "customers": customers,
            "raw_statement": self._story_raw_statement(),
            "sorted_out": self._story_sorted_out(),
            "could_not_sort": could_not_sort,
            "doing_about_it": self._story_doing_about_it(
                could_not_sort, credit_by_customer, certain_by_customer),
            "comparison": self._story_full_day,
            "counts": {
                "sorted": buckets.count("done") + buckets.count("check"),
                "could_not_sort": buckets.count("held"),
                "sorted_later": self.story_resolved_on_requeue_total,
            },
        }

    # -- Processing flow (/app) --------------------------------------------
    #
    # The real 64-payment submission dataset (engine/generate_data.py, seed
    # 42 -- the exact same world engine/run_engine.py scores), reordered so
    # 13 of its own payments run first (simulator/batch_seed.py) and given
    # /story-style narration; the remaining 51 follow in their original
    # chronological order. This is a pure reorder for display -- every
    # payment, and every reason code, is exactly what the real engine
    # already produces; see tests/test_batch_reorder.py. Same shape as the
    # Story world above: the whole run is played once here, up front, into
    # frozen frames, so navigation on the page is pure history playback.

    def _init_process_world(self) -> None:
        from engine import generate_data

        generate_data.RNG.seed(42)
        w = generate_data.build_world()
        assert len(w.payments) == 64, f"expected 64 payments, got {len(w.payments)}"

        customers_by_va = {c["virtual_account"]: c for c in w.customers}
        invoices_by_id = {i["invoice_id"]: dict(i) for i in w.invoices}
        balance = {i["invoice_id"]: i["amount_paise"] for i in w.invoices}
        ids_by_customer: dict[str, list[str]] = {}
        for inv in w.invoices:
            ids_by_customer.setdefault(inv["customer_id"], []).append(inv["invoice_id"])
        self.proc_ctx = Context(customers_by_va=customers_by_va, invoices=invoices_by_id,
                                 balance=balance, invoice_ids_by_customer=ids_by_customer)
        self.proc_customers_by_id = {c["customer_id"]: c for c in w.customers}

        chronological = sorted(w.payments, key=lambda p: (p["created_at"], p["payment_id"]))
        pay_by_id = {p["payment_id"]: p for p in w.payments}
        story_ids = set(batch_seed.STORY_SLOT_PAYMENT_IDS)
        remaining = [p for p in chronological if p["payment_id"] not in story_ids]
        ordered = [pay_by_id[pid] for pid in batch_seed.STORY_SLOT_PAYMENT_IDS] + remaining
        assert len(ordered) == 64

        self.proc_scripted = []
        for n, p in enumerate(ordered, start=1):
            narr = batch_seed.NARRATIONS.get(p["payment_id"])
            entry = {
                "n": n, "payment_id": p["payment_id"], "amount_paise": p["amount_paise"],
                "method": p["method"], "virtual_account": p.get("virtual_account"),
                "payer_name": p.get("payer_name"), "narration": p.get("narration", ""),
                "created_at": p["created_at"], "is_narrated": narr is not None,
            }
            if narr:
                entry["story_line"] = narr["story_line"]
                entry["how_it_decided"] = narr["how_it_decided"]
                entry["what_changed"] = narr["what_changed"]
            self.proc_scripted.append(entry)

        total_value_paise = sum(p["amount_paise"] for p in w.payments)
        dates = sorted(p["created_at"] for p in w.payments)
        known_customers = set()
        unmapped = 0
        for p in w.payments:
            cust = customers_by_va.get(p.get("virtual_account"))
            if cust:
                known_customers.add(cust["customer_id"])
            else:
                unmapped += 1
        self.proc_arrival = {
            "merchant_name": batch_seed.MERCHANT_NAME,
            "merchant_key": batch_seed.MERCHANT_KEY,
            "payment_count": len(w.payments),
            "total_value_paise": total_value_paise,
            "date_from": dates[0], "date_to": dates[-1],
            "customer_count": len(known_customers),
            "unmapped_count": unmapped,
            "csv_columns": ["amount_paise", "virtual_account", "payer_name", "narration", "method"],
        }

        self.proc_step_index = 0
        self.proc_ledger: list[dict] = []
        self.proc_pending: dict[str, tuple[dict, object]] = {}
        self.proc_events: list[dict] = []
        self.proc_resolved_on_requeue_total = 0
        self.proc_threshold = DEFAULT_AUTO_POST_THRESHOLD
        self.proc_actions_sent: set[str] = set()
        self.proc_history: list[dict] = [self._proc_frozen_snapshot()]
        while self.proc_step_index < len(self.proc_scripted):
            self._proc_advance()

    def _proc_frozen_snapshot(self) -> dict:
        return copy.deepcopy(self.process_snapshot())

    def process_view(self) -> dict:
        """What /app loads: the arrival-screen numbers, plus every frozen
        frame of the 64-payment run. Navigation is pure history playback,
        same contract as story_view()."""
        return {
            "arrival": self.proc_arrival,
            "step": self.proc_step_index,
            "total_steps": len(self.proc_scripted),
            "done": self.proc_step_index >= len(self.proc_scripted),
            "history": self.proc_history,
            "sent": sorted(self.proc_actions_sent),
        }

    def reset_process(self) -> dict:
        """The frozen frames never change (the run is fully deterministic),
        so 'Start over' only forgets which drafts were marked sent."""
        self.proc_actions_sent = set()
        return self.process_view()

    def send_process_action(self, action_id: str) -> dict:
        self.proc_actions_sent.add(action_id)
        return self.process_view()

    def _proc_fill_result(self, ev: dict, res) -> None:
        allocs = res.allocations
        ev["final_code"] = _code_value(allocs[0]["reason_code"])
        ev["confidence"] = min(r["confidence"] for r in allocs)
        ev["bills"] = self._story_bills(allocs)
        ev["real_paise"], ev["writeoff_paise"] = self._story_money(allocs)
        primary = next((r for r in allocs if r["invoice_id"]), None)
        if primary:
            inv = self.proc_ctx.invoices[primary["invoice_id"]]
            ev["primary_invoice_amount_paise"] = inv["amount_paise"]
            ev["invoice_balance_after_paise"] = self.proc_ctx.balance[primary["invoice_id"]]

    def _proc_advance(self) -> None:
        scripted = self.proc_scripted[self.proc_step_index]
        self.proc_step_index += 1
        pid = scripted["payment_id"]
        engine_payment = {k: scripted[k] for k in (
            "payment_id", "amount_paise", "method", "virtual_account",
            "payer_name", "narration", "created_at")}

        res = resolve_payment(engine_payment, self.proc_ctx)
        if res.resolved:
            self.proc_ledger.extend(res.allocations)
            first_code = _code_value(res.allocations[0]["reason_code"])
        else:
            self.proc_pending[pid] = (engine_payment, res)
            first_code = _code_value(res.pending_reason)

        cust = self.proc_ctx.customers_by_va.get(scripted.get("virtual_account"))
        event = {
            "n": scripted["n"], "payment_id": pid,
            "amount_paise": scripted["amount_paise"],
            "virtual_account": scripted["virtual_account"],
            "customer_name": cust["name"] if cust else None,
            "narration": scripted["narration"],
            "is_narrated": scripted["is_narrated"],
            "story_line": scripted.get("story_line"),
            "how_it_decided": scripted.get("how_it_decided"),
            "what_changed": scripted.get("what_changed"),
            "first_code": first_code, "final_code": first_code,
            "resolved_on_requeue": False, "confidence": None,
            "bills": [], "real_paise": 0, "writeoff_paise": 0,
            "primary_invoice_amount_paise": None, "invoice_balance_after_paise": None,
            "trace": res.trace,
        }
        if res.resolved:
            self._proc_fill_result(event, res)
        self.proc_events.append(event)

        resolved_on_requeue: list[str] = []
        for _ in range(MAX_REQUEUE_PASSES):
            if not self.proc_pending:
                break
            newly: list[tuple[str, object]] = []
            for ppid, (pp, _prev) in list(self.proc_pending.items()):
                pr = resolve_payment(pp, self.proc_ctx)
                if pr.resolved:
                    self.proc_ledger.extend(pr.allocations)
                    newly.append((ppid, pr))
                else:
                    self.proc_pending[ppid] = (pp, pr)
            if not newly:
                break
            for ppid, pr in newly:
                del self.proc_pending[ppid]
                if ppid != pid:
                    resolved_on_requeue.append(ppid)
                for ev in self.proc_events:
                    if ev["payment_id"] != ppid:
                        continue
                    self._proc_fill_result(ev, pr)
                    ev["resolved_on_requeue"] = ppid != pid
        self.proc_resolved_on_requeue_total += len(resolved_on_requeue)

        self.proc_history.append(self._proc_frozen_snapshot())

    def _proc_bucket_of(self, ev: dict) -> str:
        if ev["final_code"] in {"AMBIG-N", "NO-MATCH", "SUSPENSE"}:
            return "held"
        if ev["confidence"] is not None and ev["confidence"] >= self.proc_threshold:
            return "done"
        return "check"

    def _proc_bucket_entries(self) -> list[dict]:
        payment_va = {p["payment_id"]: p.get("virtual_account") for p in self.proc_scripted}
        rows_by_payment: dict[str, list[dict]] = {}
        for row in self.proc_ledger:
            code = _code_value(row["reason_code"])
            rows_by_payment.setdefault(row["payment_id"], []).append(
                {"confidence": row["confidence"], "reason_code": code,
                 "is_exception": code in EXCEPTION_CODE_VALUES})
        entries = [
            {"payment_id": pid, "resolved": not rows[0]["is_exception"],
             "confidence": min(r["confidence"] for r in rows),
             "reason_code": rows[0]["reason_code"],
             "virtual_account": payment_va.get(pid)}
            for pid, rows in rows_by_payment.items()
        ]
        entries.extend(
            {"payment_id": pid, "resolved": False, "confidence": None,
             "reason_code": _code_value(res.pending_reason),
             "virtual_account": payment.get("virtual_account")}
            for pid, (payment, res) in self.proc_pending.items()
        )
        return entries

    def _proc_threshold_curve(self) -> list[dict]:
        """auto_matched / human_touches / could_not_sort at every candidate
        dial setting, from the exact same bucket_payments() call and the
        exact same entries as the headline boxes (process_snapshot()'s
        "buckets") -- so the dial chart can never disagree with them.
        could_not_sort is flat across the curve: AMBIG-N/NO-MATCH/SUSPENSE
        are exceptions regardless of the confidence dial, only auto_matched
        and human_touches trade off against each other."""
        entries = self._proc_bucket_entries()
        curve = []
        for t in range(0, 101, 5):
            b = bucket_payments(entries, t)
            curve.append({
                "threshold": t, "auto_matched": b["auto_posted"],
                "human_touches": b["needs_confirmation"],
                "could_not_sort": b["exceptions"],
            })
        return curve

    def _proc_sorted_out(self) -> list[dict]:
        rows = []
        for ev in self.proc_events:
            bucket = self._proc_bucket_of(ev)
            if bucket == "held":
                continue
            rows.append({
                "n": ev["n"], "payment_id": ev["payment_id"],
                "amount_paise": ev["amount_paise"],
                "bills_label": self._story_bills_label(ev),
                "code": ev["final_code"],
                "code_plain": batch_seed.CODE_PLAIN.get(ev["final_code"], ""),
                "how_sure": "Done automatically" if bucket == "done" else "Needs a quick check",
                "sure_ok": bucket == "done",
                "real_paise": ev["real_paise"], "writeoff_paise": ev["writeoff_paise"],
                "sorted_later": ev["resolved_on_requeue"],
            })
        rows.sort(key=lambda r: -r["n"])
        return rows

    def _proc_could_not_sort(self) -> list[dict]:
        rows = []
        for ev in self.proc_events:
            if self._proc_bucket_of(ev) != "held":
                continue
            name = ev["customer_name"] or "Unknown sender"
            amt = ev["amount_paise"] // 100
            if ev["final_code"] == "SUSPENSE":
                reason = "No account and no name - we cannot tell who paid."
            elif ev["final_code"] == "AMBIG-N":
                possessive = name + "'" if name.endswith("s") else name + "'s"
                reason = (f"Rs {amt:,} fits more than one of {possessive} bills, and "
                          "at least one is under dispute.")
            else:  # NO-MATCH
                reason = f"Nothing {name} owes matches Rs {amt:,}."
            cust = self.proc_ctx.customers_by_va.get(ev["virtual_account"])
            rows.append({
                "n": ev["n"], "payment_id": ev["payment_id"],
                "amount_paise": ev["amount_paise"], "customer": name,
                "customer_id": cust["customer_id"] if cust else None,
                "code": ev["final_code"],
                "code_plain": batch_seed.CODE_PLAIN.get(ev["final_code"], ""),
                "reason": reason, "why": reason,
            })
        rows.sort(key=lambda r: -r["n"])
        return rows

    def _proc_unmatched_cash_for(self, cid: str) -> int:
        total = 0
        for _pid, (payment, res) in self.proc_pending.items():
            cust = self.proc_ctx.customers_by_va.get(payment.get("virtual_account"))
            if (cust and cust["customer_id"] == cid
                    and _code_value(res.pending_reason) in {"AMBIG-N", "NO-MATCH"}):
                total += payment["amount_paise"]
        return total

    def _proc_customer_rollup(self, cid: str):
        inv_ids = self.proc_ctx.invoice_ids_by_customer.get(cid, [])
        total_invoiced = sum(self.proc_ctx.invoices[i]["amount_paise"] for i in inv_ids)
        open_ledger = sum(self.proc_ctx.balance[i] for i in inv_ids)
        unmatched = self._proc_unmatched_cash_for(cid)
        return customer_status.rollup(cid, total_invoiced, open_ledger, unmatched)

    def _proc_open_bills_text(self, cid: str) -> list[str]:
        out = []
        for inv in self.proc_ctx.open_invoices(cid):
            bal, amt = inv["balance"], inv["amount_paise"]
            if bal == amt:
                out.append(f"{inv['invoice_id']} (Rs {amt // 100:,})")
            else:
                out.append(f"{inv['invoice_id']} (Rs {bal // 100:,} remaining)")
        return out

    def _proc_draft_message(self, name: str, rows: list[dict],
                             certain_balance_paise: int, cid: str | None) -> str:
        rows = sorted(rows, key=lambda r: r["n"])
        n = len(rows)
        count = {1: "one payment", 2: "two payments", 3: "three payments"}.get(n, f"{n} payments")
        subject = ("A payment we could not match to a bill" if n == 1
                   else f"{count.capitalize()} we could not match to a bill")
        amount_lines = "\n".join(f"    Rs {r['amount_paise'] // 100:,}" for r in rows)
        total = sum(r["amount_paise"] for r in rows) // 100
        open_bills = self._proc_open_bills_text(cid) if cid else []
        bills_txt = self._join_and(open_bills) if open_bills else "not on file"

        body = (
            f"To: {name}\n"
            f"Subject: {subject}\n\n"
            "Hi,\n\n"
            f"We received {count} from you that we could not match to a "
            f"bill:\n\n{amount_lines}\n\n"
        )
        if n > 1:
            body += f"That is Rs {total:,} in total. "
        body += f"Your outstanding balance is Rs {certain_balance_paise // 100:,}.\n\n"
        this_these = "these" if n > 1 else "this"
        was_were = "were" if n > 1 else "was"
        body += (f"Could you tell us which bill{'s' if n > 1 else ''} {this_these} "
                 f"{was_were} meant for? Your open bills are {bills_txt}.")
        disputed = [inv["invoice_id"] for inv in self.proc_ctx.open_invoices(cid or "")
                    if inv["disputed"]]
        if disputed:
            body += (f" One of these, {self._join_and(disputed)}, is currently "
                     "under dispute.")
        body += f"\n\nThanks,\n{batch_seed.MERCHANT_NAME}"
        return body

    def _proc_credit_by_customer(self) -> dict[str, int]:
        out: dict[str, int] = {}
        payment_va = {p["payment_id"]: p.get("virtual_account") for p in self.proc_scripted}
        for row in self.proc_ledger:
            if row["reason_code"] == ReasonCode.DUP_ONACC and row["invoice_id"] is None:
                cust = self.proc_ctx.customers_by_va.get(payment_va.get(row["payment_id"]))
                if cust:
                    out[cust["customer_id"]] = out.get(cust["customer_id"], 0) + row["amount_paise"]
        return out

    def _proc_doing_about_it(self, could_not_sort: list[dict]) -> list[dict]:
        actions: list[dict] = []
        by_customer: dict[str, list[dict]] = {}
        suspense: list[dict] = []
        for row in could_not_sort:
            if row["code"] == "SUSPENSE":
                suspense.append(row)
            else:
                by_customer.setdefault(row["customer"], []).append(row)
        for name, rows in by_customer.items():
            amts = ", ".join(f"Rs {r['amount_paise'] // 100:,}"
                             for r in sorted(rows, key=lambda r: r["n"]))
            total = sum(r["amount_paise"] for r in rows) // 100
            if len(rows) == 1:
                text = f"Ask {name} which bill their {amts} payment is for."
            else:
                text = (f"Ask {name} which bills {len(rows)} payments are for "
                        f"- {amts} (Rs {total:,} in total).")
            cid = rows[0].get("customer_id")
            action_id = f"query-{cid or name}"
            certain = self._proc_customer_rollup(cid).balance_paise if cid else 0
            actions.append({
                "action_id": action_id, "text": text, "done": False,
                "draft": self._proc_draft_message(name, rows, certain, cid),
                "sent": action_id in self.proc_actions_sent,
            })
        for row in suspense:
            actions.append({
                "action_id": f"hold-{row['payment_id']}",
                "text": (f"Hold the Rs {row['amount_paise'] // 100:,} cash deposit. "
                         "Someone needs to check the bank record for who sent it."),
                "done": False, "draft": None, "sent": False})
        for cid, amt in self._proc_credit_by_customer().items():
            if amt <= 0:
                continue
            name = self.proc_customers_by_id[cid]["name"]
            actions.append({
                "action_id": f"credit-{cid}",
                "text": (f"Done: the Rs {amt // 100:,} that {name} paid twice is now "
                         "credit toward their next bill."),
                "done": True, "draft": None, "sent": False})
        return actions

    def _proc_summary_by_code(self) -> list[dict]:
        agg: dict[str, dict] = {}
        for ev in self.proc_events:
            d = agg.setdefault(ev["final_code"], {"count": 0, "value_paise": 0})
            d["count"] += 1
            d["value_paise"] += ev["amount_paise"]
        rows = [{
            "code": code, "badge": batch_seed.CODE_BADGE.get(code, code),
            "plain": batch_seed.CODE_PLAIN.get(code, code),
            "explain": batch_seed.CODE_EXPLAIN.get(code, ""),
            "count": d["count"], "value_paise": d["value_paise"],
        } for code, d in agg.items()]
        rows.sort(key=lambda r: -r["value_paise"])
        return rows

    def _proc_setup(self) -> list[dict]:
        """"The shops - what they still owe": every customer's invoices,
        live balance included, at this exact point in the walkthrough --
        same shape as story_snapshot()'s "setup". Frozen per-frame (like
        everything else in process_snapshot()), so stepping back shows
        balances as they actually were then, not the final state."""
        setup = []
        for cid, cust in self.proc_customers_by_id.items():
            inv_ids = self.proc_ctx.invoice_ids_by_customer.get(cid, [])
            setup.append({
                "customer_id": cid, "name": cust["name"],
                "account_number": cust["virtual_account"],
                "invoices": [{
                    "invoice_id": iid,
                    "amount_paise": self.proc_ctx.invoices[iid]["amount_paise"],
                    "balance_paise": self.proc_ctx.balance[iid],
                    "disputed": bool(self.proc_ctx.invoices[iid]["disputed"]),
                } for iid in inv_ids],
            })
        return setup

    def process_snapshot(self) -> dict:
        buckets = bucket_payments(self._proc_bucket_entries(), self.proc_threshold)
        could_not_sort = self._proc_could_not_sort()
        return {
            "step": self.proc_step_index,
            "total_steps": len(self.proc_scripted),
            "done": self.proc_step_index >= len(self.proc_scripted),
            "threshold": self.proc_threshold,
            "setup": self._proc_setup(),
            "code_plain": batch_seed.CODE_PLAIN,
            "code_badge": batch_seed.CODE_BADGE,
            "code_explain": batch_seed.CODE_EXPLAIN,
            "events": self.proc_events,
            "sorted_out": self._proc_sorted_out(),
            "could_not_sort": could_not_sort,
            "doing_about_it": self._proc_doing_about_it(could_not_sort),
            "buckets": buckets,
            "threshold_curve": self._proc_threshold_curve(),
            "summary_by_code": self._proc_summary_by_code(),
            "invoice_paid_by": self._proc_invoice_paid_by(),
            "counts": {
                "sorted": buckets["auto_posted"] + buckets["needs_confirmation"],
                "could_not_sort": buckets["exceptions"],
                "sorted_later": self.proc_resolved_on_requeue_total,
            },
        }

    # -- Ask the ledger, for the /app processing flow -----------------------
    #
    # Strictly read-only, enforced by the shape of what's handed out, not
    # just by prompt wording: process_ask_context() and build_customer_
    # answer() below only ever return plain dict/list/str/int values built
    # from process_snapshot() and self.proc_scripted/proc_customers_by_id
    # (both already-immutable static data) -- never self.proc_ctx (the
    # mutable Context with the live balance dict) and never a reference to
    # any method on self. simulator/ask_ledger.answer_process() (the
    # function that actually answers a question) receives one of these
    # plain dicts as its whole argument -- it has no `self` to call back
    # into, so there is nothing in it capable of resolving a payment,
    # applying a balance, or sending anything, even in principle.

    def _proc_invoice_paid_by(self) -> dict[str, int]:
        """invoice_id -> display position (n) of the payment that resolved
        it. TOL-FEE/RESID-DED post a CASH + ADJUSTMENT row for the same
        invoice from the same payment; BULK-N posts one row per invoice
        from one payment -- either way the first ledger row seen for an
        invoice_id already names the right payment."""
        pid_to_n = {p["payment_id"]: p["n"] for p in self.proc_scripted}
        result: dict[str, int] = {}
        for row in self.proc_ledger:
            iid = row["invoice_id"]
            if iid and iid not in result:
                result[iid] = pid_to_n.get(row["payment_id"])
        return result

    # Generic suffix words several customer names share (see
    # engine/generate_data.py's SUFFIXES) -- excluded from the fuzzy
    # word-overlap match below so "which bills are open for enterprises"
    # doesn't ambiguously match whichever "... Enterprises" customer
    # happens to be found first.
    _ASK_GENERIC_NAME_WORDS = {
        "traders", "textiles", "logistics", "enterprises", "foods", "pvt",
        "ltd", "industries", "retail", "solutions", "exports",
    }

    def _proc_find_customer(self, text: str) -> dict | None:
        q = (text or "").lower()
        exact = [c for c in self.proc_customers_by_id.values() if c["name"].lower() in q]
        if exact:
            return max(exact, key=lambda c: len(c["name"]))  # prefer the more specific match

        # Fuzzy fallback: a shortened reference like "Falcon" for "Falcon
        # Pvt Ltd" -- match on whichever of the customer's own name words
        # actually appear in the question, ignoring generic suffix words
        # so it can't match on those alone.
        q_words = set(re.findall(r"[a-z]+", q))
        scored = []
        for c in self.proc_customers_by_id.values():
            name_words = re.findall(r"[a-z]+", c["name"].lower())
            overlap = [w for w in name_words
                       if w in q_words and w not in self._ASK_GENERIC_NAME_WORDS]
            if overlap:
                scored.append((len(overlap), len(c["name"]), c))
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return scored[0][2]

    def process_ask_context(self, frame: dict) -> dict:
        """Curated read-only snapshot for Ask-the-ledger questions over the
        /app processing flow -- everything a question could reasonably need
        (bills, ledger, exceptions, drafted actions), nothing that isn't
        already plain data. `frame` is one of the SAME frozen per-step
        frames the rest of the page renders from (self.proc_history[n]) --
        never a fresh, live process_snapshot() -- so an answer about step N
        can never leak state from a later payment the page hasn't shown
        yet."""
        return {
            "threshold": frame["threshold"],
            "setup": frame["setup"],
            "sorted_out": frame["sorted_out"],
            "could_not_sort": frame["could_not_sort"],
            "doing_about_it": frame["doing_about_it"],
            "buckets": frame["buckets"],
            "summary_by_code": frame["summary_by_code"],
            "events": [{k: v for k, v in ev.items() if k != "trace"} for ev in frame["events"]],
        }

    def build_customer_answer(self, name_query: str, frame: dict) -> dict | None:
        """The full, structured picture for one customer -- every bill
        named individually in one of three groups, plus totals and any
        payments of theirs we could not sort. Returns None if name_query
        doesn't resolve to a known customer. Read-only, and reads only
        `frame` (one of self.proc_history's already-frozen per-step
        snapshots) plus the static customer directory -- never
        self.proc_ctx/proc_ledger directly, so it can't answer with a
        later step's balances than the one asked about."""
        cust = self._proc_find_customer(name_query)
        if cust is None:
            return None
        cid = cust["customer_id"]
        cust_setup = next((c for c in frame["setup"] if c["customer_id"] == cid), None)
        paid_by = frame["invoice_paid_by"]

        paid_in_full, partly_paid, nothing_received = [], [], []
        total_billed = total_received = 0
        for inv in (cust_setup["invoices"] if cust_setup else []):
            billed, owed = inv["amount_paise"], inv["balance_paise"]
            received = billed - owed
            total_billed += billed
            total_received += received
            if owed == 0:
                paid_in_full.append({**inv, "received_paise": received,
                                      "paid_by_n": paid_by.get(inv["invoice_id"])})
            elif received > 0:
                partly_paid.append({**inv, "received_paise": received, "owed_paise": owed})
            else:
                nothing_received.append({**inv, "owed_paise": owed})

        could_not_sort = [r for r in frame["could_not_sort"] if r["customer_id"] == cid]
        could_not_sort_total = sum(r["amount_paise"] for r in could_not_sort)

        action = next((a for a in frame["doing_about_it"] if a["action_id"] == f"query-{cid}"), None)

        return {
            "customer_id": cid, "name": cust["name"], "account_number": cust["virtual_account"],
            "paid_in_full": paid_in_full, "partly_paid": partly_paid,
            "nothing_received": nothing_received,
            "total_billed_paise": total_billed, "total_received_paise": total_received,
            "still_owed_paise": total_billed - total_received,
            "could_not_sort": could_not_sort, "could_not_sort_total_paise": could_not_sort_total,
            "action": action,
        }

    def explain_why_open(self, name_query: str, frame: dict) -> dict | None:
        """A targeted follow-up answer ("why is that one still open?"),
        not the full picture -- used when Ask-the-ledger's caller has
        already resolved a customer from conversation history. Explains
        whichever of the customer's invoices aren't fully paid AT THIS
        FRAME; if there's exactly one, names it specifically (including
        the FIFO-TIE "identical sibling never touched" case)."""
        cust = self._proc_find_customer(name_query)
        if cust is None:
            return None
        cid = cust["customer_id"]
        cust_setup = next((c for c in frame["setup"] if c["customer_id"] == cid), None)
        if cust_setup is None:
            return None
        open_invoices = [i for i in cust_setup["invoices"] if i["balance_paise"] > 0]
        if not open_invoices:
            return {"name": cust["name"], "explanations": [],
                    "note": f"Every bill for {cust['name']} is fully paid -- nothing is still open."}

        explanations = []
        for inv in open_invoices:
            billed, owed = inv["amount_paise"], inv["balance_paise"]
            received = billed - owed
            sibling = next((i for i in cust_setup["invoices"]
                             if i["invoice_id"] != inv["invoice_id"]
                             and i["amount_paise"] == billed and i["balance_paise"] == 0), None)
            if sibling and received == 0:
                text = (f"{inv['invoice_id']} is one of two identical Rs {billed // 100:,} bills. "
                        f"The payment matched the older one, {sibling['invoice_id']}, first, so "
                        f"{inv['invoice_id']} was never touched and is still fully open.")
            elif received > 0:
                text = (f"{inv['invoice_id']} was only partly paid: Rs {received // 100:,} arrived "
                        f"against a Rs {billed // 100:,} bill, leaving Rs {owed // 100:,} still open.")
            else:
                text = f"{inv['invoice_id']} is still open -- no payment has been received against it yet."
            explanations.append({"invoice_id": inv["invoice_id"], "text": text})
        return {"name": cust["name"], "explanations": explanations, "note": None}

    _ASK_REF_WORDS = ("that one", "that", "it", "this", "they", "them", "their")

    def _proc_resolve_customer_name(self, question: str, history: list[dict]) -> str | None:
        """Resolves a customer name from the question itself, or -- for a
        follow-up like "why is that one still open?" -- from the most
        recent customer mentioned in conversation history. Read-only
        lookup only; never guesses beyond a name actually appearing
        somewhere in the text."""
        cust = self._proc_find_customer(question)
        if cust is not None:
            return cust["name"]
        ql = (question or "").lower()
        if not any(w in ql for w in self._ASK_REF_WORDS):
            return None
        for turn in reversed(history or []):
            cust = self._proc_find_customer(turn.get("text", ""))
            if cust is not None:
                return cust["name"]
        return None

    def ask_process(self, question: str, history: list[dict], step: int | None = None) -> dict:
        """Orchestrates one Ask-the-ledger turn for /app: resolves a
        customer name (possibly from history, for follow-ups), gathers
        whatever plain read-only data is relevant AT THE GIVEN STEP, and
        hands off to ask_ledger.compose_process_answer() -- the function
        that actually decides what to say, which receives only that plain
        data and never a reference to this object.

        `step` is the same view index the page's paylist/decision-trace/
        panels are currently showing (0 = opening, before any payment; 64
        = after all 64) -- the frame it reads, self.proc_history[step], is
        one of the SAME frozen snapshots those panels render from, not a
        fresh live process_snapshot(). Without this, a question asked at
        step 0 would be answered from the fully-processed end state
        instead of "nothing has happened yet" -- the exact bug this
        parameter exists to prevent. Defaults to the last frame (fully
        processed) if step isn't given.

        This method itself only ever reads (process_ask_context/
        build_customer_answer/explain_why_open are all read-only) -- it
        never calls resolve_payment, apply, or any other mutating method."""
        if step is None:
            frame = self.proc_history[-1]
        else:
            idx = max(0, min(int(step), len(self.proc_history) - 1))
            frame = self.proc_history[idx]

        context = self.process_ask_context(frame)
        resolved_name = self._proc_resolve_customer_name(question, history)
        customer_answer = self.build_customer_answer(resolved_name, frame) if resolved_name else None
        why_open = None
        ql = question.lower()
        if resolved_name and "why" in ql and any(
                w in ql for w in ("open", "owe", "unpaid", "outstanding", "stuck")):
            why_open = self.explain_why_open(resolved_name, frame)
            customer_answer = None  # a follow-up gets a targeted answer, not the full picture again
        return ask_ledger.compose_process_answer(question, context, customer_answer, why_open,
                                                  history or [])
