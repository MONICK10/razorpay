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


def bucket_payments(entries: list[dict], threshold: int) -> dict:
    """The three-bucket picture the finance team sees at a given confidence
    dial: on autopilot, needs a quick confirm, or genuinely unresolved.
    Single source of truth for both simulator/state.py (live, per-world)
    and scoring/score.py (the committed submission dataset) so the two
    can't drift into reporting different totals for "the same" dataset the
    way they did before this function existed -- a payment that produced
    several ledger rows (BULK-N split across invoices, TOL-FEE's
    CASH/ADJUSTMENT pair) was being counted once per row instead of once
    per payment, so auto_matched + human_touch could exceed the payment
    count entirely.

    entries: exactly one dict per unique payment_id --
      {"payment_id", "resolved": bool, "confidence": int | None,
       "reason_code": str, "virtual_account": str | None}
    confidence is read only when resolved is True.

    Returns auto_posted / needs_confirmation / exceptions as payment
    counts -- these three always sum to len(entries), asserted below.
    exceptions_grouped is the same exceptions bucket collapsed by
    human_touch_groups (several exceptions for one customer become one
    sitting) -- informational, not part of the sum: grouping is a "how
    many decisions" number, not a "how many payments" number, so it can
    (and usually does) come in lower than `exceptions`.
    """
    auto_posted = needs_confirmation = 0
    exception_entries = []
    for e in entries:
        if e["resolved"]:
            if e["confidence"] >= threshold:
                auto_posted += 1
            else:
                needs_confirmation += 1
        else:
            exception_entries.append(e)

    groups = human_touch_groups(exception_entries)

    result = {
        "total": len(entries),
        "auto_posted": auto_posted,
        "needs_confirmation": needs_confirmation,
        "exceptions": len(exception_entries),
        "exceptions_grouped": len(groups),
    }
    partitioned = result["auto_posted"] + result["needs_confirmation"] + result["exceptions"]
    assert partitioned == result["total"], (
        f"bucket_payments: auto_posted({result['auto_posted']}) + "
        f"needs_confirmation({result['needs_confirmation']}) + "
        f"exceptions({result['exceptions']}) = {partitioned}, "
        f"expected {result['total']}")
    return result


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
