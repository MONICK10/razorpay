"""The /app processing flow: the real 64-payment submission dataset
(engine/generate_data.py), reordered so 13 of its own payments -- picked to
walk through the same arc of reason codes /story tells, in the same order
-- run first. The remaining 51 follow in their original chronological order.

This is a pure reorder, not a substitution: all 64 payments, and every one
of their reason codes, are exactly what engine/generate_data.py already
produces. tests/test_batch_reorder.py is the proof -- it runs the real
engine both ways and asserts nothing changed.

Merchant framing ("KFG Snacks Store") matches /story so the two feel like
one product; the 64 payments themselves have nothing to do with /story's
own (separate, fictional) Kumar/Priya/Deccan world.
"""
from __future__ import annotations

from simulator.story_seed import CODE_BADGE, CODE_PLAIN  # noqa: F401 -- re-exported

MERCHANT_NAME = "KFG Snacks Store"
MERCHANT_KEY = "rzp_test_KFGSnacks01"

# One line per reason code, plain-English, no jargon -- used by the
# post-processing "summary by case type" table.
CODE_EXPLAIN = {
    "EXACT": "Bill number and amount both agreed.",
    "REF-FUZZY": "The bill number was typed wrong. AI worked out which one they meant.",
    "PART-EXP": "They paid part of it. The rest is expected later.",
    "RESID-DED": "They paid less on purpose. The bill is closed and the gap recorded as a deduction.",
    "TOL-FEE": "A tiny amount was missing, small enough to write off. The bill closed anyway.",
    "BULK-N": "One payment covering several bills at once.",
    "FIFO-TIE": "Two bills were identical in every way, so we applied the oldest-first rule.",
    "DUP-ONACC": "They paid the same bill twice. The extra is held as credit for their next bill.",
    "AMBIG-N": "We know who paid. We cannot tell which bill it settles.",
    "NO-MATCH": "We know who paid. Nothing they owe fits this amount.",
    "SUSPENSE": "We do not even know who sent this money.",
}

Q_WHO = "Who sent this money?"
Q_NUM = "Did they write the bill number?"
Q_NUM_ANY = "Did they write a bill number?"
Q_MSGY = "Can the computer make sense of the note?"
Q_SHORT = "Is the missing amount small enough to ignore?"
Q_MATCH = "Does the amount match the bill?"
Q_RULE = "Does a simple rule settle it?"
Q_PROVE = "Can we prove which bill it is?"


def _n(payment_id, story_line, how, changed):
    return {
        "payment_id": payment_id,
        "story_line": story_line,
        "how_it_decided": [{"q": q, "a": a, "ok": ok} for (q, a, ok) in how],
        "what_changed": changed,
    }


# The 13 payments that run first, in this exact order -- each one is a real
# payment_id from engine/generate_data.py's own 64 (seed 42), picked because
# its own reason code matches the position's slot in /story's arc:
# EXACT, REF-FUZZY, TOL-FEE, BULK-N, DUP-ONACC, FIFO-TIE, EXACT, EXACT,
# PART-EXP, RESID-DED, NO-MATCH, NO-MATCH, SUSPENSE.
#
# Every payment_id here is order-independent except the EXACT/DUP-ONACC
# pair (slots 1 and 5, same invoice, same customer) -- the EXACT settlement
# must run before its duplicate, which it does here (position 1 before
# position 5). tests/test_batch_reorder.py verifies all 64 outcomes are
# unchanged by running the real engine both ways.
STORY_SLOT_PAYMENT_IDS = [
    "PAY-0008",  # 1: EXACT -- Windward Solutions / INV-0008
    "PAY-0034",  # 2: REF-FUZZY -- Udaan Textiles / INV-0036 (typo'd "INV0036")
    "PAY-0014",  # 3: TOL-FEE -- Horizon Solutions / INV-0014
    "PAY-0028",  # 4: BULK-N -- Acme Exports, no reference, two invoices
    "PAY-0044",  # 5: DUP-ONACC -- Windward Solutions, duplicate of slot 1's invoice
    "PAY-0036",  # 6: FIFO-TIE -- Bluepeak Solutions, two identical open invoices
    "PAY-0007",  # 7: EXACT -- Ridge Enterprises / INV-0007
    "PAY-0006",  # 8: EXACT -- Horizon Solutions / INV-0006
    "PAY-0020",  # 9: PART-EXP -- Ridge Textiles / INV-0020
    "PAY-0026",  # 10: RESID-DED -- Indus Enterprises / INV-0026
    "PAY-0054",  # 11: NO-MATCH -- Deccan Textiles
    "PAY-0060",  # 12: NO-MATCH -- Falcon Pvt Ltd (a different customer than 11)
    "PAY-0051",  # 13: SUSPENSE -- unmapped virtual account
]

NARRATIONS = {
    n["payment_id"]: n for n in [
        _n("PAY-0008",
           "Windward Solutions owes Rs 2,23,657 on bill INV-0008. They send "
           "exactly that amount and name the bill in the transfer. "
           "Everything lines up.",
           [(Q_WHO, "We know: it came from Windward Solutions' account.", True),
            (Q_NUM, "Yes - INV-0008, and it is still unpaid.", True),
            (Q_MATCH, "Yes, to the rupee.", True)],
           "Bill INV-0008 was Rs 2,23,657. Now paid."),

        _n("PAY-0034",
           "Udaan Textiles owes Rs 33,644 on bill INV-0036. They pay the "
           "right amount but type the bill number as 'INV0036', with no "
           "dash - so the exact match fails.",
           [(Q_WHO, "We know: it came from Udaan Textiles' account.", True),
            (Q_NUM, "Not exactly - 'INV0036' matches no bill on file.", False),
            (Q_MSGY, "Yes - 'INV0036' can only mean bill INV-0036. It fixes "
                     "the typo.", True),
            (Q_MATCH, "Yes, Rs 33,644 exactly.", True)],
           "Bill INV-0036 was Rs 33,644. Now paid. Marked for a quick check "
           "because the bill number had to be corrected first."),

        _n("PAY-0014",
           "Horizon Solutions owes Rs 1,57,159 on bill INV-0014. Only Rs "
           "1,56,452.06 arrives - the bank kept Rs 706.94 in transfer "
           "charges. They name the bill.",
           [(Q_WHO, "We know: it came from Horizon Solutions' account.", True),
            (Q_NUM, "Yes - INV-0014, still unpaid.", True),
            (Q_SHORT, "Yes. Rs 706.94 is well under the limit for bank "
                      "charges, so the bill still closes.", True)],
           "Bill INV-0014 was Rs 1,57,159. Now paid - Rs 1,56,452.06 "
           "received, Rs 706.94 written off as a bank charge."),

        _n("PAY-0028",
           "Rs 68,996 lands from Acme Exports with no note at all. They "
           "have two unpaid bills left: Rs 46,564 and Rs 22,432.",
           [(Q_WHO, "We know: it came from Acme Exports' account.", True),
            (Q_NUM_ANY, "No - the transfer has no note.", False),
            (Q_MSGY, "There is no note to work with.", None),
            (Q_RULE, "Yes - Rs 46,564 plus Rs 22,432 is exactly Rs 68,996. "
                     "That is the only pair that works.", True)],
           "Bill INV-0029 was Rs 46,564. Now paid. Bill INV-0030 was Rs "
           "22,432. Now paid. Marked for a quick check because no bill "
           "number was given."),

        _n("PAY-0044",
           "Windward Solutions pays Rs 2,23,657 for bill INV-0008 again - "
           "already settled by an earlier payment. This looks like an "
           "accidental repeat.",
           [(Q_WHO, "We know: it came from Windward Solutions' account.", True),
            (Q_NUM, "Yes - INV-0008. But that bill was already paid in full.", False),
            ("What do we do with money for an already-paid bill?",
             "We do not count it twice. We keep it aside as credit.", True)],
           "Rs 2,23,657 kept aside as credit for Windward Solutions' next "
           "bill. Nothing was counted twice."),

        _n("PAY-0036",
           "Rs 27,224 arrives from Bluepeak Solutions with no bill number. "
           "They have two open bills, both exactly Rs 27,224, with nothing "
           "to tell them apart.",
           [(Q_WHO, "We know: it came from Bluepeak Solutions' account.", True),
            (Q_NUM_ANY, "No - no note.", False),
            (Q_MSGY, "There is no note to work with.", None),
            (Q_RULE, "Yes - both bills are the same amount and neither is "
                     "disputed, so it is safe to take the older one first.", True)],
           "Bill INV-0039 was Rs 27,224. Now paid. INV-0040 still open at "
           "Rs 27,224."),

        _n("PAY-0007",
           "Ridge Enterprises owes Rs 66,024 on bill INV-0007. They send "
           "exactly that amount and name the bill.",
           [(Q_WHO, "We know: it came from Ridge Enterprises' account.", True),
            (Q_NUM, "Yes - INV-0007, still unpaid.", True),
            (Q_MATCH, "Yes, to the rupee.", True)],
           "Bill INV-0007 was Rs 66,024. Now paid."),

        _n("PAY-0006",
           "Horizon Solutions -- the same company from payment 3 -- also "
           "owes Rs 1,78,346 on a separate bill, INV-0006. They send "
           "exactly that amount and name the bill.",
           [(Q_WHO, "We know: it came from Horizon Solutions' account.", True),
            (Q_NUM, "Yes - INV-0006, still unpaid.", True),
            (Q_MATCH, "Yes, to the rupee.", True)],
           "Bill INV-0006 was Rs 1,78,346. Now paid."),

        _n("PAY-0020",
           "Ridge Textiles owes Rs 67,894 on bill INV-0020. They send Rs "
           "41,493.80 and write 'part payment' with the bill number. They "
           "mean to pay the rest later.",
           [(Q_WHO, "We know: it came from Ridge Textiles' account.", True),
            (Q_NUM, "Yes - INV-0020, still unpaid.", True),
            (Q_SHORT, "No - Rs 26,400.20 is far too big to be a fee, and "
                      "the note says 'part payment'. The bill stays open.", False)],
           "Bill INV-0020 was Rs 67,894. Now Rs 26,400.20 still owed."),

        _n("PAY-0026",
           "Indus Enterprises owes Rs 82,730 on bill INV-0026 but sends Rs "
           "77,536.46. Their note says they kept Rs 5,193.54 back for tax. "
           "So the bill is closed - they are not going to pay the rest.",
           [(Q_WHO, "We know: it came from Indus Enterprises' account.", True),
            (Q_NUM, "Yes - INV-0026, still unpaid.", True),
            (Q_SHORT, "Too big for a bank fee - but the note says they kept "
                      "it back on purpose for tax. So the bill closes and we "
                      "do not chase the rest.", True)],
           "Bill INV-0026 was Rs 82,730. Now paid - Rs 77,536.46 received, "
           "Rs 5,193.54 written off as tax withheld."),

        _n("PAY-0054",
           "Rs 10,041 arrives from Deccan Textiles with no bill number. "
           "They have several open bills, but nothing - alone or in "
           "combination - adds up to Rs 10,041.",
           [(Q_WHO, "We know: it came from Deccan Textiles' account.", True),
            (Q_NUM_ANY, "No - no note.", False),
            (Q_MSGY, "There is no note to work with.", None),
            (Q_RULE, "No. Rs 10,041 matches no bill, and no group of their "
                     "bills adds up to it.", False),
            (Q_PROVE, "No. We will not guess. We will ask Deccan Textiles.", False)],
           "Nothing paid. Held, and added to the list of things to ask "
           "Deccan Textiles about."),

        _n("PAY-0060",
           "Another unmatched payment, this time Rs 4,771 from Falcon Pvt "
           "Ltd - a different customer than payment 11. Same story: "
           "nothing they owe, alone or combined, comes to this amount.",
           [(Q_WHO, "We know: it came from Falcon Pvt Ltd's account.", True),
            (Q_NUM_ANY, "No - no note.", False),
            (Q_MSGY, "There is no note to work with.", None),
            (Q_RULE, "No. Rs 4,771 matches nothing Falcon Pvt Ltd owes.", False),
            (Q_PROVE, "No. Held, to ask Falcon Pvt Ltd.", False)],
           "Nothing paid. We now know Falcon Pvt Ltd's total to the "
           "rupee, but not which bills the held money covers."),

        _n("PAY-0051",
           "Rs 40,697 shows up with a payer name attached, but the account "
           "it came from does not match any customer on file. There is no "
           "reliable way to tell who really sent it.",
           [(Q_WHO, "No account we recognise, so we cannot tell who paid.", False),
            ("Anything else we can check?",
             "No. Without a matching account, nothing else can run.", None)],
           "Rs 40,697 is set aside on its own. It stays there until someone "
           "works out who sent it."),
    ]
}
