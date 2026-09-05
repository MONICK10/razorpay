import hashlib
import hmac
import json

from simulator import webhook

SECRET = "whsec_test_secret"


def _signed_body(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, sig


def test_verify_signature_accepts_valid_signature():
    raw, sig = _signed_body({"event": "payment.captured"})
    assert webhook.verify_signature(raw, sig, SECRET) is True


def test_verify_signature_rejects_wrong_signature():
    raw, _ = _signed_body({"event": "payment.captured"})
    assert webhook.verify_signature(raw, "0" * 64, SECRET) is False


def test_verify_signature_rejects_missing_signature():
    raw, _ = _signed_body({"event": "payment.captured"})
    assert webhook.verify_signature(raw, None, SECRET) is False


def test_verify_signature_rejects_tampered_body():
    raw, sig = _signed_body({"event": "payment.captured", "amount": 100})
    tampered = raw.replace(b'"amount": 100', b'"amount": 999999')
    assert webhook.verify_signature(tampered, sig, SECRET) is False


def test_verify_signature_rejects_missing_secret():
    raw, sig = _signed_body({"event": "payment.captured"})
    assert webhook.verify_signature(raw, sig, "") is False


def test_is_handled_event():
    assert webhook.is_handled_event({"event": "payment.captured"}) is True
    assert webhook.is_handled_event({"event": "payment_link.paid"}) is True
    assert webhook.is_handled_event({"event": "payment.failed"}) is False
    assert webhook.is_handled_event({"event": "refund.processed"}) is False


def _sample_payload(**overrides) -> dict:
    entity = {
        "id": "pay_TestABC123",
        "amount": 500_000,
        "currency": "INR",
        "status": "captured",
        "method": "upi",
        "email": "buyer@example.com",
        "contact": "+919999999999",
        "notes": {
            "virtual_account": "LIVE-VA-01",
            "invoice_id": "LIVE-INV-01",
            "payer_name": "Test Buyer One",
        },
        "created_at": 1735689600,  # 2025-01-01T00:00:00Z
    }
    entity.update(overrides)
    return {
        "entity": "event",
        "event": "payment.captured",
        "payload": {"payment": {"entity": entity}},
    }


def test_map_payload_reads_notes_for_identity():
    payment = webhook.map_payload_to_payment(_sample_payload())
    assert payment["payment_id"] == "pay_TestABC123"
    assert payment["amount_paise"] == 500_000
    assert payment["virtual_account"] == "LIVE-VA-01"
    assert payment["payer_name"] == "Test Buyer One"
    assert "LIVE-INV-01" in payment["narration"]
    assert payment["method"] == "UPI"
    assert payment["created_at"] == "2025-01-01T00:00:00+00:00"


def test_map_payload_falls_back_to_email_when_no_payer_name_note():
    payload = _sample_payload(notes={"virtual_account": "LIVE-VA-01"})
    payment = webhook.map_payload_to_payment(payload)
    assert payment["payer_name"] == "buyer@example.com"


def test_map_payload_handles_missing_notes():
    payload = _sample_payload(notes={})
    payment = webhook.map_payload_to_payment(payload)
    assert payment["virtual_account"] is None
    assert "LIVE-INV" not in payment["narration"]
