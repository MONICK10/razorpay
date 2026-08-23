"""Phase 2/3 entry point: run the deterministic engine (+ AI layer) against
out/finance.db and write allocations.

Run: python -m engine.run_engine
     python -m engine.run_engine --suffix _holdout
"""
from __future__ import annotations

from pathlib import Path

from engine.db import connect
from engine.pipeline import load_context, persist_allocations, run

OUT_DIR = Path(__file__).parent.parent / "out"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="",
                         help="e.g. _holdout -- reads/writes finance_holdout.db")
    args = parser.parse_args()

    db_path = OUT_DIR / f"finance{args.suffix}.db"
    conn = connect(db_path)
    conn.execute("DELETE FROM allocations")  # idempotent re-runs
    conn.commit()

    ctx, payments = load_context(conn)
    rows = run(payments, ctx)
    persist_allocations(conn, rows)

    # Accounting integrity: every paisa tagged CASH is real money from a
    # payment, exactly once -- ADJUSTMENT rows (tolerance write-offs,
    # residual deductions) close an invoice without any cash moving, so
    # they must never be counted as received. If this ever drifts, either
    # money that never arrived is being counted as received, or real cash
    # is being miscategorized as an adjustment.
    cash_total = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM allocations "
        "WHERE entry_type = 'CASH'").fetchone()[0]
    payments_total = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM payments").fetchone()[0]
    assert cash_total == payments_total, (
        f"accounting integrity broken: CASH allocations sum to {cash_total}p "
        f"but payments sum to {payments_total}p")

    by_code: dict[str, int] = {}
    for r in rows:
        code = r["reason_code"].value if hasattr(r["reason_code"], "value") else r["reason_code"]
        by_code[code] = by_code.get(code, 0) + 1
    print(f"payments processed={len(payments)} allocation_rows={len(rows)}")
    for code, count in sorted(by_code.items()):
        print(f"  {code}: {count}")
    print(f"accounting check: CASH allocations={cash_total}p == payments={payments_total}p")

    conn.close()


if __name__ == "__main__":
    main()
