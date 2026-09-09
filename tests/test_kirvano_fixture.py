"""THE authoritative parser test: the REAL PIX_GENERATED payload.

``tests/fixtures/kirvano_pix_generated.json`` was captured from the client's own
Kirvano webhook log (PII replaced, every key/type/format byte-faithful). The spec
makes it the source of truth for payload shape — it contradicts the vendor doc in
several places — and requires this test to stay green through every parser change.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.kirvano import parse_payload, redact_payload
from app.models import JobState, Order, RecoveryJob, WebhookEvent
from tests.conftest import dumps

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kirvano_pix_generated.json"

# Values that must never reach the database (they are in the fixture).
CPF = "00000000191"
FBP_COOKIE = "fb.1.1760742058426.392676984852077911"

# Kirvano local times from the fixture (America/Sao_Paulo = UTC-3, no DST in 2026).
CREATED_UTC = datetime(2026, 7, 10, 20, 5, 30, tzinfo=UTC)
EXPIRES_UTC = datetime(2026, 7, 11, 20, 5, 30, tzinfo=UTC)  # 24 h, NOT the doc's 1 h


@pytest.fixture
def real_payload() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_fixture_parses_into_typed_payload(real_payload):
    p = parse_payload(real_payload, "America/Sao_Paulo")
    assert p.event == "PIX_GENERATED"
    assert p.status == "PENDING"
    assert p.sale_id == "5LZEB2GJ"
    assert p.checkout_id == "XE11BWM0"
    assert p.type == "ONE_TIME"
    assert p.is_pix
    # 24 h validity on this merchant's checkout — the doc sample's 1 h is not his setting.
    assert p.created_at == CREATED_UTC
    assert p.payment.expires_at == EXPIRES_UTC
    assert p.payment.expires_at - p.created_at == timedelta(hours=24)
    # fiscal.total_value is the NUMBER 97 and wins over the string "R$ 97,00".
    assert p.amount_cents == 9700
    assert p.total_price_raw == "R$ 97,00"
    assert p.fiscal is not None and p.fiscal["total_value"] == 97
    # payment.qrcode_image is the EMV string, NOT a URL → no usable image URL.
    assert p.payment.qrcode_image == p.payment.qrcode
    assert p.payment.qrcode_image_url is None
    assert p.payment.qrcode.startswith("00020101021226990014br.gov.bcb.pix")
    # Product naming ignores order bumps; this fixture has a single main product.
    assert p.product_name == "A Jornada com meu Anjo"
    assert p.main_product.offer_id == "56619168-3b4c-4052-b28b-238288ad2190"
    assert p.main_product.id == "1c5f17f3-8682-4015-9837-05d2d7807757"
    assert p.main_product.is_order_bump is False
    # Consent evidence.
    assert p.ip == "200.152.1.115"
    # Mixed snake_case/camelCase and unknown keys must not break anything.
    assert p.customer.phone_number == "5551994697674"
    assert p.customer.name == "Maria Souza de Oliveira"
    assert p.idempotency_key() == "PIX_GENERATED|5LZEB2GJ|2026-07-10 17:05:30"


def test_parsed_payload_carries_no_cpf(real_payload):
    """The CPF is dropped at parse time — the dataclass has no field for it."""
    p = parse_payload(real_payload)
    assert not hasattr(p.customer, "document")
    assert CPF not in repr(p.customer)


def test_redact_payload_drops_cpf_and_cookies(real_payload):
    safe = redact_payload(real_payload)
    assert "cookies" not in safe
    assert "document" not in safe["customer"]
    blob = json.dumps(safe)
    assert CPF not in blob and FBP_COOKIE not in blob
    # Everything else survives verbatim, including the odd camelCase keys.
    assert safe["sale_id"] == "5LZEB2GJ"
    assert safe["contactEmail"] == "jornadacommeuanjo@outlook.com"
    assert safe["utm"]["utm_source"] == "FB"
    assert safe["fiscal"]["total_value"] == 97
    assert safe["ip"] == "200.152.1.115"


def test_real_webhook_happy_path(client, session, settings, frozen_clock, real_payload):
    """POST the real body to the endpoint and assert the whole happy path."""
    frozen_clock.set(CREATED_UTC)
    response = client.post(
        "/webhooks/kirvano",
        content=dumps(real_payload),
        headers={"content-type": "application/json", "x-kirvano-token": "kirvano-test-token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "processed" and body["reason"] == "created"

    order = session.execute(select(Order).where(Order.sale_id == "5LZEB2GJ")).scalar_one()
    assert order.status == "pending"
    assert order.checkout_id == "XE11BWM0"
    assert order.offer_id == "56619168-3b4c-4052-b28b-238288ad2190"
    assert order.product_name == "A Jornada com meu Anjo"
    assert order.customer_name == "Maria Souza de Oliveira"
    assert order.customer_email == "maria.souza.exemplo@gmail.com"
    assert order.amount_cents == 9700
    assert order.currency == "BRL"
    # 55 + DDD 51 + 9 digits, plus the 12-digit alternate form.
    assert order.phone_raw == "5551994697674"
    assert order.phone_e164 == "5551994697674"
    assert order.phone_alt == "555194697674"
    assert order.pix_code.startswith("00020101021226990014br.gov.bcb.pix")
    assert order.pix_qr_image_url is None  # EMV string, never an <img src>
    assert order.pix_expires_at == EXPIRES_UTC
    assert order.pix_expires_at.tzinfo is not None
    assert order.consent_ip == "200.152.1.115"
    assert order.consent_at == CREATED_UTC
    assert len(order.page_token) >= 16
    assert order.page_token.isascii()

    # LGPD: no CPF anywhere.
    assert order.customer_document is None

    job = session.execute(select(RecoveryJob).where(RecoveryJob.order_id == order.id)).scalar_one()
    assert job.state == JobState.SCHEDULED.value
    assert job.run_at == CREATED_UTC + timedelta(minutes=10)  # now + delay, no clamp at 24 h
    assert job.attempts == 0
    assert job.reason is None

    event = session.execute(select(WebhookEvent)).scalar_one()
    assert event.source == "kirvano" and event.event == "PIX_GENERATED"
    assert event.sale_id == "5LZEB2GJ" and event.outcome == "processed"
    # Unknown keys are stored untouched; CPF and cookies are not stored at all.
    stored = json.dumps(event.payload, ensure_ascii=False)
    assert CPF not in stored and FBP_COOKIE not in stored
    assert "cookies" not in event.payload
    assert "document" not in event.payload["customer"]
    assert event.payload["event_description"] == "PIX gerado"
    assert event.payload["coproductionCommission"] == 0
    assert event.payload["customer"]["address"] == {
        "city": None,
        "state": None,
        "number": None,
        "street": None,
        "zipcode": None,
        "complement": None,
        "neighborhood": None,
    }


def test_real_payload_replay_is_idempotent(client, session, frozen_clock, real_payload):
    """Kirvano's "Reenviar webhook" button replays the same body: one order, one job."""
    frozen_clock.set(CREATED_UTC)
    headers = {"content-type": "application/json", "x-kirvano-token": "kirvano-test-token"}
    first = client.post("/webhooks/kirvano", content=dumps(real_payload), headers=headers)
    second = client.post("/webhooks/kirvano", content=dumps(real_payload), headers=headers)
    assert first.json()["outcome"] == "processed"
    assert second.json()["outcome"] == "duplicate"
    assert len(session.execute(select(Order)).scalars().all()) == 1
    assert len(session.execute(select(RecoveryJob)).scalars().all()) == 1
    assert len(session.execute(select(WebhookEvent)).scalars().all()) == 1


def test_real_payload_template_params(client, session, store, settings, frozen_clock, real_payload):
    """The four body params the client's template expects, from the real order."""
    from app.whatsapp import build_template_payload

    frozen_clock.set(CREATED_UTC)
    client.post(
        "/webhooks/kirvano",
        content=dumps(real_payload),
        headers={"content-type": "application/json", "x-kirvano-token": "kirvano-test-token"},
    )
    order = session.execute(select(Order).where(Order.sale_id == "5LZEB2GJ")).scalar_one()
    body = build_template_payload(order, store, to=order.phone_e164, settings=settings)
    params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
    assert params == ["Maria", "5LZEB2GJ", "97,00", "11/07 às 17:05"]
    assert body["template"]["components"][1]["parameters"][0]["text"] == order.page_token
    assert body["to"] == "5551994697674"


def test_amount_falls_back_to_total_price_without_fiscal(real_payload):
    """Older/other events have no `fiscal` block: the formatted string is the fallback."""
    payload = dict(real_payload)
    payload.pop("fiscal")
    payload["total_price"] = "R$ 1.169,80"
    assert parse_payload(payload).amount_cents == 116980


def test_expiry_from_fixture_is_not_clamped_but_a_short_one_is(real_payload, settings):
    """Correction #1: 24 h expiry never trips the clamp; a short expiry still does."""
    from datetime import time

    from app.scheduling import compute_run_at

    p = parse_payload(real_payload)
    decision = compute_run_at(CREATED_UTC, 10, p.payment.expires_at, time(22, 0), time(8, 0))
    assert decision.run_at == CREATED_UTC + timedelta(minutes=10)
    assert not decision.clamped_to_expiry

    short = compute_run_at(
        CREATED_UTC, 10, CREATED_UTC + timedelta(minutes=8), time(22, 0), time(8, 0)
    )
    assert short.run_at == CREATED_UTC + timedelta(minutes=5) and short.clamped_to_expiry


def test_unknown_keys_and_missing_sections_are_tolerated(real_payload):
    """The parser must never assume a naming convention or a key's presence."""
    payload = dict(real_payload)
    payload["brandNewCamelCaseKey"] = {"nested": [1, 2, 3]}
    payload["another_snake_key"] = None
    payload.pop("products")
    payload.pop("utm")
    payload["customer"] = {"phone_number": "5551994697674"}
    p = parse_payload(payload)
    assert p.sale_id == "5LZEB2GJ"
    assert p.products == [] and p.product_name is None and p.offer_id is None
    assert p.customer.name is None
    assert p.customer.phone_number == "5551994697674"
