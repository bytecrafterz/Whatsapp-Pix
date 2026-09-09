"""Meta webhook routes: GET challenge, signature verification, status + template updates."""

import hashlib
import hmac

from sqlalchemy import select

from app.inbound import record_outbound_message, window_open
from app.models import Contact, Message, OptOut, TemplateStatus
from tests.conftest import dumps, meta_status_payload, meta_text_payload


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_signed(client, payload, secret="meta-app-secret"):
    body = dumps(payload)
    return client.post(
        "/webhooks/meta",
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": sign(secret, body)},
    )


def test_meta_get_challenge(client):
    r = client.get(
        "/webhooks/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
    )
    assert r.status_code == 200
    assert r.text == "12345"
    assert r.headers["content-type"].startswith("text/plain")


def test_meta_get_challenge_rejects_bad_token(client):
    r = client.get(
        "/webhooks/meta",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert r.status_code == 403
    r = client.get(
        "/webhooks/meta",
        params={"hub.mode": "unsubscribe", "hub.verify_token": "verify-me", "hub.challenge": "1"},
    )
    assert r.status_code == 403


def test_meta_post_signature_valid_invalid_missing(client):
    payload = meta_status_payload("wamid.NOPE", "delivered")
    body = dumps(payload)
    ok = client.post(
        "/webhooks/meta",
        content=body,
        headers={"x-hub-signature-256": sign("meta-app-secret", body)},
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = client.post(
        "/webhooks/meta", content=body, headers={"x-hub-signature-256": sign("other-secret", body)}
    )
    assert bad.status_code == 403
    missing = client.post("/webhooks/meta", content=body)
    assert missing.status_code == 403
    malformed = client.post(
        "/webhooks/meta", content=body, headers={"x-hub-signature-256": "md5=abc"}
    )
    assert malformed.status_code == 403


def test_meta_post_without_secret_configured_accepts(monkeypatch, settings, engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(settings, "meta_app_secret", None)
    with TestClient(create_app(settings)) as c:
        r = c.post("/webhooks/meta", content=dumps(meta_status_payload("wamid.X", "sent")))
        assert r.status_code == 200


def test_meta_post_invalid_json_still_200(client):
    body = b"{not json"
    r = client.post(
        "/webhooks/meta",
        content=body,
        headers={"x-hub-signature-256": sign("meta-app-secret", body)},
    )
    assert r.status_code == 200 and r.json()["outcome"] == "invalid_json"


def test_status_webhook_updates_message_row(client, session, frozen_clock):
    record_outbound_message(
        session,
        wa_id="5511987654321",
        phone="5511987654321",
        message_id="wamid.OUT1",
        kind="template",
        body="x",
        template_name="pix_pendente_v2",
    )
    session.commit()

    r = post_signed(client, meta_status_payload("wamid.OUT1", "delivered"))
    assert r.status_code == 200 and r.json()["statuses"] == 1
    session.expire_all()
    msg = session.execute(select(Message).where(Message.wa_message_id == "wamid.OUT1")).scalar_one()
    assert msg.status == "delivered"
    assert msg.status_updated_at is not None and msg.status_updated_at.tzinfo is not None

    post_signed(client, meta_status_payload("wamid.OUT1", "read"))
    session.expire_all()
    assert session.get(Message, msg.id).status == "read"

    # Out-of-order "sent" after "read" must not regress.
    post_signed(client, meta_status_payload("wamid.OUT1", "sent"))
    session.expire_all()
    assert session.get(Message, msg.id).status == "read"

    err = {
        "code": 131050,
        "title": "opted out",
        "message": "User opted out",
        "error_data": {"details": "no marketing"},
    }
    post_signed(client, meta_status_payload("wamid.OUT1", "failed", error=err))
    session.expire_all()
    m = session.get(Message, msg.id)
    assert m.status == "failed" and m.error_code == "131050" and "opted out" in m.error_text
    rows = session.execute(select(OptOut)).scalars().all()
    assert {r.phone for r in rows} == {"5511987654321", "551187654321"}
    assert rows[0].source == "meta_131050"


def test_status_for_unknown_message_is_counted_unmatched(client):
    r = post_signed(client, meta_status_payload("wamid.UNKNOWN", "delivered"))
    assert r.status_code == 200 and r.json()["statuses"] == 0


def test_inbound_text_opens_window_and_creates_contact(client, session, frozen_clock):
    payload = meta_text_payload("5511987654321", "oi", ts=int(frozen_clock.now.timestamp()))
    r = post_signed(client, payload)
    assert r.status_code == 200 and r.json()["messages"] == 1
    contact = session.get(Contact, "5511987654321")
    assert contact is not None and contact.profile_name == "Fulano"
    assert contact.last_inbound_at == frozen_clock.now
    assert window_open(contact, frozen_clock.now)
    frozen_clock.advance(hours=25)
    assert not window_open(contact, frozen_clock.now)


def test_template_status_and_category_webhooks(client, session):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "958025707339789",
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "event": "PAUSED",
                            "message_template_id": 123,
                            "message_template_name": "pix_pendente_v2",
                            "message_template_language": "pt_BR",
                            "reason": "quality",
                        },
                    }
                ],
            }
        ],
    }
    r = post_signed(client, payload)
    assert r.status_code == 200 and r.json()["template_updates"] == 1
    row = session.get(TemplateStatus, ("pix_pendente_v2", "pt_BR"))
    assert row.status == "PAUSED" and row.reason == "quality" and row.template_id == "123"

    payload["entry"][0]["changes"][0] = {
        "field": "template_category_update",
        "value": {
            "message_template_id": 123,
            "message_template_name": "pix_pendente_v2",
            "message_template_language": "pt_BR",
            "previous_category": "UTILITY",
            "new_category": "MARKETING",
        },
    }
    post_signed(client, payload)
    session.expire_all()
    row = session.get(TemplateStatus, ("pix_pendente_v2", "pt_BR"))
    assert row.category == "MARKETING" and row.status == "PAUSED"


def test_other_fields_are_stored_and_200(client):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "958025707339789",
                "changes": [
                    {
                        "field": "phone_number_quality_update",
                        "value": {
                            "display_phone_number": "55...",
                            "event": "FLAGGED",
                            "current_limit": "TIER_250",
                        },
                    }
                ],
            }
        ],
    }
    r = post_signed(client, payload)
    assert r.status_code == 200 and r.json()["ok"] is True
