"""Side-by-side comparison of the submission run against the held-out
run -- the direct answer to "100% on your own data, what happens on data
you didn't design for?"

Reads out/results.json and out/results_holdout.json; does not regenerate
or re-run anything. Produces both first:

    python -m engine.generate_data && python -m engine.run_engine && python -m scoring.score
    python -m engine.generate_data --mode holdout && python -m engine.run_engine --suffix _holdout && python -m scoring.score --suffix _holdout
    python -m scoring.compare
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "out"


def load(suffix: str) -> dict:
    path = OUT_DIR / f"results{suffix}.json"
    if not path.exists():
        print(f"missing {path} -- run the {'holdout' if suffix else 'submission'} "
              "pipeline first (see this module's docstring)", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def main() -> None:
    submission = load("")
    holdout = load("_holdout")

    rows = [
        ("Match rate", pct(submission["overall_match_rate"]), pct(holdout["overall_match_rate"])),
        ("Precision", pct(submission["precision"]), pct(holdout["precision"])),
        ("Recall", pct(submission["recall"]), pct(holdout["recall"])),
        ("F1", pct(submission["f1"]), pct(holdout["f1"])),
        ("True positives", str(submission["true_positives"]), str(holdout["true_positives"])),
        ("False positives", str(submission["false_positives"]), str(holdout["false_positives"])),
        ("False negatives", str(submission["false_negatives"]), str(holdout["false_negatives"])),
        ("Auto-resolution rate", pct(submission["auto_resolution_rate"]), pct(holdout["auto_resolution_rate"])),
        ("Precision @ default threshold", pct(submission["precision_at_default_threshold"]),
         pct(holdout["precision_at_default_threshold"])),
        ("Human touches", str(submission["human_touches"]), str(holdout["human_touches"])),
        ("Customer balance accuracy", pct(submission["customer_status"]["balance_accuracy"]),
         pct(holdout["customer_status"]["balance_accuracy"])),
    ]

    label_w = max(len(r[0]) for r in rows)
    print(f"{'':{label_w}}  {'SUBMISSION':>12}  {'HOLDOUT':>12}")
    for label, sub, hold in rows:
        print(f"{label:{label_w}}  {sub:>12}  {hold:>12}")

    print()
    if holdout["overall_match_rate"] >= submission["overall_match_rate"]:
        print("WARNING: holdout match rate is not lower than the submission's -- "
              "the holdout dataset may not be exercising anything the engine "
              "wasn't already built for.")
    else:
        drop = (submission["overall_match_rate"] - holdout["overall_match_rate"]) * 100
        print(f"Holdout match rate is {drop:.1f} points lower than submission -- "
              "expected: the holdout tests inputs the spec never enumerated.")

    if holdout["mismatches"]:
        print(f"\n{len(holdout['mismatches'])} holdout mismatch(es), explained:")
        for m in holdout["mismatches"]:
            print(f"  {m['payment_id']}: expected {m['expected_reason_code']}, "
                  f"got {m['actual_reason_code']}")
            print(f"    {m['notes']}")


if __name__ == "__main__":
    main()
