"""Confidence is computed by us from countable signals -- never taken from
the model's own self-report. See PRD section 6."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConfidenceSignals:
    reference_exact: bool = False
    reference_repaired: bool = False
    amount_exact: bool = False
    amount_in_tolerance: bool = False
    candidate_count: int = 0
    payer_name_match: bool = False

    def score(self) -> int:
        points = 0
        if self.reference_exact:
            points += 50
        elif self.reference_repaired:
            points += 30

        if self.amount_exact:
            points += 30
        elif self.amount_in_tolerance:
            points += 15

        if self.candidate_count == 1:
            points += 20
        elif self.candidate_count == 2:
            points += 0
        elif self.candidate_count >= 3:
            points -= 20

        if self.payer_name_match:
            points += 10

        # The point signals above sum to a max of 110 and a min of -20 --
        # clamp to the 0-100 scale the auto-post dial and every downstream
        # comparison (scoring, the simulator's live ledger, the threshold
        # curve) actually operate on.
        return max(0, min(100, points))


DEFAULT_AUTO_POST_THRESHOLD = 85
