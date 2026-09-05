"""Pure, testable pieces of the Razorpay webhook lane -- no FastAPI, no
SessionState, so these can be unit tested without spinning up a server.
simulator/app.py's POST /webhook/razorpay wires these together.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

HANDLED_EVENTS = {"payment.captured", "payment_link.paid"}


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Razorpay's webhook scheme: HMAC-SHA256 of the exact raw request body,
    hex-digest, sent as X-Razorpay-Signature. Must be checked against the
    raw bytes, before any JSON parsing/re-serialization -- re-encoding the
    body (even to semantically identical JSON) changes the bytes and would
    make a genuine delivery fail verification."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_handled_event(payload: dict) -> bool:
    """A webhook URL commonly receives every event type a merchant
    subscribed to -- only payment.captured / payment_link.paid actually
    represent money having arrived; everything else (payment.failed,
    order.paid, refund.*, ...) should be acknowledged (200) and ignored,
    not treated as an error."""
    return payload.get("event") in HANDLED_EVENTS


def map_payload_to_payment(payload: dict) -> dict:
    """Razorpay payment.captured / payment_link.paid event -> this
    project's internal payment shape (same fields simulator/state.py
    builds for every other payment: payment_id, amount_paise, method,
    virtual_account, payer_name, narration, created_at).

    Razorpay Payment Links have no bank-narration equivalent -- so
    scripts/create_payment_link.py attaches virtual_account / invoice_id /
    payer_name as `notes` when creating the link, and this function reads
    them back, the realistic bridge Razorpay itself documents `notes` for.
    Razorpay's `amount` for INR is already an integer in paise, matching
    this project's money convention directly -- no conversion needed.
    """
    entity = payload["payload"]["payment"]["entity"]
    notes = entity.get("notes") or {}
    invoice_id = notes.get("invoice_id")
    virtual_account = notes.get("virtual_account")
    payer_name = notes.get("payer_name") or entity.get("email") or entity.get("contact")

    narration_bits = ["Razorpay", entity["id"]]
    if invoice_id:
        narration_bits.append(invoice_id)
    narration = "/".join(narration_bits) + " PAYMENT LINK"

    created_at_ts = entity.get("created_at")
    created_at = (
        datetime.fromtimestamp(created_at_ts, tz=timezone.utc).isoformat(timespec="seconds")
        if created_at_ts
        else datetime.now(timezone.utc).isoformat(timespec="seconds"))

    return {
        "payment_id": entity["id"],
        "amount_paise": entity["amount"],
        "method": (entity.get("method") or "razorpay").upper(),
        "virtual_account": virtual_account,
        "payer_name": payer_name,
        "narration": narration,
        "created_at": created_at,
    }
