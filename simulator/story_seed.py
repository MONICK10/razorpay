"""The Story world: one scripted day at "KFG Snacks Store", told for a
short screen recording -- 13 payments in order, every outcome produced by
the real engine (engine.pipeline.resolve_payment via simulator.state), and
every word on the page written for someone with no finance or engineering
background.

Independent of every other world. Nothing here changes how matching works;
it only picks the inputs and supplies the plain-English narration.

=== Verified, not reasoned ===
Every `expected_code` / `first_code` below was checked by running the real
engine step by step (forward pass + re-check after each payment, exactly as
the UI drives it). tests/test_story_world.py is that check, committed. Where
an amount differs from a naive reading of the original brief, a comment
says which clash the change avoids.

Amount convention: whole rupees here, x100 to paise at load
(simulator.state._init_story_world). "5000" means Rs 5,000.
"""
from __future__ import annotations

RUPEE = 100

CUSTOMERS = [
    {"customer_id": "STORY-KUMAR", "name": "Kumar Stores", "virtual_account": "VA-01"},
    {"customer_id": "STORY-PRIYA", "name": "Priya Mart", "virtual_account": "VA-02"},
    {"customer_id": "STORY-DECCAN", "name": "Deccan Traders", "virtual_account": "VA-03"},
]

# Only INV-08 is under dispute. That is what makes payments 6 and 7
# genuinely un-sortable: Rs 6,000 fits all three of Priya's bills, and
# because one of the three is disputed, which bill the money lands on
# actually matters -- so the app refuses to pick. Once payment 8 names
# INV-08 and clears it, only two identical, undisputed bills are left and
# the two held payments each match one, on the next pass.
INVOICES_RUPEES = [
    {"invoice_id": "INV-01", "customer_id": "STORY-KUMAR", "amount": 5000,
     "issue_date": "2026-08-01", "due_date": "2026-08-25", "disputed": 0},
    {"invoice_id": "INV-02", "customer_id": "STORY-KUMAR", "amount": 9000,
     "issue_date": "2026-08-02", "due_date": "2026-08-26", "disputed": 0},
    {"invoice_id": "INV-03", "customer_id": "STORY-KUMAR", "amount": 2000,
     "issue_date": "2026-08-03", "due_date": "2026-08-27", "disputed": 0},
    {"invoice_id": "INV-04", "customer_id": "STORY-KUMAR", "amount": 3500,
     "issue_date": "2026-08-04", "due_date": "2026-08-28", "disputed": 0},
    {"invoice_id": "INV-05", "customer_id": "STORY-KUMAR", "amount": 4500,
     "issue_date": "2026-08-05", "due_date": "2026-08-29", "disputed": 0},

    {"invoice_id": "INV-06", "customer_id": "STORY-PRIYA", "amount": 6000,
     "issue_date": "2026-08-06", "due_date": "2026-08-30", "disputed": 0},
    {"invoice_id": "INV-07", "customer_id": "STORY-PRIYA", "amount": 6000,
     "issue_date": "2026-08-07", "due_date": "2026-08-31", "disputed": 0},
    {"invoice_id": "INV-08", "customer_id": "STORY-PRIYA", "amount": 6000,
     "issue_date": "2026-08-08", "due_date": "2026-09-01", "disputed": 1},

    {"invoice_id": "INV-09", "customer_id": "STORY-DECCAN", "amount": 15000,
     "issue_date": "2026-08-09", "due_date": "2026-09-02", "disputed": 0},
    {"invoice_id": "INV-10", "customer_id": "STORY-DECCAN", "amount": 20000,
     "issue_date": "2026-08-10", "due_date": "2026-09-03", "disputed": 0},
    {"invoice_id": "INV-11", "customer_id": "STORY-DECCAN", "amount": 10000,
     "issue_date": "2026-08-11", "due_date": "2026-09-04", "disputed": 0},
]

# Short badges a finance person recognises. Plain English always sits
# beside them in the UI -- never the badge alone.
CODE_PLAIN = {
    "EXACT": "Matched exactly",
    "REF-FUZZY": "Bill number typo, fixed",
    "TOL-FEE": "A few rupees short, ignored",
    "RESID-DED": "Short on purpose (tax) - bill closed",
    "PART-EXP": "Part payment - rest still owed",
    "BULK-N": "One payment across several bills",
    "FIFO-TIE": "Two identical bills - took the older one",
    "DUP-ONACC": "Paid twice - extra kept as credit",
    "AMBIG-N": "Could not tell which bill",
    "NO-MATCH": "Nothing they owe matches this",
    "SUSPENSE": "Do not know who sent it",
}

# What the badge actually reads on screen. Same as the engine code
# everywhere except FIFO-TIE -- "FIFO" is jargon a viewer should not have
# to know, so the tie badge reads plainly instead.
CODE_BADGE = {c: c for c in CODE_PLAIN}
CODE_BADGE["FIFO-TIE"] = "OLDEST-FIRST"

# Live customer state, in plain words (the closing-panel copy in ENDING is
# separate -- that describes the end of the day, this describes right now).
STATUS_PLAIN = {
    "CLEAN": "All settled",
    "PARTIAL": "Part paid",
    "PROVISIONAL": "Amount known, bills unclear",
}

Q_WHO = "Who sent this money?"
Q_NUM = "Did they write the bill number?"
Q_NUM_ANY = "Did they write a bill number?"
Q_MSGY = "Can the computer make sense of the note?"
Q_SHORT = "Is the missing amount small enough to ignore?"
Q_MATCH = "Does the amount match the bill?"
Q_RULE = "Does a simple rule settle it?"
Q_PROVE = "Can we prove which bill it is?"


def _p(n, amount, va, narration, expected_code, story_line, how, changed,
       raw_payer, first_code=None):
    return {
        "n": n, "amount": amount, "virtual_account": va, "narration": narration,
        "expected_code": expected_code, "first_code": first_code or expected_code,
        "story_line": story_line, "how_it_decided": how, "what_changed": changed,
        "raw_payer": raw_payer,
    }


PAYMENTS_RUPEES = [
    _p(1, 5000, "VA-01", "NEFT/KUMAR STORES/INV-01", "EXACT",
       "Kumar owes Rs 5,000 on bill INV-01. He sends exactly Rs 5,000 and "
       "writes the bill number on the transfer. Everything lines up.",
       [(Q_WHO, "We know: it came from Kumar's account.", True),
        (Q_NUM, "Yes - INV-01, and it is still unpaid.", True),
        (Q_MATCH, "Yes, to the rupee.", True)],
       "Bill INV-01 is fully paid. Kumar owes nothing on it.",
       "KUMAR STORES"),

    _p(2, 9000, "VA-01", "NEFT/KUMAR/INV02", "REF-FUZZY",
       "Kumar owes Rs 9,000 on bill INV-02. He pays the right amount but "
       "types the bill number as 'INV02', with no dash - so the exact "
       "match fails.",
       [(Q_WHO, "We know: it came from Kumar's account.", True),
        (Q_NUM, "Not exactly - 'INV02' matches no bill on file.", False),
        (Q_MSGY, "Yes - 'INV02' can only mean bill INV-02. It fixes the typo.", True),
        (Q_MATCH, "Yes, Rs 9,000 exactly.", True)],
       "Bill INV-02 is fully paid. Marked for a quick check, because the "
       "bill number had to be corrected first.",
       "KUMAR STORES"),

    _p(3, 1960, "VA-01", "NEFT/KUMAR/INV-03", "TOL-FEE",
       "Kumar owes Rs 2,000 on bill INV-03. Only Rs 1,960 arrives - the "
       "bank kept Rs 40 in transfer charges. He names the bill.",
       [(Q_WHO, "We know: it came from Kumar's account.", True),
        (Q_NUM, "Yes - INV-03, still unpaid.", True),
        (Q_SHORT, "Yes. Rs 40 is well under the limit for bank charges, so "
                  "the bill still closes.", True)],
       "Bill INV-03 is closed. Rs 1,960 is real money received; the Rs 40 "
       "gap is written off as a bank charge.",
       "KUMAR STORES"),

    _p(4, 8000, "VA-01", "", "BULK-N",
       "Rs 8,000 lands from Kumar with no note at all. He has two unpaid "
       "bills left: Rs 3,500 and Rs 4,500.",
       [(Q_WHO, "We know: it came from Kumar's account.", True),
        (Q_NUM_ANY, "No - the transfer has no note.", False),
        (Q_MSGY, "There is no note to work with.", None),
        (Q_RULE, "Yes - Rs 3,500 plus Rs 4,500 is exactly Rs 8,000. That is "
                 "the only pair that works.", True)],
       "Both bills - INV-04 and INV-05 - are paid. Marked for a quick "
       "check, because no bill number was given.",
       "KUMAR STORES"),

    _p(5, 5000, "VA-01", "NEFT/KUMAR/INV-01", "DUP-ONACC",
       "Kumar pays Rs 5,000 for bill INV-01 again - he already paid it with "
       "payment 1. This looks like an accidental repeat.",
       [(Q_WHO, "We know: it came from Kumar's account.", True),
        (Q_NUM, "Yes - INV-01. But that bill was already paid in full.", False),
        ("What do we do with money for an already-paid bill?",
         "We do not count it twice. We keep it aside as credit.", True)],
       "Rs 5,000 kept aside as credit for Kumar's next bill. Nothing was "
       "counted twice.",
       "KUMAR STORES"),

    _p(6, 6000, "VA-02", "", "FIFO-TIE",
       "Rs 6,000 from Priya with no note. She has three bills of exactly "
       "Rs 6,000, and one of those three is under dispute - so which bill "
       "this pays actually matters.",
       [(Q_WHO, "We know: it came from Priya's account.", True),
        (Q_NUM_ANY, "No - no note.", False),
        (Q_MSGY, "There is no note to work with.", None),
        (Q_RULE, "No. Rs 6,000 fits all three bills, and one is under "
                 "dispute - so the choice matters.", False),
        (Q_PROVE, "No. We will not guess. We will ask Priya.", False)],
       "Nothing paid yet. Held, and added to the list of things to ask "
       "Priya about.",
       "PRIYA MART", first_code="AMBIG-N"),

    _p(7, 6000, "VA-02", "", "EXACT",
       "A second Rs 6,000 from Priya, again with no note. Same problem: "
       "three Rs 6,000 bills, one under dispute, no way to be sure which "
       "one this pays.",
       [(Q_WHO, "We know: it came from Priya's account.", True),
        (Q_NUM_ANY, "No - no note.", False),
        (Q_MSGY, "There is no note to work with.", None),
        (Q_RULE, "No. Still three matching Rs 6,000 bills with one under "
                 "dispute - the choice still matters.", False),
        (Q_PROVE, "No. Held with payment 6, waiting on Priya.", False)],
       "Nothing paid yet. Held with payment 6.",
       "PRIYA MART", first_code="AMBIG-N"),

    _p(8, 6000, "VA-02", "NEFT/PRIYA MART/INV-08 DISPUTE SETTLED", "EXACT",
       "Priya pays Rs 6,000 and this time names the bill - INV-08, the "
       "disputed one, now agreed. That clears it and leaves only two "
       "identical bills, so payments 6 and 7 can finally be sorted.",
       [(Q_WHO, "We know: it came from Priya's account.", True),
        (Q_NUM, "Yes - INV-08, still unpaid.", True),
        (Q_MATCH, "Yes, Rs 6,000 exactly.", True)],
       "Bill INV-08 is paid. With it settled, the two held payments each "
       "match one of the two bills left - both are now sorted, on their own.",
       "PRIYA MART"),

    _p(9, 8000, "VA-03", "PART PAYMENT INV-09", "PART-EXP",
       "Deccan owes Rs 15,000 on bill INV-09. He sends Rs 8,000 and writes "
       "'part payment' with the bill number. He means to pay the rest later.",
       [(Q_WHO, "We know: it came from Deccan's account.", True),
        (Q_NUM, "Yes - INV-09, still unpaid.", True),
        (Q_SHORT, "No - Rs 7,000 is far too big to be a fee, and his note "
                  "says 'part payment'. The bill stays open.", False)],
       "Rs 8,000 goes towards bill INV-09. Rs 7,000 is still owed on it.",
       "DECCAN TRADERS"),

    _p(10, 9000, "VA-03", "INV-11 NET OF TDS", "RESID-DED",
       "Deccan owes Rs 10,000 on bill INV-11 but sends Rs 9,000. His note "
       "says he kept Rs 1,000 back for tax. So the bill is closed - he "
       "isn't going to pay the rest.",
       [(Q_WHO, "We know: it came from Deccan's account.", True),
        (Q_NUM, "Yes - INV-11, still unpaid.", True),
        (Q_SHORT, "Too big for a bank fee - but his note says he kept it "
                  "back on purpose for tax. So the bill closes and we do "
                  "not chase the Rs 1,000.", True)],
       "Bill INV-11 is closed. Rs 9,000 is real money received; Rs 1,000 is "
       "written off as tax he withheld.",
       "DECCAN TRADERS"),

    _p(11, 4300, "VA-03", "", "NO-MATCH",
       "Rs 4,300 from Deccan with no note. He has two unpaid bills: "
       "Rs 7,000 left on one, Rs 20,000 on another. Rs 4,300 is neither, "
       "and no combination of his bills adds up to it.",
       [(Q_WHO, "We know: it came from Deccan's account.", True),
        (Q_NUM_ANY, "No - no note.", False),
        (Q_MSGY, "There is no note to work with.", None),
        (Q_RULE, "No. Rs 4,300 matches no bill, and no group of his bills "
                 "adds up to it.", False),
        (Q_PROVE, "No. Held, to ask Deccan.", False)],
       "Nothing paid. Held, and added to the list to ask Deccan about.",
       "DECCAN TRADERS"),

    _p(12, 2800, "VA-03", "", "NO-MATCH",
       "Another payment from Deccan with no note - Rs 2,800 this time. "
       "Still nothing he owes matches this amount.",
       [(Q_WHO, "We know: it came from Deccan's account.", True),
        (Q_NUM_ANY, "No - no note.", False),
        (Q_MSGY, "There is no note to work with.", None),
        (Q_RULE, "No. Rs 2,800 matches nothing Deccan owes.", False),
        (Q_PROVE, "No. Held with payment 11.", False)],
       "Nothing paid. We now know Deccan's total to the rupee, but not "
       "which bills the held money covers.",
       "DECCAN TRADERS"),

    _p(13, 4200, None, "CASH DEPOSIT", "SUSPENSE",
       "Rs 4,200 shows up as a cash deposit with no account and no name. "
       "There is no way to tell who paid it.",
       [(Q_WHO, "No account number, so we cannot tell who paid.", False),
        ("Anything else we can check?",
         "No. Without knowing the shop, nothing else can run.", None)],
       "Rs 4,200 is set aside on its own. It stays there until someone "
       "works out who sent it.",
       "CASH / BRANCH DEPOSIT"),
]

# Shown when you press "Why?" on a payment we could not sort. Plain,
# self-contained -- the viewer has the full reason without leaving the row.
WHY_HELD = {
    6: "We can see whose account this came from: Priya Mart. But Rs 6,000 "
       "matches all three of her bills, and one of those three is under "
       "dispute, so which bill it pays really matters. We will not guess "
       "between them; we will ask her.",
    7: "Same as payment 6: from Priya, Rs 6,000, fits all three of her bills, "
       "one of them disputed. The choice matters and cannot be proven, so it "
       "waits with payment 6 for Priya to confirm.",
    11: "We can see this came from Deccan Traders. But he owes Rs 7,000 left "
        "on one bill and Rs 20,000 on another. Rs 4,300 is neither, and no "
        "combination of his bills adds up to it. Putting it on the wrong bill "
        "would be worse than waiting, so we hold it and ask.",
    12: "Also from Deccan, Rs 2,800, and again nothing he owes matches it and "
        "no combination of his bills adds up to it. Held with payment 11 to "
        "ask him about together.",
    13: "This arrived with no account and no name. A cash deposit like this "
        "could be from any customer. There is nothing to match it against "
        "until someone finds out who sent it.",
}

# The closing panel.
ENDING = {
    "STORY-KUMAR": {
        "status_plain": "All settled",
        "line": "every bill paid. Rs 5,000 is sitting as credit for his next one.",
    },
    "STORY-PRIYA": {
        # The original brief wanted three self-sorting refusals. With three
        # identical Rs 6,000 bills that cannot happen: the payment that
        # breaks the tie has to actually name a bill, so it is a clean
        # match, not a refusal. Two held payments (6 and 7) is the most the
        # shape allows -- both clear on the next pass once payment 8
        # settles the disputed bill.
        "status_plain": "All settled",
        "line": "two payments we could not place at first sorted themselves "
                "out once she named a bill.",
    },
    "STORY-DECCAN": {
        "status_plain": "Amount known, bills unclear",
        "line": "we know exactly what he owes. Which bills the held money "
                "covers is still a guess.",
    },
}


def customers() -> list[dict]:
    return [dict(c) for c in CUSTOMERS]


def invoices() -> list[dict]:
    return [
        {"invoice_id": i["invoice_id"], "customer_id": i["customer_id"],
         "amount_paise": i["amount"] * RUPEE, "issue_date": i["issue_date"],
         "due_date": i["due_date"], "disputed": i["disputed"]}
        for i in INVOICES_RUPEES
    ]


def payments() -> list[dict]:
    """Full payment dicts in engine shape, in scripted order, plus the
    display-only copy (`story_line`, `how_it_decided`, `what_changed`,
    `raw_payer`, `expected_code`, `first_code`) the engine ignores."""
    out = []
    for p in PAYMENTS_RUPEES:
        hh = int(p["n"]) + 9
        out.append({
            "payment_id": f"PAY-{p['n']:02d}",
            "amount_paise": p["amount"] * RUPEE,
            "method": "NEFT",
            "virtual_account": p["virtual_account"],
            "payer_name": None,
            "narration": p["narration"],
            "created_at": f"2026-09-04 {hh:02d}:05:00",
            "n": p["n"],
            "story_line": p["story_line"],
            "how_it_decided": [
                {"q": q, "a": a, "ok": ok} for (q, a, ok) in p["how_it_decided"]
            ],
            "what_changed": p["what_changed"],
            "raw_payer": p["raw_payer"],
            "raw_time": f"4 Sep, {hh:02d}:05",
            "date_long": "4 September",  # the whole story is one day
            "expected_code": p["expected_code"],
            "first_code": p["first_code"],
            "why_held": WHY_HELD.get(p["n"], ""),
        })
    return out
