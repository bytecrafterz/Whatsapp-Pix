"""Post-sale follow-up: Kirvano ``SALE_APPROVED`` → 1–3 messages after the purchase.

Flow
----
1. :func:`handle_sale_approved` (called by ``kirvano.handle_event`` inside its
   transaction, after the order and cart paths) stores one ``post_sales`` row per
   ``sale_id`` — card and PIX sales alike — and, unless something rules it out, one
   ``post_sale_jobs`` row per enabled step. Kirvano repeating the event never starts a
   second sequence.
2. :func:`on_sale_reversed` (``SALE_REFUNDED`` / ``SALE_CHARGEBACK``): whatever is
   still waiting is cancelled — no "how is the product going?" after a refund.
3. :func:`claim_due_post_sale_jobs` (worker) is the claim protocol of ``app.cart`` with
   the POST_SALE row in the role of the cart row: lock the sale, lock the job
   ``SKIP LOCKED``, re-check everything, flip to ``sending``, commit, and only then
   call Meta.

Lock order: the Kirvano webhook holds an ORDER lock, then CART locks, then the
post-sale lock; the worker and the opt-out path take a POST_SALE lock and then that
sale's JOB locks. Nothing takes an order or cart lock while holding a post-sale lock.

What these messages are for: confirming the purchase, how to reach the product, a
check-in a few days later. They are meant to go out as Utility templates; an offer or
a coupon in one makes Meta file it as Marketing (docs/TEMPLATE.md §10).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.cart import cart_template_ok, phone_forms
from app.format import SP_TZ
from app.models import JobState, PostSale, PostSaleJob, PostSaleStatus
from app.optout import is_opted_out
from app.phone import normalize_br
from app.scheduling import (
    DISABLED_RECHECK,
    STALE_SENDING_AFTER,
    _recipient_key,
    backoff_delay,
    in_quiet_hours,
    mark_skipped,
    postpone,
    quiet_hours_end,
    recipients_last_24h,
)
from app.settings_store import SettingsStore, StepConfig

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime (kirvano imports us)
    from app.kirvano import EventResult, KirvanoPayload

log = logging.getLogger(__name__)

EVENT_SALE_APPROVED = "SALE_APPROVED"
# Events that end the follow-up, and the status they leave the sale in.
REVERSAL_EVENTS: dict[str, str] = {
    "SALE_REFUNDED": PostSaleStatus.REFUNDED.value,
    "SALE_CHARGEBACK": PostSaleStatus.CHARGEBACK.value,
}

# A step is never sent later than this after its configured time (quiet hours,
# retries, the switch being off for a while): stored per job as ``deadline_at``.
LATE_TOLERANCE = timedelta(hours=24)
# Two messages of one sequence are never closer than this, whatever the timing.
MIN_STEP_GAP = timedelta(minutes=60)
# A step waiting for the previous one re-checks this often.
PREVIOUS_STEP_RECHECK = timedelta(minutes=5)
MAX_METHOD_LEN = 32


# --- locking ------------------------------------------------------------------------


def _lock_sale(session: Session, post_sale_id: int) -> PostSale | None:
    """SELECT ... FOR UPDATE on a post-sale row, re-read from the row."""
    return session.execute(
        select(PostSale)
        .where(PostSale.id == post_sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _lock_sale_by_sale_id(session: Session, sale_id: str) -> PostSale | None:
    return session.execute(
        select(PostSale)
        .where(PostSale.sale_id == sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _insert_or_lock_sale(
    session: Session, sale_id: str, paid_at: datetime, now: datetime
) -> PostSale:
    """Create the row for ``sale_id``, or lock the one a concurrent delivery just made.

    Same SAVEPOINT pattern as ``kirvano._insert_or_lock_order``: two simultaneous
    first deliveries both find nothing to lock and both INSERT; the loser rolls back
    only its own INSERT and locks the winner's row.
    """
    sale = PostSale(
        sale_id=sale_id,
        status=PostSaleStatus.ACTIVE.value,
        paid_at=paid_at,
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(sale)
            session.flush()
        return sale
    except IntegrityError:
        log.info("concurrent insert for post-sale %s — using the existing row", sale_id)
    existing = _lock_sale_by_sale_id(session, sale_id)
    if existing is None:  # pragma: no cover - only if the row vanished again
        raise RuntimeError(f"post-sale {sale_id} disappeared after a duplicate-key insert")
    return existing


# --- starting the sequence ----------------------------------------------------------------


def _apply_payload(sale: PostSale, payload: KirvanoPayload) -> None:
    """Copy what the event carries onto the row; never blank a field already known."""
    c = payload.customer
    if c.name:
        sale.customer_name = c.name
    if c.email:
        sale.customer_email = c.email
    if c.phone_number:
        sale.phone_raw = c.phone_number
        forms = normalize_br(c.phone_number)
        if forms:
            sale.phone_e164 = forms.primary
            sale.phone_alt = forms.alternate
    if payload.checkout_id:
        sale.checkout_id = payload.checkout_id
    if payload.offer_id:
        sale.offer_id = payload.offer_id
    if payload.product_name:
        sale.product_name = payload.product_name
    if payload.amount_cents is not None:
        sale.amount_cents = payload.amount_cents
    method = payload.payment.method or payload.payment_method
    if method:
        sale.payment_method = method[:MAX_METHOD_LEN]


def start_blocker(
    session: Session, sale: PostSale, store: SettingsStore, now: datetime
) -> str | None:
    """Why no sequence may start for ``sale`` (stored on ``sale.reason``), or ``None``."""
    if not store.post_enabled:
        return "disabled"
    if sale.status != PostSaleStatus.ACTIVE.value:
        return f"sale_{sale.status}"
    if not sale.phone_variants:
        return "no_phone"
    if is_opted_out(session, sale.phone_variants, sale.wa_id):
        return "opted_out"
    return None


def compute_post_run_at(
    paid_at: datetime,
    delay_minutes: int,
    now: datetime,
    store: SettingsStore,
    tz: ZoneInfo = SP_TZ,
) -> tuple[datetime, datetime, bool]:
    """``(run_at, deadline_at, postponed_for_quiet)`` for one step."""
    due = paid_at + timedelta(minutes=delay_minutes)
    run_at = max(due, now)
    quiet = in_quiet_hours(run_at, store.quiet_start, store.quiet_end, tz)
    if quiet:
        run_at = quiet_hours_end(run_at, store.quiet_start, store.quiet_end, tz)
    return run_at, due + LATE_TOLERANCE, quiet


def schedule_post_sale_jobs(
    session: Session, sale: PostSale, store: SettingsStore, now: datetime, tz: ZoneInfo = SP_TZ
) -> list[PostSaleJob]:
    """One job per enabled step whose moment has not passed (caller holds the lock)."""
    jobs: list[PostSaleJob] = []
    for cfg in store.post_step_configs():
        run_at, deadline, quiet = compute_post_run_at(
            sale.paid_at, cfg.delay_minutes, now, store, tz
        )
        if deadline <= now:
            continue  # the event reached us too late for this step
        job = PostSaleJob(
            post_sale_id=sale.id,
            step=cfg.step,
            run_at=run_at,
            deadline_at=deadline,
            state=JobState.SCHEDULED.value,
            reason="quiet_hours" if quiet else None,
            attempts=0,
            template_name=cfg.template_name or None,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        jobs.append(job)
    session.flush()
    return jobs


def handle_sale_approved(
    session: Session,
    payload: KirvanoPayload,
    store: SettingsStore,
    now: datetime,
    result: EventResult,
) -> PostSale | None:
    """Store the approved sale and start its follow-up when allowed. Does not commit."""
    if not payload.sale_id:
        return None
    # The approval time, not the sale's creation: a boleto created days earlier would
    # otherwise have every step "already late" on arrival. Unknown → when we heard.
    paid_at = min(payload.payment.finished_at or now, now)
    sale = _lock_sale_by_sale_id(session, payload.sale_id)
    if sale is None:
        sale = _insert_or_lock_sale(session, payload.sale_id, paid_at, now)
    _apply_payload(sale, payload)
    sale.updated_at = now
    session.flush()
    result.post_sale_id = sale.id

    has_jobs = session.execute(select(exists().where(PostSaleJob.post_sale_id == sale.id))).scalar()
    if has_jobs:
        return sale
    blocker = start_blocker(session, sale, store, now)
    if blocker:
        sale.reason = blocker
        log.info("post-sale %s stored without sequence: %s", sale.sale_id, blocker)
        return sale
    jobs = schedule_post_sale_jobs(session, sale, store, now)
    sale.reason = None if jobs else "sale_too_old"
    if jobs and result.outcome == "ignored":
        # A card sale the PIX flow had no use for: the follow-up is what happened to it.
        result.outcome = "processed"
        result.reason = f"post_sale_scheduled_{len(jobs)}"
    log.info(
        "post-sale %s sequence scheduled steps=%s first=%s",
        sale.sale_id,
        len(jobs),
        jobs[0].run_at if jobs else None,
    )
    return sale


# --- stopping it ----------------------------------------------------------------------------


def cancel_post_sale_jobs(
    session: Session, sale: PostSale, reason: str, *, now: datetime | None = None
) -> list[PostSaleJob]:
    """Cancel the sale's still-scheduled steps (caller holds the post-sale lock)."""
    now = now or clock.utcnow()
    jobs = list(
        session.execute(
            select(PostSaleJob)
            .where(
                PostSaleJob.post_sale_id == sale.id,
                PostSaleJob.state == JobState.SCHEDULED.value,
            )
            .order_by(PostSaleJob.step)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars()
    )
    for job in jobs:
        job.state = JobState.CANCELLED.value
        job.reason = reason
        job.updated_at = now
    session.flush()
    return jobs


def on_sale_reversed(session: Session, payload: KirvanoPayload, now: datetime) -> PostSale | None:
    """``SALE_REFUNDED`` / ``SALE_CHARGEBACK``: end the sale's follow-up."""
    status = REVERSAL_EVENTS.get(payload.event)
    if status is None or not payload.sale_id:
        return None
    sale = _lock_sale_by_sale_id(session, payload.sale_id)
    if sale is None:
        return None
    sale.status = status
    sale.updated_at = now
    cancelled = cancel_post_sale_jobs(session, sale, status, now=now)
    log.info("post-sale %s %s: %s message(s) cancelled", sale.sale_id, status, len(cancelled))
    return sale


def cancel_post_sale_jobs_for_phones(
    session: Session,
    phones: Iterable[str | None],
    wa_id: str | None,
    *,
    reason: str = "opted_out",
    now: datetime | None = None,
) -> list[PostSaleJob]:
    """Cancel every scheduled post-sale step for this phone/wa_id (the opt-out path)."""
    now = now or clock.utcnow()
    forms = phone_forms([*phones, wa_id])
    conds = []
    if forms:
        conds += [PostSale.phone_e164.in_(forms), PostSale.phone_alt.in_(forms)]
    if wa_id:
        conds.append(PostSale.wa_id == wa_id)
    if not conds:
        return []
    # Unlocked scan first, then SALE lock before JOB lock (see cart.cancel_cart_jobs_for_phones).
    candidates = session.execute(
        select(PostSaleJob.id, PostSaleJob.post_sale_id)
        .join(PostSale, PostSale.id == PostSaleJob.post_sale_id)
        .where(PostSaleJob.state == JobState.SCHEDULED.value, or_(*conds))
        .order_by(PostSaleJob.post_sale_id, PostSaleJob.id)
    ).all()
    cancelled: list[PostSaleJob] = []
    for job_id, post_sale_id in candidates:
        session.execute(
            select(PostSale.id).where(PostSale.id == post_sale_id).with_for_update()
        ).first()
        job = session.execute(
            select(PostSaleJob)
            .where(PostSaleJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if job is None or job.state != JobState.SCHEDULED.value:
            continue
        job.state = JobState.CANCELLED.value
        job.reason = reason
        job.updated_at = now
        cancelled.append(job)
    session.flush()
    return cancelled


# --- worker side ----------------------------------------------------------------------------


@dataclass
class ClaimedPostSaleJob:
    job: PostSaleJob
    sale: PostSale
    step: StepConfig


def _previous_step_wait(session: Session, job: PostSaleJob, now: datetime) -> datetime | None:
    """When step ``job.step`` must wait for the previous one, the time to look again.

    Unlike a cart, a step does not depend on the previous one having gone out: a
    check-in a week later is worth sending even if the day-one message was skipped.
    It only never overtakes it, and never lands within an hour of it.
    """
    if job.step <= 1:
        return None
    prev = session.execute(
        select(PostSaleJob).where(
            PostSaleJob.post_sale_id == job.post_sale_id, PostSaleJob.step == job.step - 1
        )
    ).scalar_one_or_none()
    if prev is None:
        return None
    if prev.state in (JobState.SCHEDULED.value, JobState.SENDING.value):
        return max(prev.run_at, now) + PREVIOUS_STEP_RECHECK
    if prev.state == JobState.SENT.value and prev.sent_at is not None:
        earliest = prev.sent_at + MIN_STEP_GAP
        return earliest if now < earliest else None
    return None


def claim_due_post_sale_jobs(
    session: Session,
    store: SettingsStore,
    *,
    now: datetime | None = None,
    limit: int = 20,
    tz: ZoneInfo = SP_TZ,
) -> list[ClaimedPostSaleJob]:
    """Claim due post-sale steps under lock (see module docstring). Commits the claim."""
    now = now or clock.utcnow()
    candidates = session.execute(
        select(PostSaleJob.id, PostSaleJob.post_sale_id)
        .where(PostSaleJob.state == JobState.SCHEDULED.value, PostSaleJob.run_at <= now)
        .order_by(PostSaleJob.run_at, PostSaleJob.id)  # total order: no cross-worker deadlock
        .limit(limit)
    ).all()
    if not candidates:
        session.commit()
        return []

    recipients = recipients_last_24h(session, now)
    daily_limit = store.daily_recipient_limit
    claimed: list[ClaimedPostSaleJob] = []

    for job_id, post_sale_id in candidates:
        sale = _lock_sale(session, post_sale_id)
        job = session.execute(
            select(PostSaleJob)
            .where(PostSaleJob.id == job_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if job is None or sale is None:
            continue
        if job.state != JobState.SCHEDULED.value or job.run_at > now:
            continue

        too_late = now > job.deadline_at
        if not store.post_enabled:
            if too_late:
                mark_skipped(session, job, "too_late", now=now)
            else:
                postpone(session, job, now + DISABLED_RECHECK, reason="disabled", now=now)
            continue
        if sale.status != PostSaleStatus.ACTIVE.value:
            mark_skipped(session, job, f"sale_{sale.status}", now=now)
            continue
        if too_late:
            mark_skipped(session, job, "too_late", now=now)
            continue
        if job.step > store.post_steps:
            mark_skipped(session, job, "step_disabled", now=now)
            continue
        if is_opted_out(session, sale.phone_variants, sale.wa_id):
            mark_skipped(session, job, "opted_out", now=now)
            cancel_post_sale_jobs(session, sale, "opted_out", now=now)
            continue
        if in_quiet_hours(now, store.quiet_start, store.quiet_end, tz):
            until = quiet_hours_end(now, store.quiet_start, store.quiet_end, tz)
            postpone(session, job, until, reason="quiet_hours", now=now)
            continue
        step = store.post_step(job.step)
        ok, why = cart_template_ok(session, step)
        if not ok:
            mark_skipped(session, job, why or "template_unavailable", now=now)
            continue
        wait_until = _previous_step_wait(session, job, now)
        if wait_until is not None:
            postpone(session, job, wait_until, reason="waiting_previous", now=now)
            continue
        to = sale.phone_e164 or sale.phone_alt
        if not to:
            mark_skipped(session, job, "no_phone", now=now)
            continue
        key = _recipient_key(to) or to
        if key not in recipients:
            if len(recipients) >= daily_limit:
                mark_skipped(session, job, "daily_limit", now=now)
                continue
            recipients.add(key)

        job.state = JobState.SENDING.value
        job.claimed_at = now
        job.template_name = step.template_name
        job.updated_at = now
        claimed.append(ClaimedPostSaleJob(job=job, sale=sale, step=step))

    session.commit()
    return claimed


def requeue_post_sale_job(
    session: Session,
    job: PostSaleJob,
    sale: PostSale,
    *,
    error_code: str | None,
    error_text: str | None,
    now: datetime,
    max_attempts: int,
) -> PostSaleJob:
    """Retryable error: count the attempt and reschedule, unless that would be too late."""
    job.attempts = (job.attempts or 0) + 1
    job.error_code = error_code
    job.error_text = error_text
    job.claimed_at = None
    job.updated_at = now
    if job.attempts >= max_attempts:
        job.state = JobState.FAILED.value
        job.reason = "max_attempts"
    else:
        run_at = now + backoff_delay(job.attempts)
        if run_at > job.deadline_at:
            job.state = JobState.SKIPPED.value
            job.reason = "too_late"
        else:
            job.state = JobState.SCHEDULED.value
            job.run_at = run_at
            job.reason = f"retry_{error_code}" if error_code else "retry"
    session.flush()
    return job


def reap_stale_post_sale_sending(session: Session, *, now: datetime | None = None) -> int:
    """Fail post-sale steps stuck in ``sending`` (worker died mid-request). Commits.

    Same rule as ``scheduling.reap_stale_sending``: Meta may already have delivered
    it, so a stuck step is failed, never resent.
    """
    now = now or clock.utcnow()
    stale = (
        session.execute(
            select(PostSaleJob)
            .where(
                PostSaleJob.state == JobState.SENDING.value,
                PostSaleJob.claimed_at < now - STALE_SENDING_AFTER,
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for job in stale:
        job.state = JobState.FAILED.value
        job.reason = "stale_sending"
        job.error_text = "worker died while sending; outcome unknown"
        job.updated_at = now
    session.commit()
    return len(stale)
