"""Shared fixtures: env-driven test settings, in-memory SQLite, frozen clock, payload builders."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import clock, db
from app.config import Settings, get_settings, reset_settings_cache
from app.models import Base
from app.settings_store import SettingsStore

# 2026-09-08 15:00 UTC == 12:00 America/Sao_Paulo (UTC-3, no DST): outside quiet hours.
DEFAULT_NOW = datetime(2026, 9, 8, 15, 0, 0, tzinfo=UTC)

GRAPH_MESSAGES_URL = "https://graph.facebook.com/v23.0/1347340825121720/messages"

TEST_ENV: dict[str, str] = {
    "APP_ENV": "test",
    "DATABASE_URL": "sqlite://",
    "LOG_LEVEL": "WARNING",
    "PUBLIC_BASE_URL": "https://api.test.local",
    "KIRVANO_WEBHOOK_TOKEN": "kirvano-test-token",
    "KIRVANO_TOKEN_MODE": "enforce",
    "KIRVANO_TZ": "America/Sao_Paulo",
    "KIRVANO_CHECKOUT_URL": "https://pay.kirvano.com/checkout-uuid",
    "META_ACCESS_TOKEN": "meta-test-token",
    "META_APP_SECRET": "meta-app-secret",
    "META_VERIFY_TOKEN": "verify-me",
    "META_PHONE_NUMBER_ID": "1347340825121720",
    "META_WABA_ID": "958025707339789",
    "PANEL_USER": "admin",
    "PANEL_PASSWORD": "panel-pw",
    "REMINDER_DELAY_MINUTES": "10",
    "DAILY_RECIPIENT_LIMIT": "250",
    "TEMPLATE_URL_BUTTON_INDEX": "1",
    "WORKER_POLL_SECONDS": "0.01",
}


class FrozenClock:
    """Callable stand-in for ``app.clock.utcnow`` with ``set``/``advance`` helpers."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, dt: datetime) -> None:
        assert dt.tzinfo is not None
        self.now = dt

    def advance(self, **kwargs: float) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    fc = FrozenClock(DEFAULT_NOW)
    monkeypatch.setattr(clock, "utcnow", fc)
    return fc


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Test settings come from the environment, exactly like production."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    yield get_settings()
    reset_settings_cache()


@pytest.fixture
def engine(settings: Settings):
    eng = db.make_engine("sqlite://")
    Base.metadata.create_all(eng)
    db.configure(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    s = db.get_sessionmaker()()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def store(session: Session, settings: Settings) -> SettingsStore:
    return SettingsStore(session, settings)


@pytest.fixture
def client(settings: Settings, engine) -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --- payload builders ------------------------------------------------------------------


def sp_str(dt: datetime) -> str:
    """Aware datetime → Kirvano-style naive local string in America/Sao_Paulo."""
    from app.format import SP_TZ

    return dt.astimezone(SP_TZ).strftime("%Y-%m-%d %H:%M:%S")


def kirvano_payload(
    event: str = "PIX_GENERATED",
    *,
    sale_id: str = "D2RP8RQ7",
    phone: str = "5511987654321",
    name: str = "Fulano de Tal",
    total: str = "R$ 169,80",
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    finished_at: datetime | None = None,
    method: str = "PIX",
    checkout_url: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = created_at or DEFAULT_NOW
    status = {
        "PIX_GENERATED": "PENDING",
        "PIX_EXPIRED": "CANCELED",
        "SALE_APPROVED": "APPROVED",
        "SALE_REFUSED": "REFUSED",
        "SALE_REFUNDED": "REFUNDED",
        "SALE_CHARGEBACK": "CHARGEBACK",
    }.get(event, "UNKNOWN")
    payment: dict[str, Any] = {"method": method}
    if event == "PIX_GENERATED":
        payment.update(
            {
                "qrcode": "00020126580014br.gov.bcb.pix0136" + "a" * 100 + "5204000053039865802BR",
                "qrcode_image": "https://example.com/qr.png",
                "expires_at": sp_str(expires_at or created_at + timedelta(minutes=60)),
            }
        )
    elif expires_at is not None:
        payment["expires_at"] = sp_str(expires_at)
    if finished_at is not None:
        payment["finished_at"] = sp_str(finished_at)
    body: dict[str, Any] = {
        "event": event,
        "event_description": event.replace("_", " ").title(),
        "checkout_id": "Q8J1N6K3",
        "sale_id": sale_id,
        "payment_method": method,
        "total_price": total,
        "type": "ONE_TIME",
        "status": status,
        "created_at": sp_str(created_at),
        "customer": {
            "name": name,
            "document": "12345678900",
            "email": "fulano@example.com",
            "phone_number": phone,
        },
        "payment": payment,
        "products": [
            {
                "id": "prod-uuid",
                "offer_id": "offer-uuid",
                "name": "Jornada com Meu Anjo",
                "price": total,
                "is_order_bump": False,
            },
            {
                "id": "bump-uuid",
                "offer_id": "bump-offer",
                "name": "Bônus",
                "price": "R$ 10,00",
                "is_order_bump": True,
            },
        ],
        "utm": {"src": None, "utm_source": None},
    }
    if checkout_url:
        body["checkout_url"] = checkout_url
    if extra:
        body.update(extra)
    return body


def graph_success(wa_id: str = "5511987654321", message_id: str = "wamid.OK") -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "contacts": [{"input": wa_id, "wa_id": wa_id}],
        "messages": [{"id": message_id}],
    }


def graph_error(code: int, message: str = "error", subcode: int | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code}
    if subcode is not None:
        err["error_subcode"] = subcode
    err["error_data"] = {"details": f"details for {code}"}
    err["fbtrace_id"] = "trace"
    return {"error": err}


def meta_status_payload(
    message_id: str,
    status: str,
    *,
    recipient: str = "5511987654321",
    error: dict[str, Any] | None = None,
    ts: int = 1757343600,
) -> dict[str, Any]:
    st: dict[str, Any] = {
        "id": message_id,
        "status": status,
        "timestamp": str(ts),
        "recipient_id": recipient,
        "conversation": {"id": "conv1", "origin": {"type": "utility"}},
        "pricing": {"billable": True, "pricing_model": "PMP", "category": "utility"},
    }
    if error:
        st["errors"] = [error]
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "958025707339789",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "55...",
                                "phone_number_id": "1347340825121720",
                            },
                            "statuses": [st],
                        },
                    }
                ],
            }
        ],
    }


def meta_text_payload(
    wa_id: str,
    text: str,
    *,
    message_id: str = "wamid.IN1",
    name: str = "Fulano",
    ts: int = 1757343600,
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "958025707339789",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "55...",
                                "phone_number_id": "1347340825121720",
                            },
                            "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": message_id,
                                    "timestamp": str(ts),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def meta_button_payload(
    wa_id: str,
    title: str,
    *,
    payload: str | None = None,
    message_id: str = "wamid.BTN1",
    ts: int = 1757343600,
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "958025707339789",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1347340825121720"},
                            "contacts": [{"profile": {"name": "Fulano"}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": message_id,
                                    "timestamp": str(ts),
                                    "type": "button",
                                    "button": {"payload": payload or title, "text": title},
                                    "context": {"from": "55...", "id": "wamid.TEMPLATE"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
