"""Creates a Razorpay test-mode Payment Link for one of the Live world's
seeded invoices (simulator/live_seed.py) and prints its URL.

Attaches virtual_account / invoice_id / payer_name as `notes` on the link --
Razorpay Payment Links have no bank-narration equivalent, so `notes` is the
realistic bridge simulator/webhook.py reads back on the other end to map
the eventual webhook payload to this project's internal payment shape.

Run: python -m scripts.create_payment_link [--invoice LIVE-INV-01]
     python -m scripts.create_payment_link --invoice LIVE-INV-02 --amount 12000
     python -m scripts.create_payment_link --invoice LIVE-INV-01 --notes identity

--notes controls what identifying information rides along in the link's
`notes` (the realistic bridge Razorpay Payment Links use in place of a
bank narration -- see simulator/webhook.py):
  full      (default) notes carry virtual_account, payer_name, AND
            invoice_id -- the engine has a reference to match against,
            so the payment resolves against that bill.
  identity  notes carry virtual_account and payer_name only, no
            invoice_id -- the engine can identify WHO paid but not WHICH
            bill, so it refuses (NO-MATCH) rather than guess. If
            --amount isn't also given, the link amount is deliberately
            set to the invoice's balance + Rs 1 so it can never
            accidentally match by amount alone either.

With --amount, the link is for an arbitrary rupee amount rather than the
invoice's exact balance. The customer association is kept (--invoice
still picks *whose* payment this is), but this implies --notes identity
unless --notes full is passed explicitly to keep the invoice_id
reference despite the different amount.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

import os

from simulator import live_seed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice", default=live_seed.INVOICES[0]["invoice_id"],
                         help="invoice_id from simulator/live_seed.py "
                              f"(default: {live_seed.INVOICES[0]['invoice_id']})")
    parser.add_argument("--amount", type=int, default=None, metavar="RUPEES",
                         help="link amount in whole rupees (default: the invoice's "
                              "exact balance, or that balance + Rs 1 for "
                              "--notes identity). Implies --notes identity "
                              "unless --notes full is passed explicitly.")
    parser.add_argument("--notes", choices=["full", "identity"], default=None,
                         help="full: notes include invoice_id, so the payment "
                              "resolves against that bill. identity: notes carry "
                              "payer identity only (virtual_account + payer_name), "
                              "no invoice_id, so the engine refuses (NO-MATCH). "
                              "Default: full, unless --amount is given (then identity, "
                              "for backward compatibility).")
    args = parser.parse_args()
    if args.amount is not None and args.amount <= 0:
        print("--amount must be a positive number of rupees.", file=sys.stderr)
        sys.exit(1)

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not (key_id and key_secret):
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env first "
              "(test-mode keys from the Razorpay dashboard).", file=sys.stderr)
        sys.exit(1)

    invoices_by_id = {inv["invoice_id"]: inv for inv in live_seed.INVOICES}
    invoice = invoices_by_id.get(args.invoice)
    if invoice is None:
        known = ", ".join(invoices_by_id)
        print(f"Unknown invoice '{args.invoice}'. Known invoices: {known}", file=sys.stderr)
        sys.exit(1)

    customers_by_id = {c["customer_id"]: c for c in live_seed.CUSTOMERS}
    customer = customers_by_id[invoice["customer_id"]]

    notes_mode = args.notes or ("identity" if args.amount is not None else "full")
    identity_only = notes_mode == "identity"

    if args.amount is not None:
        amount_paise = args.amount * 100
    elif identity_only:
        # Off by Rs 1 from the invoice's own balance, on purpose: with no
        # invoice_id in notes, an amount that happened to equal an open
        # invoice's balance would still resolve EXACT via amount-only
        # matching, silently defeating the point of --notes identity.
        amount_paise = invoice["amount_paise"] + 100
    else:
        amount_paise = invoice["amount_paise"]

    notes = {
        "virtual_account": customer["virtual_account"],
        "payer_name": customer["name"],
    }
    if identity_only:
        description = (f"Test payment Rs {amount_paise / 100:,.2f} from "
                       f"{customer['name']} (identity only, no invoice reference)")
    else:
        notes["invoice_id"] = invoice["invoice_id"]
        description = f"Test payment for {invoice['invoice_id']} ({customer['name']})"

    import razorpay

    client = razorpay.Client(auth=(key_id, key_secret))
    link = client.payment_link.create({
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": notes,
    })

    if identity_only:
        print(f"Identity-only link (no invoice_id in notes)  Amount: Rs {amount_paise / 100:,.2f}"
              f"  Customer: {customer['name']}  (identity via {customer['virtual_account']})")
        print("Expect: NO-MATCH -- the engine can tell who paid but not which bill.")
    else:
        print(f"Invoice: {invoice['invoice_id']}  Amount: Rs {amount_paise / 100:,.2f}"
              f"  Customer: {customer['name']}")
        print("Expect: the payment resolves against that invoice.")
    print(f"Payment link: {link['short_url']}")


if __name__ == "__main__":
    main()
