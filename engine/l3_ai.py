"""L3 -- AI. The only layer allowed to touch the tail: garbled references
and shortlist ambiguity that survived L0-L2 untouched. It gets a shortlist
it must choose from and is explicitly allowed to say "I don't know."

Confidence is never taken from the model. Whatever this layer returns, the
caller (pipeline.py) recomputes confidence itself from countable signals --
see confidence.py.

Two backends:
  - real: calls the Claude API (claude-opus-5) for strict-JSON decisions,
    used when ANTHROPIC_API_KEY (or another SDK-recognized credential) is
    available.
  - stub: a deterministic fallback -- the same normalization/containment/
    ratio heuristic in reference.py for repair, and a conservative
    "never override a dispute" rule for shortlist choice.

The stub is not a lesser demo mode bolted on for when there's no key: it is
what "never guess" looks like when there is no model to ask, and the real
backend falls back to it on any API error so a transient outage degrades
the pipeline's confidence, not its correctness.
"""
from __future__ import annotations

import logging

from engine import reference
from engine.ai_client import get_client as _get_client

logger = logging.getLogger(__name__)


class ReferenceRepair:
    def __init__(self, invoice_id: str | None, rationale: str, ai_used: bool):
        self.invoice_id = invoice_id
        self.rationale = rationale
        self.ai_used = ai_used


class ShortlistChoice:
    def __init__(self, invoice_id: str | None, rationale: str, ai_used: bool):
        self.invoice_id = invoice_id
        self.rationale = rationale
        self.ai_used = ai_used


def repair_reference(token: str, candidates: list[dict]) -> ReferenceRepair:
    """Try to repair a garbled reference token against the payer's open
    invoices. `candidates` is a list of {invoice_id, amount_paise,
    issue_date} dicts."""
    client = _get_client()
    if client is None or len(candidates) == 0:
        inv_id, score = reference.repair(token, [c["invoice_id"] for c in candidates])
        rationale = (f"stub: normalized/fuzzy match, score={score:.2f}"
                     if inv_id else "stub: no candidate cleared the match bar")
        return ReferenceRepair(inv_id, rationale, ai_used=False)

    try:
        from pydantic import BaseModel

        class Result(BaseModel):
            matched_invoice_id: str | None
            rationale: str

        candidate_lines = "\n".join(
            f"- {c['invoice_id']}: amount_paise={c['amount_paise']}, "
            f"issue_date={c['issue_date']}"
            for c in candidates)
        prompt = (
            "A bank payment narration contains a possibly garbled invoice "
            f"reference: \"{token}\".\n\n"
            "Candidate open invoices for this same payer:\n"
            f"{candidate_lines}\n\n"
            "Decide which invoice (if any) this reference most likely refers "
            "to -- typos, truncation, dropped separators, and case changes "
            "are common. If no candidate is a confident, unambiguous match, "
            "set matched_invoice_id to null rather than guessing.")

        response = client.messages.parse(
            model="claude-opus-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=Result,
        )
        result = response.parsed_output
        valid_ids = {c["invoice_id"] for c in candidates}
        matched = result.matched_invoice_id
        if matched is not None and matched not in valid_ids:
            matched = None  # model hallucinated an id outside the shortlist
        return ReferenceRepair(matched, result.rationale, ai_used=True)
    except Exception as exc:
        logger.warning("L3 AI repair_reference call failed (%s), falling back", exc)
        inv_id, score = reference.repair(token, [c["invoice_id"] for c in candidates])
        rationale = f"fallback after AI error: score={score:.2f}"
        return ReferenceRepair(inv_id, rationale, ai_used=False)


def choose_candidate(payment: dict, candidates: list[dict]) -> ShortlistChoice:
    """Ask the AI to choose among a shortlist of financially-plausible
    invoices for a payment with no usable reference. `candidates` is a list
    of {invoice_id, amount_paise, issue_date, due_date, disputed} dicts.
    Returns invoice_id=None when the model (or the stub) declines to pick."""
    client = _get_client()
    if client is None or len(candidates) == 0:
        return _stub_choose(candidates)

    try:
        from pydantic import BaseModel

        class Result(BaseModel):
            chosen_invoice_id: str | None
            rationale: str

        candidate_lines = "\n".join(
            f"- {c['invoice_id']}: amount_paise={c['amount_paise']}, "
            f"issue_date={c['issue_date']}, due_date={c['due_date']}, "
            f"disputed={c['disputed']}"
            for c in candidates)
        prompt = (
            f"A payment of {payment['amount_paise']} paise arrived from a "
            "known customer with no usable invoice reference in the "
            f"narration (\"{payment.get('narration', '')}\"). "
            "Candidate open invoices for this customer:\n"
            f"{candidate_lines}\n\n"
            "Pick the single invoice this payment most likely settles. "
            "Never pick a disputed invoice -- a dispute means the amount "
            "itself is contested and cannot be silently allocated. "
            "If the candidates are financially indistinguishable and none "
            "is disputed, prefer the oldest issue_date (FIFO). "
            "If you cannot decide with confidence -- for example because a "
            "disputed invoice is tied with a clean one -- set "
            "chosen_invoice_id to null rather than guessing.")

        response = client.messages.parse(
            model="claude-opus-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            output_format=Result,
        )
        result = response.parsed_output
        valid_ids = {c["invoice_id"] for c in candidates}
        chosen = result.chosen_invoice_id
        if chosen is not None and chosen not in valid_ids:
            chosen = None
        return ShortlistChoice(chosen, result.rationale, ai_used=True)
    except Exception as exc:
        logger.warning("L3 AI choose_candidate call failed (%s), falling back", exc)
        return _stub_choose(candidates)


def _stub_choose(candidates: list[dict]) -> ShortlistChoice:
    if any(c["disputed"] for c in candidates):
        return ShortlistChoice(
            None, "stub: a disputed candidate is tied with others, refusing to guess",
            ai_used=False)
    oldest = min(candidates, key=lambda c: c["issue_date"])
    return ShortlistChoice(
        oldest["invoice_id"], "stub: candidates financially identical, FIFO",
        ai_used=False)
