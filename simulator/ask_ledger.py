"""Two strictly read-only, LLM-backed simulator features:

  answer(question, state)    -- Section C, "Ask the ledger": free-text
    questions about current simulator state, answered from a structured
    JSON snapshot.
  explain_refusal(ctx)       -- Section D, "Explain this refusal": one
    plain-English paragraph on why a specific exception couldn't be
    resolved, given its decision trace and candidate invoices.

Same two-backend contract as engine/l3_ai.py and engine/l6_action.py: a
real Claude call when engine.ai_client.get_client() returns a client, a
deterministic template otherwise (including on any API error).

Read-only is enforced in code, not just the prompt: both functions only
ever read from a SessionState snapshot (or a dict already derived from
one) and return plain text. Neither function holds a reference to any
mutating method on SessionState (submit_payment / apply_dial /
send_action / _apply_credit) -- there is nothing for a model response to
call even if it tried, since no tool-calling is wired up here. The model
only ever produces a string that gets handed back to the client.
"""
from __future__ import annotations

import json
import logging
import re

from engine.ai_client import get_client

logger = logging.getLogger(__name__)

_DIAL_MENTION_RE = re.compile(r"\bdial\b|\bthreshold\b", re.I)
_NUMBER_RE = re.compile(r"\b(\d{1,3})\b")


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def _detect_dial_whatif(question: str) -> int | None:
    """Pulls a candidate confidence-dial value (0-100) out of a question
    like "what would change if I set the dial to 95?" -- only when the
    question actually mentions the dial/threshold, so an unrelated number
    ("2 payments") doesn't get misread as a dial value."""
    if not _DIAL_MENTION_RE.search(question):
        return None
    for m in _NUMBER_RE.finditer(question):
        n = int(m.group(1))
        if 0 <= n <= 100:
            return n
    return None


def _find_customer(question: str, context: dict) -> dict | None:
    q = question.lower()
    matches = [c for c in context["customers"] if c["name"].lower() in q]
    if not matches:
        return None
    return max(matches, key=lambda c: len(c["name"]))  # prefer the more specific match


def _build_context(state) -> dict:
    """A curated (not raw) read of state.snapshot(): the fields relevant to
    answering questions about current state, never anything that would let
    a caller reconstruct a mutating call."""
    snap = state.snapshot()
    return {
        "threshold": snap["kpis"]["threshold"],
        "customers": snap["customers"],
        "invoices": snap["invoices"],
        "ledger": snap["ledger"],
        "exception_queue": snap["exception_queue"],
        "actions": snap["actions"],
        "kpis": snap["kpis"],
    }


# -- Section C: Ask the ledger ----------------------------------------------

def answer(question: str, state) -> dict:
    """Returns {"answer": str, "ai_used": bool}."""
    context = _build_context(state)
    dial_n = _detect_dial_whatif(question)
    if dial_n is not None:
        context["hypothetical_dial"] = state.dial_whatif(dial_n)

    client = get_client()
    if client is None:
        return {"answer": _stub_answer(question, context), "ai_used": False}
    try:
        prompt = _build_ask_prompt(question, context)
        return {"answer": client.complete(prompt, max_tokens=500), "ai_used": True}
    except Exception as exc:
        logger.warning("ask_ledger.answer failed (%s), falling back", exc)
        return {"answer": _stub_answer(question, context), "ai_used": False}


def _build_ask_prompt(question: str, context: dict) -> str:
    return (
        "You are a strictly read-only assistant for a payment reconciliation "
        "ledger. You can only describe and summarize the data given to you "
        "below -- you cannot allocate a payment, post an entry, modify a "
        "balance, change the confidence dial, or take any other action. If "
        "asked to do something rather than answer a question, say plainly "
        "that you can only answer questions about current state.\n\n"
        "Current ledger state, as JSON:\n"
        f"{json.dumps(context, indent=2, default=str)}\n\n"
        f"Question: {question}\n\n"
        "Answer in plain English, using ONLY the data above. If the data "
        "doesn't let you answer confidently, say so explicitly rather than "
        "guessing. Keep it under 150 words.")


def _stub_answer(question: str, context: dict) -> str:
    q = question.lower()

    if "provisional" in q:
        cust = _find_customer(question, context)
        if cust is None:
            provisional = [c for c in context["customers"] if c["status"] == "PROVISIONAL"]
            if not provisional:
                return "No customer is currently PROVISIONAL."
            names = ", ".join(c["name"] for c in provisional)
            return ("I couldn't match a specific customer name in your question. "
                    f"Customers currently PROVISIONAL: {names}.")
        if cust["status"] != "PROVISIONAL":
            return f"{cust['name']} is not PROVISIONAL -- their status is {cust['status']}."
        unmatched = cust["total_outstanding_paise"] - cust["certain_balance_paise"]
        return (f"{cust['name']} is PROVISIONAL: their open balance is "
                f"{_rupees(cust['total_outstanding_paise'])}, of which "
                f"{_rupees(unmatched)} is cash we've received from them but haven't "
                "yet tied to a specific invoice. The certain amount they still owe "
                f"is {_rupees(cust['certain_balance_paise'])}.")

    if "owe" in q and ("most" in q or "highest" in q or "largest" in q):
        if not context["customers"]:
            return "There are no customers in the current ledger."
        top = max(context["customers"], key=lambda c: c["certain_balance_paise"])
        if top["certain_balance_paise"] == 0:
            return "No customer currently owes anything -- every balance is clear."
        return (f"{top['name']} owes the most: {_rupees(top['certain_balance_paise'])} "
                f"(status: {top['status']}).")

    if "refuse" in q or "couldn't" in q or "could not" in q or "why" in q and "exception" in q:
        if not context["exception_queue"]:
            return "Nothing is currently in the exception queue -- every payment resolved."
        rows = context["exception_queue"][:5]
        lines = [f"{r['payment_id']} ({_rupees(r['amount_paise'])}, {r['reason_code']}): "
                 f"{r['rationale']}" for r in rows]
        more = (f" ...and {len(context['exception_queue']) - 5} more."
                if len(context["exception_queue"]) > 5 else "")
        return "Payments we refused to auto-match, and why:\n" + "\n".join(lines) + more

    if "stuck" in q or ("exception" in q and ("value" in q or "how much" in q)):
        total = sum(r["amount_paise"] for r in context["exception_queue"])
        return (f"{_rupees(total)} is currently stuck in the exception queue across "
                f"{len(context['exception_queue'])} payment(s).")

    if "hypothetical_dial" in context:
        h = context["hypothetical_dial"]
        now, cand = h["buckets_now"], h["buckets_at_candidate"]
        delta = cand["auto_posted"] - now["auto_posted"]
        direction = "gain" if delta > 0 else "lose" if delta < 0 else "not change"
        return (f"Moving the dial from {h['current_threshold']} to "
                f"{h['candidate_threshold']} would {direction} "
                f"{abs(delta)} auto-posted payment(s) ({now['auto_posted']} -> "
                f"{cand['auto_posted']}), moving {len(h['moved_payment_ids'])} "
                "payment(s) between auto-posted and needs-confirmation. The "
                "exception queue itself is unaffected -- the dial only changes "
                "which already-resolved payments need a human glance.")

    return ("I can't answer that from the current ledger data. Try asking about a "
            "specific customer's status, who owes the most, what's in the exception "
            "queue, how much value is stuck in exceptions, or what a different "
            "confidence dial setting would change.")


# -- Ask the ledger, for /app's processing flow ------------------------------
#
# compose_process_answer() is the function that actually decides what to
# say -- including the one LLM call in this section. It takes only plain
# data (dicts/lists/str built by simulator.state.SessionState.ask_process())
# and holds no reference to SessionState itself, so nothing here can
# allocate a payment, post an entry, change a balance, or send anything,
# even in principle. simulator.state.ask_process() does the read-only
# lookups (resolving a customer name, pulling their live bill balances)
# and hands the results in as plain data; this module never reads
# anything mutable directly.

EXCEPTION_CODES = {"AMBIG-N", "NO-MATCH", "SUSPENSE"}
_PAYMENT_N_RE = re.compile(r"#?\s*(\d{1,2})\b")


def _in_rupees(paise: int) -> str:
    """Rs with Indian digit grouping (1,23,456), matching this app's
    hand-written narration everywhere else (e.g. simulator/batch_seed.py)."""
    rupees = paise // 100
    sign = "-" if rupees < 0 else ""
    s = str(abs(rupees))
    if len(s) > 3:
        last3, rest = s[-3:], s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups) + "," + last3
    return f"Rs {sign}{s}"


def _find_payment_n(question: str) -> int | None:
    if "payment" not in question.lower() and "#" not in question:
        return None
    for m in _PAYMENT_N_RE.finditer(question):
        n = int(m.group(1))
        if 1 <= n <= 64:
            return n
    return None


def _format_customer_answer(ca: dict) -> str:
    """The strict, always-every-bill-named format Section 4 requires --
    never collapsed into a summary phrase like "some bills still open"."""
    lines = [f"{ca['name']} - account {ca['account_number']}", ""]
    if ca["paid_in_full"]:
        lines.append("Paid in full:")
        for inv in ca["paid_in_full"]:
            by = f" paid by payment #{inv['paid_by_n']}" if inv.get("paid_by_n") else ""
            lines.append(f"  {inv['invoice_id']}  {_in_rupees(inv['amount_paise'])}{by}")
        lines.append("")
    if ca["partly_paid"]:
        lines.append("Partly paid:")
        for inv in ca["partly_paid"]:
            lines.append(f"  {inv['invoice_id']}  {_in_rupees(inv['amount_paise'])} billed, "
                         f"{_in_rupees(inv['received_paise'])} received, "
                         f"{_in_rupees(inv['owed_paise'])} still open")
        lines.append("")
    if ca["nothing_received"]:
        lines.append("Nothing received yet:")
        for inv in ca["nothing_received"]:
            lines.append(f"  {inv['invoice_id']}  {_in_rupees(inv['amount_paise'])}")
        lines.append("")
    lines.append(f"Total billed:    {_in_rupees(ca['total_billed_paise'])}")
    lines.append(f"Total received:  {_in_rupees(ca['total_received_paise'])}")
    lines.append(f"Still owed:      {_in_rupees(ca['still_owed_paise'])}")
    if ca["could_not_sort"]:
        lines.append("")
        lines.append("Payments we could not sort:")
        for r in ca["could_not_sort"]:
            lines.append(f"  #{r['n']}  {_in_rupees(r['amount_paise'])}  {r['reason']}")
        lines.append(f"  -> {_in_rupees(ca['could_not_sort_total_paise'])} waiting on "
                     f"{ca['name']} to say which bills these were for")
    return "\n".join(lines)


def _format_why_open(why: dict) -> str:
    if why["note"]:
        return why["note"]
    return "\n".join(e["text"] for e in why["explanations"])


def _stub_process_answer(question: str, context: dict) -> str:
    q = question.lower()

    n = _find_payment_n(question)
    if n is not None:
        held = next((r for r in context["could_not_sort"] if r["n"] == n), None)
        if held:
            return (f"Payment #{n} ({_in_rupees(held['amount_paise'])}, {held['code']}) was held, "
                    f"not resolved: {held['reason']}")
        row = next((r for r in context["sorted_out"] if r["n"] == n), None)
        if row:
            return (f"Payment #{n} was not refused -- it resolved as {row['code']} "
                    f"({row['code_plain']}), {row['how_sure'].lower()}.")
        return f"I don't have a payment #{n} in this batch (it only has 64 payments)."

    if "owe" in q and ("most" in q or "highest" in q or "largest" in q):
        candidates = [(c["name"], sum(i["balance_paise"] for i in c["invoices"]))
                      for c in context["setup"]]
        candidates = [c for c in candidates if c[1] > 0]
        if not candidates:
            return "No customer currently owes anything -- every bill is fully paid."
        name, owed = max(candidates, key=lambda c: c[1])
        return f"{name} owes the most: {_in_rupees(owed)} still outstanding."

    if "stuck" in q or ("how much" in q and ("sort" in q or "exception" in q or "held" in q)):
        by_code = [r for r in context["summary_by_code"] if r["code"] in EXCEPTION_CODES]
        if not by_code:
            return "Nothing is stuck -- every payment sorted."
        total = sum(r["value_paise"] for r in by_code)
        lines = [f"{_in_rupees(total)} is stuck across "
                 f"{sum(r['count'] for r in by_code)} payment(s):"]
        for r in sorted(by_code, key=lambda r: -r["value_paise"]):
            lines.append(f"  {r['code']} ({r['count']}, {_in_rupees(r['value_paise'])}): {r['explain']}")
        return "\n".join(lines)

    return ("I can't answer that from the current ledger data. Try asking about a specific "
            "customer by name, who owes the most, how much is stuck and why, or why a "
            "specific payment number was held.")


def _build_process_prompt(question: str, context: dict, history: list[dict]) -> str:
    history_text = "\n".join(
        f"{h.get('role', 'user')}: {h.get('text', '')}" for h in history[-6:]) or "(none yet)"
    return (
        "You are a strictly read-only assistant for a payment reconciliation "
        "ledger (KFG Snacks Store's 64-payment batch). You can only describe "
        "and summarize the data given below -- you cannot allocate a payment, "
        "post an entry, change a balance, or take any other action. If asked "
        "to do something rather than answer a question, say plainly that you "
        "can only answer questions about current state.\n\n"
        "Recent conversation:\n" + history_text + "\n\n"
        "Current ledger state, as JSON:\n"
        f"{json.dumps(context, indent=2, default=str)}\n\n"
        f"Question: {question}\n\n"
        "Answer in plain English, using ONLY the data above. If asked about a "
        "specific customer, name every one of their bills individually in "
        "three groups -- paid in full, partly paid, nothing received yet -- "
        "never a vague summary phrase. If the data doesn't let you answer "
        "confidently, say so explicitly rather than guessing. Keep it under "
        "200 words.")


def compose_process_answer(question: str, context: dict, customer_answer: dict | None,
                            why_open: dict | None, history: list[dict]) -> dict:
    """Returns {"answer": str, "ai_used": bool, "customer": dict | None}.
    See the module-level note above this section for the read-only
    guarantee: every parameter here is already plain data, and this
    function never receives (or needs) a reference to SessionState."""
    if why_open is not None:
        return {"answer": _format_why_open(why_open), "ai_used": False, "customer": None}
    if customer_answer is not None:
        return {"answer": _format_customer_answer(customer_answer), "ai_used": False,
                "customer": customer_answer}

    client = get_client()
    if client is None:
        return {"answer": _stub_process_answer(question, context), "ai_used": False, "customer": None}
    try:
        prompt = _build_process_prompt(question, context, history)
        return {"answer": client.complete(prompt, max_tokens=500), "ai_used": True, "customer": None}
    except Exception as exc:
        logger.warning("ask_ledger.compose_process_answer failed (%s), falling back", exc)
        return {"answer": _stub_process_answer(question, context), "ai_used": False, "customer": None}


# -- Section D: Explain this refusal -----------------------------------------

def explain_refusal(ctx: dict) -> dict:
    """ctx is simulator.state.SessionState.explain_context(payment_id)'s
    return value. Returns {"explanation": str, "ai_used": bool}."""
    client = get_client()
    if client is None:
        return {"explanation": _stub_explain(ctx), "ai_used": False}
    try:
        prompt = _build_explain_prompt(ctx)
        return {"explanation": client.complete(prompt, max_tokens=400), "ai_used": True}
    except Exception as exc:
        logger.warning("ask_ledger.explain_refusal failed (%s), falling back", exc)
        return {"explanation": _stub_explain(ctx), "ai_used": False}


def _build_explain_prompt(ctx: dict) -> str:
    payment = ctx["payment"]
    trace_lines = "\n".join(
        f"- {s['layer']}: {s['outcome']} -- {s['note']}" for s in ctx["trace"]) or "(none)"
    candidate_lines = "\n".join(
        f"- {c['invoice_id']}: amount={c['amount_paise']}p, balance={c['balance']}p, "
        f"issue_date={c['issue_date']}, due_date={c['due_date']}, disputed={c['disputed']}"
        for c in ctx["candidates"]) or "(none -- no customer was identified for this payment)"
    return (
        "A payment reconciliation engine already made a final decision NOT to "
        "automatically match a payment. You are explaining that decision after "
        "the fact, in plain English -- you are not deciding anything yourself "
        "and cannot change the outcome.\n\n"
        f"Payment: {payment['amount_paise']} paise, narration "
        f"\"{payment.get('narration') or '(none)'}\", customer: "
        f"{ctx['customer_name'] or 'unidentified'}.\n"
        f"Final code: {ctx['reason_code']}. Rationale: {ctx['rationale']}.\n\n"
        "Decision trace:\n" + trace_lines + "\n\n"
        "Candidate invoices considered:\n" + candidate_lines + "\n\n"
        "Write ONE paragraph, under 120 words, covering: what we know for "
        "certain, what we could not determine, and what would resolve it. "
        "Plain text only, no headers.")


def _stub_explain(ctx: dict) -> str:
    payment = ctx["payment"]
    who = ctx["customer_name"] or "the sender"
    known = f"We know {who} sent {_rupees(payment['amount_paise'])}."
    if ctx["reason_code"] == "SUSPENSE":
        return (f"{known} We could not determine who this payment is from -- the "
                "account it arrived from doesn't map to any known customer, so "
                "there were no candidate invoices to consider. This would be "
                "resolved by identifying the payer, for example by matching the "
                "bank account or asking them directly.")
    candidates = ctx["candidates"]
    if candidates:
        cand_list = ", ".join(c["invoice_id"] for c in candidates)
        return (f"{known} We could not confidently determine which invoice it "
                f"settles: {ctx['rationale']} The candidate invoice(s) considered "
                f"were {cand_list}. This would be resolved by a clearer reference "
                f"in the payment narration, or by asking {who} directly which "
                "invoice this payment was intended for.")
    return (f"{known} {who} has no open invoices this payment could plausibly "
            f"settle: {ctx['rationale']} This would be resolved by confirming "
            f"with {who} what this payment was for, or by issuing/locating the "
            "invoice it's meant to cover.")
