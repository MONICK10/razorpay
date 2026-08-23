"""L2 -- Tolerance. A shortfall within tolerance auto-clears the invoice
and the difference posts to bank charges. Outside tolerance, the shortfall
either keeps the invoice open (more expected) or -- when the narration says
so -- is a deduction to be tracked separately."""
from __future__ import annotations

import re

FIXED_TOLERANCE_PAISE = 10_000  # Rs 100
PCT_TOLERANCE = 0.005  # 0.5%

DEDUCTION_KEYWORDS = re.compile(
    r"\b(TDS|DEDUCTION|NET OF|WITHHOLDING|WHT)\b", re.IGNORECASE)


def tolerance_paise(reference_amount_paise: int) -> int:
    return max(FIXED_TOLERANCE_PAISE, round(reference_amount_paise * PCT_TOLERANCE))


def within_tolerance(gap_paise: int, reference_amount_paise: int) -> bool:
    return 0 <= gap_paise <= tolerance_paise(reference_amount_paise)


def looks_like_deduction(narration: str) -> bool:
    return bool(DEDUCTION_KEYWORDS.search(narration or ""))
