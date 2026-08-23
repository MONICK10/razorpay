"""Regression guard: regenerate the submission dataset, run the full
engine, score it, and check the numbers PRD section 2/4 commit to."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def run_module(module: str, *args: str) -> None:
    result = subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT,
                             capture_output=True, text=True)
    assert result.returncode == 0, f"{module} failed:\n{result.stdout}\n{result.stderr}"


def test_full_pipeline_hits_expected_scale_and_match_rate():
    run_module("engine.generate_data")
    run_module("engine.run_engine")
    run_module("scoring.score")

    import csv
    import json
    from collections import Counter

    with open(ROOT / "out" / "ground_truth.csv", newline="") as f:
        gt_rows = list(csv.DictReader(f))
    assert len(gt_rows) == 64
    codes = {r["expected_reason_code"] for r in gt_rows}
    # 11 payment-level codes. PROVISIONAL is deliberately not one of them --
    # it's a customer_status, not a payment outcome (see
    # engine/customer_status.py and ground_truth_customers.csv).
    assert codes == {"EXACT", "TOL-FEE", "PART-EXP", "RESID-DED", "BULK-N",
                      "REF-FUZZY", "FIFO-TIE", "DUP-ONACC", "SUSPENSE",
                      "AMBIG-N", "NO-MATCH"}

    with open(ROOT / "out" / "ground_truth_customers.csv", newline="") as f:
        gt_customer_rows = list(csv.DictReader(f))
    customer_statuses = {r["expected_customer_status"] for r in gt_customer_rows}
    assert customer_statuses == {"CLEAN", "PARTIAL", "PROVISIONAL"}

    # Distribution: fewer, denser customers (the panel-review fix for the
    # 45-customers/55-invoices ≈ 1.2-invoices-each dataset that made every
    # match trivial) -- see engine/generate_data.py's shared customer pool.
    import sqlite3
    conn = sqlite3.connect(ROOT / "out" / "finance.db")
    invoice_customers = [r[0] for r in conn.execute("SELECT customer_id FROM invoices")]
    conn.close()
    dense = sum(1 for count in Counter(invoice_customers).values() if count >= 3)
    assert dense >= 6, f"expected at least 6 customers with 3+ invoices, got {dense}"

    with open(ROOT / "out" / "results.json") as f:
        results = json.load(f)

    assert results["totals"]["payments"] == 64
    assert results["overall_match_rate"] >= 0.95, (
        f"match rate regressed: {results['overall_match_rate']}, "
        f"mismatches: {results['mismatches']}")
    assert 0 < results["auto_resolution_rate"] < 1
    assert results["human_touches"] <= results["raw_exception_count"]

    # Re-queue proven in the actual submission run, not just the standalone
    # scenario in tests/test_requeue.py -- see the resolution_group:A/B
    # payments planted in engine/generate_data.py.
    assert results["resolved_on_requeue_count"] >= 2, (
        "expected the two planted resolution_group sequences to resolve "
        f"via re-queue, got {results['resolved_on_requeue_count']}")

    cs = results["customer_status"]
    assert cs["balance_accuracy"] == 1.0, (
        f"customer balance accuracy regressed: {cs['mismatches']}")
    assert cs["balance_accuracy_by_status"]["PROVISIONAL"] == 1.0, (
        "PROVISIONAL customers should have a 100% provable balance even "
        "though their individual payments are, correctly, NO-MATCH")


def test_holdout_scores_lower_than_submission():
    """The whole point of the holdout dataset: it's messy, adversarial
    input the 12-code spec never enumerated, so the match rate must be
    meaningfully below the submission run's -- if it isn't, the holdout
    isn't testing anything the engine wasn't already built for."""
    run_module("engine.generate_data")
    run_module("engine.run_engine")
    run_module("scoring.score")
    run_module("engine.generate_data", "--mode", "holdout")
    run_module("engine.run_engine", "--suffix", "_holdout")
    run_module("scoring.score", "--suffix", "_holdout")

    import json

    with open(ROOT / "out" / "results.json") as f:
        submission = json.load(f)
    with open(ROOT / "out" / "results_holdout.json") as f:
        holdout = json.load(f)

    assert holdout["overall_match_rate"] < submission["overall_match_rate"], (
        f"holdout match rate ({holdout['overall_match_rate']}) should be "
        f"lower than the submission's ({submission['overall_match_rate']})")
    assert holdout["overall_match_rate"] >= 0.5, (
        f"holdout match rate regressed too far: {holdout['overall_match_rate']}, "
        f"mismatches: {holdout['mismatches']}")
