"""compute_run_at / quiet hours / worker claim re-checks / requeue backoff."""

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select

from app.format import SP_TZ
from app.inbound import record_outbound_message, upsert_template_status
from app.kirvano import handle_event
from app.models import JobState, Order, RecoveryJob
from app.optout import add_opt_out
from app.scheduling import (
    backoff_delay,
    claim_due_jobs,
    compute_run_at,
    in_quiet_hours,
    quiet_hours_end,
    reap_stale_sending,
    requeue,
)
from tests.conftest import DEFAULT_NOW, kirvano_payload

Q_START, Q_END = time(22, 0), time(8, 0)


def sp(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=SP_TZ).astimezone(UTC)


# --- pure functions ----------------------------------------------------------------------


def test_in_quiet_hours_overnight_and_same_day():
    assert in_quiet_hours(sp(2026, 9, 8, 23, 30), Q_START, Q_END)
    assert in_quiet_hours(sp(2026, 9, 8, 7, 59), Q_START, Q_END)
    assert in_quiet_hours(sp(2026, 9, 8, 22, 0), Q_START, Q_END)
    assert not in_quiet_hours(sp(2026, 9, 8, 8, 0), Q_START, Q_END)
    assert not in_quiet_hours(sp(2026, 9, 8, 12, 0), Q_START, Q_END)
    assert in_quiet_hours(sp(2026, 9, 8, 13, 30), time(13, 0), time(14, 0))
    assert not in_quiet_hours(sp(2026, 9, 8, 14, 0), time(13, 0), time(14, 0))
    assert not in_quiet_hours(sp(2026, 9, 8, 23, 0), time(9, 0), time(9, 0))  # disabled


def test_quiet_hours_end():
    assert quiet_hours_end(sp(2026, 9, 8, 23, 30), Q_START, Q_END) == sp(2026, 9, 9, 8, 0)
    assert quiet_hours_end(sp(2026, 9, 9, 3, 0), Q_START, Q_END) == sp(2026, 9, 9, 8, 0)
    assert quiet_hours_end(sp(2026, 9, 8, 13, 30), time(13, 0), time(14, 0)) == sp(
        2026, 9, 8, 14, 0
    )


def test_compute_run_at_plain_delay():
    d = compute_run_at(DEFAULT_NOW, 10, DEFAULT_NOW + timedelta(hours=1), Q_START, Q_END)
    assert d.run_at == DEFAULT_NOW + timedelta(minutes=10)
    assert not d.clamped_to_expiry and not d.postponed_for_quiet


def test_compute_run_at_clamps_and_skips():
    d = compute_run_at(DEFAULT_NOW, 10, DEFAULT_NOW + timedelta(minutes=8), Q_START, Q_END)
    assert d.run_at == DEFAULT_NOW + timedelta(minutes=5) and d.clamped_to_expiry
    d = compute_run_at(DEFAULT_NOW, 10, DEFAULT_NOW + timedelta(minutes=3), Q_START, Q_END)
    assert d.run_at is None and d.reason == "expires_too_soon"
    d = compute_run_at(DEFAULT_NOW, 10, None, Q_START, Q_END)
    assert d.run_at == DEFAULT_NOW + timedelta(minutes=10)


def test_compute_run_at_postpones_quiet_hours():
    now = sp(2026, 9, 8, 21, 55)
    d = compute_run_at(now, 10, now + timedelta(hours=12), Q_START, Q_END)
    assert d.postponed_for_quiet
    assert d.run_at == sp(2026, 9, 9, 8, 0)


def test_backoff_delay():
    assert backoff_delay(1) == timedelta(seconds=60)
    assert backoff_delay(2) == timedelta(seconds=120)
    assert backoff_delay(3) == timedelta(seconds=240)
    assert backoff_delay(10) == timedelta(seconds=900)


# --- claim protocol ------------------------------------------------------------------------


def _schedule(session, settings, **kw) -> tuple[Order, RecoveryJob]:
    handle_event(session, kirvano_payload(**kw), settings=settings)
    order = session.execute(
        select(Order).where(Order.sale_id == kw.get("sale_id", "D2RP8RQ7"))
    ).scalar_one()
    job = session.execute(select(RecoveryJob).where(RecoveryJob.order_id == order.id)).scalar_one()
    return order, job


def test_claim_flips_due_job_to_sending(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []  # not due yet
    frozen_clock.advance(minutes=10)
    claimed = claim_due_jobs(session, store, now=frozen_clock.now)
    assert len(claimed) == 1
    assert claimed[0].job.id == job.id and claimed[0].order.id == order.id
    session.refresh(job)
    assert job.state == JobState.SENDING.value
    assert job.claimed_at == frozen_clock.now
    # A second claim must not return it again.
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []


def test_worker_skips_when_paid_between_schedule_and_fire(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings)
    # Simulate the race: the order became paid but (hypothetically) the job was not cancelled.
    order.status = "paid"
    session.commit()
    frozen_clock.advance(minutes=10)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "order_paid"


def test_worker_skips_expired(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings, expires_at=DEFAULT_NOW + timedelta(minutes=30))
    order.pix_expires_at = DEFAULT_NOW + timedelta(minutes=10, seconds=30)  # < now+60s at fire time
    session.commit()
    frozen_clock.advance(minutes=10)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "expired"


def test_worker_skips_order_expired_status(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings)
    order.status = "expired"
    session.commit()
    frozen_clock.advance(minutes=10)
    claim_due_jobs(session, store, now=frozen_clock.now)
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "order_expired"


def test_worker_skips_opted_out(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings)
    add_opt_out(session, phone=order.phone_alt, wa_id=None, source="manual")
    session.commit()
    frozen_clock.advance(minutes=10)
    claim_due_jobs(session, store, now=frozen_clock.now)
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "opted_out"


def test_worker_postpones_for_quiet_hours_at_fire_time(session, settings, store, frozen_clock):
    start = sp(2026, 9, 8, 21, 45)
    frozen_clock.set(start)
    order, job = _schedule(
        session, settings, created_at=start, expires_at=start + timedelta(hours=12)
    )
    assert job.run_at == start + timedelta(minutes=10)  # 21:55, still outside quiet hours
    frozen_clock.set(sp(2026, 9, 8, 22, 5))  # worker only gets to it inside quiet hours
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == "scheduled" and job.reason == "quiet_hours"
    assert job.run_at == sp(2026, 9, 9, 8, 0)


def test_worker_skips_on_daily_limit(session, settings, store, frozen_clock):
    store.set("daily_recipient_limit", 1)
    session.commit()
    record_outbound_message(
        session,
        wa_id="5521999990000",
        phone="5521999990000",
        message_id="wamid.prev",
        kind="template",
        body="x",
        template_name="pix_pendente_v2",
        now=DEFAULT_NOW - timedelta(hours=2),
    )
    session.commit()
    order, job = _schedule(session, settings)
    frozen_clock.advance(minutes=10)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "daily_limit"


def test_daily_limit_does_not_count_same_recipient_twice(session, settings, store, frozen_clock):
    store.set("daily_recipient_limit", 1)
    session.commit()
    record_outbound_message(
        session,
        wa_id="5511987654321",
        phone="551187654321",
        message_id="wamid.prev",
        kind="template",
        body="x",
        template_name="pix_pendente_v2",
        now=DEFAULT_NOW - timedelta(hours=2),
    )
    session.commit()
    _schedule(session, settings, phone="5511987654321")
    frozen_clock.advance(minutes=10)
    assert len(claim_due_jobs(session, store, now=frozen_clock.now)) == 1


def test_worker_skips_when_template_paused(session, settings, store, frozen_clock):
    upsert_template_status(session, store.template_name, store.template_language, status="PAUSED")
    session.commit()
    order, job = _schedule(session, settings)
    frozen_clock.advance(minutes=10)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "template_unavailable"


def test_worker_postpones_when_disabled(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings)
    store.set("enabled", False)
    session.commit()
    frozen_clock.advance(minutes=10)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == "scheduled" and job.reason == "disabled"
    assert job.run_at == frozen_clock.now + timedelta(seconds=60)


def test_requeue_backoff_then_fail_then_expiry(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings, expires_at=DEFAULT_NOW + timedelta(hours=2))
    now = frozen_clock.now
    requeue(session, job, order, error_code="130429", error_text="rate", now=now)
    assert (
        job.state == "scheduled" and job.attempts == 1 and job.run_at == now + timedelta(seconds=60)
    )
    requeue(session, job, order, error_code="130429", error_text="rate", now=now)
    assert (
        job.state == "scheduled"
        and job.attempts == 2
        and job.run_at == now + timedelta(seconds=120)
    )
    requeue(session, job, order, error_code="130429", error_text="rate", now=now)
    assert job.state == "failed" and job.reason == "max_attempts" and job.attempts == 3

    order2, job2 = _schedule(
        session, settings, sale_id="S2", expires_at=DEFAULT_NOW + timedelta(minutes=30)
    )
    requeue(
        session,
        job2,
        order2,
        error_code="131049",
        error_text="x",
        now=now,
        delay=timedelta(hours=24),
    )
    assert job2.state == "skipped" and job2.reason == "expires_too_soon"


def test_reap_stale_sending(session, settings, store, frozen_clock):
    order, job = _schedule(session, settings)
    frozen_clock.advance(minutes=10)
    claim_due_jobs(session, store, now=frozen_clock.now)
    assert reap_stale_sending(session, now=frozen_clock.now) == 0
    frozen_clock.advance(minutes=11)
    assert reap_stale_sending(session, now=frozen_clock.now) == 1
    session.refresh(job)
    assert job.state == "failed" and job.reason == "stale_sending"


# --- the race guarantee -------------------------------------------------------------------


def test_payment_during_send_cannot_recall_an_in_flight_job(session, settings, store, frozen_clock):
    """Once the claim commits (state='sending'), the HTTP call is in flight.

    The webhook still marks the order paid, but it must NOT rewrite the job: the
    message is already on its way and "sent" is the honest record. The opposite
    ordering (payment first) is covered by test_worker_skips_when_paid_between_*.
    """
    order, job = _schedule(session, settings)
    frozen_clock.advance(minutes=10)
    assert len(claim_due_jobs(session, store, now=frozen_clock.now)) == 1
    session.refresh(job)
    assert job.state == JobState.SENDING.value

    handle_event(
        session,
        kirvano_payload("SALE_APPROVED", created_at=frozen_clock.now, finished_at=frozen_clock.now),
        settings=settings,
    )
    session.refresh(order)
    session.refresh(job)
    assert order.status == "paid" and order.paid_at == frozen_clock.now
    assert job.state == JobState.SENDING.value and job.reason is None


def test_claim_is_a_no_op_once_the_webhook_cancelled_the_job(
    session, settings, store, frozen_clock
):
    """Payment first: the job is cancelled inside the webhook transaction, so the
    worker finds nothing due — the normal (non-racy) ordering."""
    order, job = _schedule(session, settings)
    handle_event(
        session,
        kirvano_payload("SALE_APPROVED", created_at=DEFAULT_NOW + timedelta(minutes=1)),
        settings=settings,
    )
    session.refresh(job)
    assert job.state == JobState.CANCELLED.value and job.reason == "paid"
    frozen_clock.advance(minutes=10)
    assert claim_due_jobs(session, store, now=frozen_clock.now) == []
    session.refresh(job)
    assert job.state == JobState.CANCELLED.value


def test_only_one_job_per_order_ever(session, settings, store, frozen_clock):
    """The unique constraint on recovery_jobs.order_id is the "one nudge, ever" guarantee."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    order, job = _schedule(session, settings)
    session.add(
        RecoveryJob(
            order_id=order.id,
            run_at=frozen_clock.now,
            state=JobState.SCHEDULED.value,
            attempts=0,
            created_at=frozen_clock.now,
            updated_at=frozen_clock.now,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
