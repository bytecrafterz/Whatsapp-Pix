"""/health, settings_store round-trips, query helpers, include_extra_routers hook."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Setting, WorkerHeartbeat
from app.queries import dashboard_counts, recent_orders, start_of_today_sp
from app.settings_store import SettingsStore, SettingValueError
from tests.conftest import DEFAULT_NOW, kirvano_payload


def test_health_reports_db_and_worker(client, session, frozen_clock):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] == "ok" and body["worker"] == "stale" and body["status"] == "degraded"
    assert body["worker_heartbeat_age_s"] is None

    session.merge(
        WorkerHeartbeat(id=1, beat_at=frozen_clock.now - timedelta(seconds=5), pid=1, hostname="t")
    )
    session.commit()
    body = client.get("/health").json()
    assert (
        body["status"] == "ok" and body["worker"] == "ok" and body["worker_heartbeat_age_s"] == 5.0
    )


def test_settings_store_defaults_and_roundtrip(session, settings):
    store = SettingsStore(session, settings)
    assert store.enabled is True
    assert store.delay_minutes == 10
    assert store.quiet_start.strftime("%H:%M") == "22:00"
    assert store.quiet_end.strftime("%H:%M") == "08:00"
    assert store.daily_recipient_limit == 250
    assert store.template_name == "pix_pendente_v2"
    assert store.template_language == "pt_BR"
    assert store.url_button_index == 1
    assert store.template_params == ["first_name", "sale_id", "amount", "expiry"]
    assert store.checkout_url == "https://pay.kirvano.com/checkout-uuid"
    assert store.get_raw("delay_minutes") is None

    store.set("delay_minutes", "15")
    store.set("enabled", "off")
    store.set("quiet_start", "23:30")
    session.commit()
    fresh = SettingsStore(session, settings)
    assert fresh.delay_minutes == 15 and fresh.enabled is False
    assert fresh.quiet_start.strftime("%H:%M") == "23:30"
    assert (
        session.execute(select(Setting).where(Setting.key == "delay_minutes")).scalar_one().value
        == "15"
    )
    assert fresh.as_dict()["delay_minutes"] == "15"


def test_settings_store_validation(session, settings):
    store = SettingsStore(session, settings)
    with pytest.raises(SettingValueError):
        store.set("delay_minutes", "abc")
    with pytest.raises(SettingValueError):
        store.set("delay_minutes", -1)
    with pytest.raises(SettingValueError):
        store.set("quiet_start", "25:00")
    with pytest.raises(SettingValueError):
        store.set("template_name", "")
    with pytest.raises(SettingValueError):
        store.set("url_button_index", 12)
    with pytest.raises(KeyError):
        store.set("not_a_key", "x")
    # set_many validates everything before writing anything.
    with pytest.raises(SettingValueError):
        store.set_many({"delay_minutes": "20", "quiet_end": "bad"})
    assert store.delay_minutes == 10


def test_dashboard_counts_and_recent_orders(session, settings, frozen_clock):
    from app.kirvano import handle_event

    handle_event(session, kirvano_payload(sale_id="A"), settings=settings)
    handle_event(session, kirvano_payload(sale_id="B"), settings=settings)
    handle_event(
        session,
        kirvano_payload(
            "SALE_APPROVED", sale_id="B", created_at=DEFAULT_NOW + timedelta(minutes=1)
        ),
        settings=settings,
    )
    counts = dashboard_counts(session, now=frozen_clock.now)
    assert counts.pending_now == 1 and counts.scheduled == 1 and counts.cancelled_paid == 1
    assert counts.sent_today == 0 and counts.failed == 0
    rows = recent_orders(session, limit=10)
    assert [o.sale_id for o, _ in rows] == ["B", "A"] or {o.sale_id for o, _ in rows} == {"A", "B"}
    assert all(j is not None for _, j in rows)
    assert start_of_today_sp(DEFAULT_NOW).isoformat() == "2026-09-08T03:00:00+00:00"


def test_include_extra_routers_skips_missing_modules(settings, monkeypatch):
    from fastapi import FastAPI

    from app import main as main_module
    from app.main import include_extra_routers

    # A module that does not exist must be skipped, not crash the app: the core has
    # to boot even when the optional panel/pages modules are absent. (Both now exist,
    # so the list is patched to an unimportable name instead of relying on that.)
    monkeypatch.setattr(main_module, "EXTRA_ROUTER_MODULES", ("app.modulo_inexistente",))
    app = FastAPI()
    assert include_extra_routers(app) == []


def test_utc_datetime_rejects_naive(session, settings):
    from datetime import datetime

    from app.models import Alert

    session.add(Alert(code="x", message="m", created_at=datetime(2026, 1, 1)))
    with pytest.raises(Exception):  # noqa: B017 - StatementError wrapping ValueError
        session.flush()
    session.rollback()


def test_include_extra_routers_mounts_a_module_that_exposes_router(settings):
    """The contract other builders rely on: define `router` (and optionally `setup(app)`)."""
    import sys
    import types

    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from app.main import include_extra_routers

    module = types.ModuleType("app.panel")
    router = APIRouter()

    @router.get("/painel/ping")
    def _ping() -> dict[str, bool]:
        return {"ok": True}

    module.router = router
    module.setup = lambda app: setattr(app.state, "panel_setup_called", True)
    sys.modules["app.panel"] = module
    try:
        app = FastAPI()
        # app.pages is a real module and mounts too; only the fake one matters here.
        assert "app.panel" in include_extra_routers(app)
        assert app.state.panel_setup_called is True
        with TestClient(app) as c:
            assert c.get("/painel/ping").json() == {"ok": True}
    finally:
        del sys.modules["app.panel"]
