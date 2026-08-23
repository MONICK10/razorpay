"""Reference-token extraction and normalization. Shared by L1 (exact match)
and L3 (fuzzy repair) -- both need to find invoice-id-shaped tokens inside
free-text narration, they just differ in how forgiving the comparison is."""
from __future__ import annotations

import difflib
import re

TOKEN_RE = re.compile(r"[A-Za-z]{2,5}-?\s?O?0?\d{2,6}[A-Za-z0-9]?")


def extract_tokens(narration: str) -> list[str]:
    """Pull invoice-id-shaped substrings out of a narration string."""
    return TOKEN_RE.findall(narration or "")


def normalize(token: str) -> str:
    """Uppercase, strip non-alphanumerics, fold O -> 0 (common OCR/typo
    confusion in reference numbers)."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", token).upper()
    return cleaned.replace("O", "0")


def repair(token: str, candidate_invoice_ids: list[str]) -> tuple[str | None, float]:
    """Attempt to repair a corrupted reference token against a list of
    candidate invoice ids belonging to the payer. Returns (invoice_id, score)
    for the best unique match, or (None, 0.0) if nothing clears the bar.

    Repair strategy, cheapest and most certain first:
      1. exact match after normalization (dash/case/O-0 differences)
      2. containment after normalization (truncation, trailing garbage)
      3. fuzzy ratio (difflib), requiring a clear margin over the runner-up
    """
    if not token or not candidate_invoice_ids:
        return None, 0.0

    norm_token = normalize(token)
    norm_candidates = {inv_id: normalize(inv_id) for inv_id in candidate_invoice_ids}

    exact = [inv for inv, norm in norm_candidates.items() if norm == norm_token]
    if len(exact) == 1:
        return exact[0], 1.0
    if len(exact) > 1:
        return None, 0.0  # ambiguous, refuse to guess

    contained = []
    for inv, norm in norm_candidates.items():
        shorter, longer = sorted([norm, norm_token], key=len)
        if shorter and shorter in longer and (len(longer) - len(shorter)) <= 2:
            contained.append(inv)
    if len(contained) == 1:
        return contained[0], 0.9
    if len(contained) > 1:
        return None, 0.0

    scored = sorted(
        ((inv, difflib.SequenceMatcher(None, norm, norm_token).ratio())
         for inv, norm in norm_candidates.items()),
        key=lambda pair: pair[1], reverse=True)
    if not scored:
        return None, 0.0
    best_inv, best_score = scored[0]
    runner_score = scored[1][1] if len(scored) > 1 else 0.0
    if best_score >= 0.75 and (best_score - runner_score) >= 0.1:
        return best_inv, best_score
    return None, 0.0


def find_reference_candidates(narration: str, invoice_ids: list[str]) -> list[str]:
    """Return every invoice_id from `invoice_ids` that appears verbatim in
    narration, as a whole token (used for exact single- and multi-reference
    matching). Boundary-checked so a corrupted reference like "INV-0035X"
    does NOT count as an exact hit on "INV-0035" just because it's a
    prefix -- that case belongs to L3 fuzzy repair, not L1 exact match."""
    text = narration or ""
    found = []
    for inv_id in invoice_ids:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(inv_id) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text):
            found.append(inv_id)
    return found
