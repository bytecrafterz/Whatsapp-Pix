"""PIX_GENERATED → order + job; status events → cancel; idempotency; quiet hours."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.format import SP_TZ
from app.kirvano import handle_event
from app.models import JobState, Order, RecoveryJob, WebhookEvent
from app.optout import add_opt_out
from tests.conftest import DEFAULT_NOW, kirvano_payload


def _order(session, sale_id="D2RP8RQ7") -> Order:
    return session.execute(select(Order).where(Order.sale_id == sale_id)).scalar_one()


def _job(session, order: Order) -> RecoveryJob | None:
    return session.execute(
        select(RecoveryJob).where(RecoveryJob.order_id == order.id)
    ).scalar_one_or_none()


def test_pix_generated_creates_order_and_job_at_now_plus_delay(session, settings, frozen_clock):
    res = handle_event(session, kirvano_payload(), {"x-kirvano-token": "x"}, settings=settings)
    assert res.outcome == "processed"
    assert res.reason == "created"
    order = _order(session)
    assert order.status == "pending"
    assert order.phone_raw == "5511987654321"
    assert order.phone_e164 == "5511987654321"
    assert order.phone_alt == "551187654321"
    assert order.amount_cents == 16980
    assert order.product_name == "Jornada com Meu Anjo"
    assert order.offer_id == "offer-uuid"
    assert order.customer_name == "Fulano de Tal"
    assert order.pix_code and order.pix_code.startswith("00020126")
    assert order.pix_qr_image_url == "https://example.com/qr.png"
    assert order.pix_expires_at == DEFAULT_NOW + timedelta(minutes=60)
    assert order.pix_expires_at.tzinfo is not None
    assert len(order.page_token) >= 16
    job = _job(session, order)
    assert job is not None
    assert job.state == JobState.SCHEDULED.value
    assert job.run_at == DEFAULT_NOW + timedelta(minutes=10)
    assert job.attempts == 0
    events = session.execute(select(WebhookEvent)).scalars().all()
    assert len(events) == 1 and events[0].source == "kirvano" and events[0].outcome == "processed"
    assert events[0].payload["sale_id"] == "D2RP8RQ7"


def test_delay_is_clamped_before_expiry(session, settings, frozen_clock):
    payload = kirvano_payload(expires_at=DEFAULT_NOW + timedelta(minutes=8))
    handle_event(session, payload, settings=settings)
    job = _job(session, _order(session))
    assert job.state == "scheduled"
    assert job.run_at == DEFAULT_NOW + timedelta(minutes=5)  # expiry - 3 min
    assert job.reason == "clamped_to_expiry"


def test_expires_too_soon_is_skipped(session, settings, frozen_clock):
    payload = kirvano_payload(expires_at=DEFAULT_NOW + timedelta(minutes=2))
    res = handle_event(session, payload, settings=settings)
    assert res.reason == "expires_too_soon"
    job = _job(session, _order(session))
    assert job.state == JobState.SKIPPED.value
    assert job.reason == "expires_too_soon"


def test_sale_approved_marks_paid_and_cancels_job(session, settings, frozen_clock):
    handle_event(session, kirvano_payload(), settings=settings)
    frozen_clock.advance(minutes=3)
    paid_at = frozen_clock.now
    res = handle_event(
        session,
        kirvano_payload("SALE_APPROVED", finished_at=paid_at, created_at=paid_at),
        settings=settings,
    )
    assert res.outcome == "processed" and res.reason == "job_cancelled"
    order = _order(session)
    assert order.status == "paid"
    assert order.paid_at == paid_at
    job = _job(session, order)
    assert job.state == JobState.CANCELLED.value
    assert job.reason == "paid"


def test_pix_expired_marks_expired_and_cancels_job(session, settings, frozen_clock):
    handle_event(session, kirvano_payload(), settings=settings)
    frozen_clock.advance(minutes=61)
    res = handle_event(
        session,
        kirvano_payload(
            "PIX_EXPIRED",
            created_at=frozen_clock.now,
            checkout_url="https://pay.kirvano.com/recovery/uuid",
        ),
        settings=settings,
    )
    assert res.outcome == "processed"
    order = _order(session)
    assert order.status == "expired"
    assert order.checkout_recovery_url == "https://pay.kirvano.com/recovery/uuid"
    job = _job(session, order)
    assert job.state == "cancelled" and job.reason == "expired"


def test_refused_refunded_chargeback_cancel(session, settings, frozen_clock):
    for event, status in (
        ("SALE_REFUSED", "refused"),
        ("SALE_REFUNDED", "refunded"),
        ("SALE_CHARGEBACK", "chargeback"),
    ):
        sale = f"S-{status}"
        handle_event(session, kirvano_payload(sale_id=sale), settings=settings)
        handle_event(
            session,
            kirvano_payload(event, sale_id=sale, created_at=DEFAULT_NOW + timedelta(seconds=1)),
            settings=settings,
        )
        order = _order(session, sale)
        assert order.status == status
        assert _job(session, order).state == "cancelled"


def test_duplicate_webhook_is_idempotent(session, settings, frozen_clock):
    payload = kirvano_payload()
    first = handle_event(session, payload, settings=settings)
    second = handle_event(session, payload, settings=settings)
    assert first.outcome == "processed"
    assert second.outcome == "duplicate"
    assert session.execute(select(func.count()).select_from(WebhookEvent)).scalar_one() == 1
    assert session.execute(select(func.count()).select_from(RecoveryJob)).scalar_one() == 1
    assert session.execute(select(func.count()).select_from(Order)).scalar_one() == 1


def test_second_pix_generated_with_new_created_at_keeps_single_job(session, settings, frozen_clock):
    handle_event(session, kirvano_payload(), settings=settings)
    res = handle_event(
        session, kirvano_payload(created_at=DEFAULT_NOW + timedelta(minutes=1)), settings=settings
    )
    assert res.outcome == "processed" and res.reason == "exists"
    assert session.execute(select(func.count()).select_from(RecoveryJob)).scalar_one() == 1
    assert session.execute(select(func.count()).select_from(WebhookEvent)).scalar_one() == 2


def test_late_pix_generated_never_downgrades_a_paid_order(session, settings, frozen_clock):
    handle_event(session, kirvano_payload(), settings=settings)
    handle_event(
        session,
        kirvano_payload("SALE_APPROVED", created_at=DEFAULT_NOW + timedelta(minutes=1)),
        settings=settings,
    )
    res = handle_event(
        session, kirvano_payload(created_at=DEFAULT_NOW + timedelta(minutes=2)), settings=settings
    )
    assert res.outcome == "ignored" and res.reason == "order_already_paid"
    assert _order(session).status == "paid"


def test_unknown_and_ignored_events_are_stored(session, settings, frozen_clock):
    res = handle_event(session, kirvano_payload("SUBSCRIPTION_RENEWED"), settings=settings)
    assert res.outcome == "ignored"
    # No longer ignored: ABANDONED_CART is the cart-recovery trigger (tests/test_cart.py).
    # It creates a cart, never an order.
    res = handle_event(
        session, {"event": "ABANDONED_CART", "checkout_id": "Q8J1N6K3"}, settings=settings
    )
    assert res.outcome == "processed" and res.cart_id is not None
    res = handle_event(session, {"event": "SOMETHING_NEW", "sale_id": "Z1"}, settings=settings)
    assert res.outcome == "unknown"
    assert session.execute(select(func.count()).select_from(WebhookEvent)).scalar_one() == 3
    assert session.execute(select(func.count()).select_from(Order)).scalar_one() == 0


def test_disabled_setting_skips_scheduling(session, settings, store, frozen_clock):
    store.set("enabled", False)
    session.commit()
    res = handle_event(session, kirvano_payload(), settings=settings, store=store)
    assert res.outcome == "processed" and res.reason == "disabled"
    assert _job(session, _order(session)) is None


def test_opted_out_phone_blocks_scheduling(session, settings, frozen_clock):
    add_opt_out(session, phone="551187654321", wa_id=None, source="manual")
    session.commit()
    res = handle_event(session, kirvano_payload(phone="5511987654321"), settings=settings)
    assert res.reason == "opted_out"
    assert _job(session, _order(session)) is None


def test_sale_approved_for_unknown_card_sale_is_ignored(session, settings, frozen_clock):
    res = handle_event(
        session,
        kirvano_payload("SALE_APPROVED", sale_id="CARD1", method="CREDIT_CARD"),
        settings=settings,
    )
    assert res.outcome == "ignored" and res.reason == "unknown_order_not_pix"
    res = handle_event(
        session, kirvano_payload("SALE_APPROVED", sale_id="PIX1", method="PIX"), settings=settings
    )
    assert res.outcome == "processed"
    assert _order(session, "PIX1").status == "paid"


def test_quiet_hours_postpone_at_schedule_time(session, settings, frozen_clock):
    # 23:30 in São Paulo → 02:30 UTC next day: inside 22:00–08:00.
    late = datetime(2026, 9, 8, 23, 30, tzinfo=SP_TZ).astimezone(UTC)
    frozen_clock.set(late)
    payload = kirvano_payload(created_at=late, expires_at=late + timedelta(hours=12))
    handle_event(session, payload, settings=settings)
    job = _job(session, _order(session))
    assert job.state == "scheduled"
    assert job.reason == "quiet_hours"
    assert job.run_at == datetime(2026, 9, 9, 8, 0, tzinfo=SP_TZ).astimezone(UTC)


def test_missing_expiry_uses_configured_fallback(session, settings, frozen_clock, monkeypatch):
    monkeypatch.setattr(settings, "kirvano_pix_expiry_minutes", 30)
    payload = kirvano_payload()
    del payload["payment"]["expires_at"]
    handle_event(session, payload, settings=settings)
    assert _order(session).pix_expires_at == DEFAULT_NOW + timedelta(minutes=30)


def test_processing_error_is_recorded_not_raised(session, settings, frozen_clock, monkeypatch):
    import app.kirvano as kirvano_mod

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(kirvano_mod, "schedule_for_order", boom)
    res = handle_event(session, kirvano_payload(), settings=settings)
    assert res.outcome == "error" and res.reason == "RuntimeError"
    evt = session.execute(select(WebhookEvent)).scalar_one()
    assert evt.outcome == "error" and "kaboom" in evt.error
