"""L5 -- Exception queue. Every payment that could not be resolved lands
here, typed by why (SUSPENSE / AMBIG-N / NO-MATCH), grouped, and ranked by
rupees so a clerk works the highest-value break first, not the oldest or
the first alphabetically."""
from __future__ import annotations


def human_touch_groups(rows: list[dict]) -> set[tuple[str, str | None]]:
    """Group exception rows into the distinct human decisions they need:
    same reason code + same customer (identified by virtual_account)
    collapse to one touch -- a clerk resolving three AMBIG-N payments for
    the same customer does it in one sitting, not three. SUSPENSE never
    collapses: the payer isn't even identified, so there's no customer to
    group by -- each one is its own touch.

    Single source of truth for "human touches" -- scoring/score.py (the
    batch report) and simulator/state.py (the live comparison strip) both
    call this instead of each grouping exceptions their own way, so the
    two can't drift into reporting different numbers for the same idea.

    `rows` needs `payment_id`, `reason_code`, and `virtual_account` on
    each entry."""
    groups = set()
    for r in rows:
        if r["reason_code"] == "SUSPENSE":
            groups.add(("SUSPENSE", r["payment_id"]))
        else:
            groups.add((r["reason_code"], r.get("virtual_account")))
    return groups


def build_queue(exception_allocations: list[dict]) -> dict:
    """exception_allocations: rows with payment_id, amount_paise,
    reason_code, rationale, virtual_account, payer_name. Returns a report
    dict: total value, per-code groups (each sorted by value desc), and a
    flat list sorted by value desc for the "work this first" view."""
    by_code: dict[str, list[dict]] = {}
    for row in exception_allocations:
        by_code.setdefault(row["reason_code"], []).append(row)

    for code in by_code:
        by_code[code].sort(key=lambda r: r["amount_paise"], reverse=True)

    flat = sorted(exception_allocations, key=lambda r: r["amount_paise"], reverse=True)
    total_value = sum(r["amount_paise"] for r in exception_allocations)

    return {
        "total_value_paise": total_value,
        "count": len(exception_allocations),
        "by_code": {
            code: {
                "count": len(rows),
                "value_paise": sum(r["amount_paise"] for r in rows),
                "rows": rows,
            }
            for code, rows in by_code.items()
        },
        "ranked": flat,
    }
