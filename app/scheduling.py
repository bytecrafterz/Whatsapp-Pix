"""Reminder scheduling and the worker-side claim protocol.

This module is the heart of the "never nudge someone who already paid" promise.

Race-condition guarantees (Postgres; SQLite is single-writer so it is trivially safe):

1. The Kirvano webhook that marks an order paid/expired/refused does, in ONE
   transaction: ``SELECT ... FROM orders WHERE sale_id=? FOR UPDATE`` → update the
   status → cancel the order's *scheduled* job → COMMIT.
2. The worker, in ONE transaction, for each due job: ``SELECT ... FROM orders
   WHERE id=? FOR UPDATE`` → ``SELECT ... FROM recovery_jobs WHERE id=? FOR UPDATE
   SKIP LOCKED`` → re-check EVERY condition (job still scheduled, order still
   pending, not opted out, not expiring, not quiet hours, daily limit, template
   available) → flip the job to ``sending`` → COMMIT. Only after that commit does
   it call the Graph API.

Because both paths lock the **order row first**, they serialise on it: either
the payment commits first (and the worker then sees ``status != pending`` and
skips), or the worker's ``sending`` flip commits first (and the webhook finds no
``scheduled`` job to cancel — the message is already on its way, which is the
only outcome physics allows). Taking the order lock before the job lock on
*both* sides also removes the lock-ordering deadlock that would exist if the
worker locked jobs first with SKIP LOCKED and then waited on the order.

``SKIP LOCKED`` on the job row lets several worker processes run concurrently
without double-sending; SQLAlchemy silently drops ``FOR UPDATE`` on SQLite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.format import SP_TZ
from app.models import (
    JobState,
    Message,
    MessageDirection,
    MessageKind,
    Order,
    OrderStatus,
    RecoveryJob,
    TemplateStatus,
)
from app.optout import is_opted_out
from app.phone import normalize_br
from app.settings_store import SettingsStore

log = logging.getLogger(__name__)

# Schedule-time clamp: fire at least this long before the PIX expires.
EXPIRY_CLAMP_MARGIN = timedelta(minutes=3)
# Fire-time guard: never send when the PIX expires within this window.
FIRE_EXPIRY_MARGIN = timedelta(seconds=60)
# A job stuck in "sending" longer than this had its worker die mid-request.
STALE_SENDING_AFTER = timedelta(minutes=10)
DEFAULT_MAX_ATTEMPTS = 3
# When the panel switch is off at fire time, re-check this often (keeps jobs alive
# until expiry instead of burning the one-and-only reminder).
DISABLED_RECHECK = timedelta(seconds=60)

# Template statuses (Meta) under which we must not send — only the ones Meta itself
# refuses to deliver on. FLAGGED and PENDING_DELETION are deliberately NOT here:
# FLAGGED is a quality WARNING (the template still sends, Meta is telling us it will
# pause it if quality keeps dropping) and a template pending deletion still sends
# until it is actually deleted. Blocking on either would silently kill the whole
# recovery flow on a launch day for no Meta-side reason.
TEMPLATE_BLOCKING_STATUSES = frozenset({"PAUSED", "DISABLED", "REJECTED", "DELETED", "IN_APPEAL"})
# Stored and shown in the panel, and worth an alert, but sends continue.
TEMPLATE_WARNING_STATUSES = frozenset({"FLAGGED", "PENDING_DELETION"})


# --- quiet hours -----------------------------------------------------------------


def in_quiet_hours(dt: datetime, quiet_start: time, quiet_end: time, tz: ZoneInfo = SP_TZ) -> bool:
    """True when ``dt`` (aware) falls inside [quiet_start, quiet_end) local time.

    Supports overnight windows (22:00–08:00) and same-day windows (13:00–14:00);
    ``quiet_start == quiet_end`` disables quiet hours.
    """
    if quiet_start == quiet_end:
        return False
    local = dt.astimezone(tz).time().replace(second=0, microsecond=0)
    if quiet_start < quiet_end:
        return quiet_start <= local < quiet_end
    return local >= quiet_start or local < quiet_end


def quiet_hours_end(
    dt: datetime, quiet_start: time, quiet_end: time, tz: ZoneInfo = SP_TZ
) -> datetime:
    """The next moment quiet hours end for a ``dt`` that is inside them (aware UTC)."""
    local = dt.astimezone(tz)
    end_today = datetime.combine(local.date(), quiet_end, tzinfo=tz)
    if quiet_start < quiet_end or local.time() < quiet_end:
        end = end_today
    else:
        end = end_today + timedelta(days=1)
    return end.astimezone(UTC)


# --- run_at computation ----------------------------------------------------------


@dataclass(frozen=True)
class RunAtDecision:
    run_at: datetime | None
    reason: str | None = None  # "expires_too_soon" when run_at is None
    clamped_to_expiry: bool = False
    postponed_for_quiet: bool = False


def compute_run_at(
    now: datetime,
    delay_minutes: int,
    expires_at: datetime | None,
    quiet_start: time,
    quiet_end: time,
    tz: ZoneInfo = SP_TZ,
) -> RunAtDecision:
    """Spec algorithm: now+delay → clamp to expiry-3min → postpone out of quiet hours."""
    run_at = now + timedelta(minutes=delay_minutes)
    clamped = False
    if expires_at is not None:
        latest = expires_at - EXPIRY_CLAMP_MARGIN
        if run_at > latest:
            if latest <= now:
                return RunAtDecision(None, "expires_too_soon")
            run_at = latest
            clamped = True
    postponed = False
    if in_quiet_hours(run_at, quiet_start, quiet_end, tz):
        # Fire-time checks will skip it if the PIX expired meanwhile (spec).
        run_at = quiet_hours_end(run_at, quiet_start, quiet_end, tz)
        postponed = True
    return RunAtDecision(run_at, None, clamped, postponed)


# --- schedule / cancel -------------------------------------------------------------

ScheduleAction = Literal["created", "exists", "skipped", "disabled", "opted_out", "no_phone"]


@dataclass
class ScheduleOutcome:
    action: ScheduleAction
    job: RecoveryJob | None
    reason: str | None = None


def _job_for_order(session: Session, order: Order, *, lock: bool = False) -> RecoveryJob | None:
    stmt = select(RecoveryJob).where(RecoveryJob.order_id == order.id)
    if lock:
        # populate_existing: a locking read MUST return what the row says right now.
        # Without it SQLAlchemy would hand back the instance already in this session's
        # identity map (expire_on_commit=False keeps it unexpired) and the re-check
        # would run against a stale copy — exactly the race we are locking against.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.execute(stmt).scalar_one_or_none()


def schedule_for_order(
    session: Session,
    order: Order,
    store: SettingsStore,
    *,
    now: datetime | None = None,
    tz: ZoneInfo = SP_TZ,
) -> ScheduleOutcome:
    """Create the single recovery job for ``order`` (caller holds the order lock).

    Does not commit. A job row is created in state ``skipped`` for
    ``expires_too_soon`` so the panel can show why nothing was sent.
    """
    now = now or clock.utcnow()
    existing = _job_for_order(session, order)
    if existing is not None:
        return ScheduleOutcome("exists", existing)
    if not store.enabled:
        return ScheduleOutcome("disabled", None, "disabled")
    if not order.phone_variants:
        return ScheduleOutcome("no_phone", None, "no_phone")
    if is_opted_out(session, order.phone_variants, order.wa_id):
        return ScheduleOutcome("opted_out", None, "opted_out")

    decision = compute_run_at(
        now, store.delay_minutes, order.pix_expires_at, store.quiet_start, store.quiet_end, tz
    )
    if decision.run_at is None:
        job = RecoveryJob(
            order_id=order.id,
            run_at=now,
            state=JobState.SKIPPED.value,
            reason=decision.reason,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        return ScheduleOutcome("skipped", job, decision.reason)

    reason = None
    if decision.postponed_for_quiet:
        reason = "quiet_hours"
    elif decision.clamped_to_expiry:
        reason = "clamped_to_expiry"
    job = RecoveryJob(
        order_id=order.id,
        run_at=decision.run_at,
        state=JobState.SCHEDULED.value,
        reason=reason,
        attempts=0,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    log.info("job scheduled order=%s run_at=%s reason=%s", order.sale_id, job.run_at, reason)
    return ScheduleOutcome("created", job, reason)


def cancel_for_order(
    session: Session, order: Order, reason: str, *, now: datetime | None = None
) -> RecoveryJob | None:
    """Cancel the order's job if it is still ``scheduled`` (caller holds the order lock).

    A job already in ``sending`` cannot be recalled — the HTTP call is in flight.
    """
    now = now or clock.utcnow()
    job = _job_for_order(session, order, lock=True)
    if job is None or job.state != JobState.SCHEDULED.value:
        return None
    job.state = JobState.CANCELLED.value
    job.reason = reason
    job.updated_at = now
    session.flush()
    log.info("job cancelled order=%s reason=%s", order.sale_id, reason)
    return job


# --- worker side -------------------------------------------------------------------


@dataclass
class ClaimedJob:
    job: RecoveryJob
    order: Order


def _recipient_key(phone: str | None) -> str | None:
    if not phone:
        return None
    forms = normalize_br(phone)
    return forms.primary if forms else phone


def recipients_last_24h(session: Session, now: datetime) -> set[str]:
    """Distinct recipients of template messages in the rolling 24h window.

    Includes jobs currently ``sending`` (claimed but not yet recorded) so a batch
    cannot overshoot the limit between claim and record.
    """
    since = now - timedelta(hours=24)
    phones = session.execute(
        select(Message.phone).where(
            Message.direction == MessageDirection.OUT.value,
            Message.kind == MessageKind.TEMPLATE.value,
            Message.created_at > since,
        )
    ).scalars()
    keys = {k for k in (_recipient_key(p) for p in phones) if k}
    in_flight = session.execute(
        select(Order.phone_e164, Order.phone_alt)
        .join(RecoveryJob, RecoveryJob.order_id == Order.id)
        .where(RecoveryJob.state == JobState.SENDING.value)
    ).all()
    for e164, alt in in_flight:
        k = _recipient_key(e164 or alt)
        if k:
            keys.add(k)
    return keys


def template_available(session: Session, store: SettingsStore) -> tuple[bool, str | None]:
    """(ok, reason): configured name and not known to be paused/disabled/rejected."""
    name = store.template_name.strip()
    if not name:
        return False, "template_not_configured"
    row = session.get(TemplateStatus, (name, store.template_language))
    if row is not None and row.status and row.status.upper() in TEMPLATE_BLOCKING_STATUSES:
        return False, "template_unavailable"
    return True, None


def claim_due_jobs(
    session: Session,
    store: SettingsStore,
    *,
    now: datetime | None = None,
    limit: int = 20,
    tz: ZoneInfo = SP_TZ,
) -> list[ClaimedJob]:
    """Atomically claim due jobs (see module docstring). Commits the claim transaction.

    Every job that fails a re-check is marked ``skipped`` (or postponed for quiet
    hours / disabled switch) inside the same transaction, so it is never
    re-evaluated unnecessarily.
    """
    now = now or clock.utcnow()
    candidates = session.execute(
        select(RecoveryJob.id, RecoveryJob.order_id)
        .where(RecoveryJob.state == JobState.SCHEDULED.value, RecoveryJob.run_at <= now)
        # `id` breaks the tie: jobs scheduled in the same tick share an identical
        # run_at, and without a TOTAL order two workers could walk the same
        # candidates in opposite directions — worker A holding order 5 waiting on
        # order 7 while B holds 7 waiting on 5 is a deadlock (the ORDER lock below
        # is a plain blocking FOR UPDATE; only the job lock uses SKIP LOCKED).
        .order_by(RecoveryJob.run_at, RecoveryJob.id)
        .limit(limit)
    ).all()
    if not candidates:
        session.commit()
        return []

    recipients = recipients_last_24h(session, now)
    daily_limit = store.daily_recipient_limit
    template_ok, template_reason = template_available(session, store)
    claimed: list[ClaimedJob] = []

    for job_id, order_id in candidates:
        # Lock ORDER first, then the job (same order as the webhook path — no deadlock).
        # Both reads use populate_existing so the re-check below sees the CURRENT row,
        # never a cached instance from an earlier tick of this session.
        order = session.execute(
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        job = session.execute(
            select(RecoveryJob)
            .where(RecoveryJob.id == job_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if job is None or order is None:
            continue  # another worker holds it, or it vanished
        if job.state != JobState.SCHEDULED.value or job.run_at > now:
            continue  # changed between the candidate scan and the lock

        # --- full re-check under lock (spec list) ---------------------------------
        if not store.enabled:
            postpone(session, job, now + DISABLED_RECHECK, reason="disabled", now=now)
            continue
        if order.status != OrderStatus.PENDING.value:
            mark_skipped(session, job, f"order_{order.status}", now=now)
            continue
        if is_opted_out(session, order.phone_variants, order.wa_id):
            mark_skipped(session, job, "opted_out", now=now)
            continue
        if order.pix_expires_at is not None and order.pix_expires_at <= now + FIRE_EXPIRY_MARGIN:
            mark_skipped(session, job, "expired", now=now)
            continue
        if in_quiet_hours(now, store.quiet_start, store.quiet_end, tz):
            postpone(
                session,
                job,
                quiet_hours_end(now, store.quiet_start, store.quiet_end, tz),
                reason="quiet_hours",
                now=now,
            )
            continue
        if not template_ok:
            mark_skipped(session, job, template_reason or "template_unavailable", now=now)
            continue
        to = order.phone_e164 or order.phone_alt
        if not to:
            mark_skipped(session, job, "no_phone", now=now)
            continue
        key = _recipient_key(to) or to
        if key not in recipients:
            if len(recipients) >= daily_limit:
                # Spec: skip with reason daily_limit — never queue past the limit.
                mark_skipped(session, job, "daily_limit", now=now)
                continue
            recipients.add(key)

        job.state = JobState.SENDING.value
        job.claimed_at = now
        job.updated_at = now
        claimed.append(ClaimedJob(job=job, order=order))

    session.commit()
    return claimed


def reap_stale_sending(session: Session, *, now: datetime | None = None) -> int:
    """Fail jobs stuck in ``sending`` (worker died mid-request).

    We cannot know whether Meta accepted the message, and "one reminder per order,
    ever" forbids a resend, so the job is failed rather than requeued.
    """
    now = now or clock.utcnow()
    cutoff = now - STALE_SENDING_AFTER
    stale = (
        session.execute(
            select(RecoveryJob)
            .where(RecoveryJob.state == JobState.SENDING.value, RecoveryJob.claimed_at < cutoff)
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


# --- state transitions (no commit; the worker commits) ------------------------------


def mark_sent(
    session: Session,
    job: RecoveryJob,
    *,
    wa_id: str | None,
    message_id: str | None,
    sent_to: str,
    now: datetime | None = None,
) -> None:
    now = now or clock.utcnow()
    job.state = JobState.SENT.value
    job.sent_at = now
    job.sent_to = sent_to
    job.wa_message_id = message_id
    job.attempts = (job.attempts or 0) + 1
    job.error_code = None
    job.error_text = None
    job.reason = None
    job.updated_at = now
    session.flush()


def mark_failed(
    session: Session,
    job: RecoveryJob,
    *,
    reason: str,
    error_code: str | None = None,
    error_text: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or clock.utcnow()
    job.state = JobState.FAILED.value
    job.reason = reason
    job.error_code = error_code
    job.error_text = error_text
    job.attempts = (job.attempts or 0) + 1
    job.updated_at = now
    session.flush()


def mark_skipped(
    session: Session, job: RecoveryJob, reason: str, *, now: datetime | None = None
) -> None:
    now = now or clock.utcnow()
    job.state = JobState.SKIPPED.value
    job.reason = reason
    job.updated_at = now
    session.flush()


def postpone(
    session: Session,
    job: RecoveryJob,
    run_at: datetime,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """Keep the job ``scheduled`` but move ``run_at`` (quiet hours, disabled switch, token)."""
    now = now or clock.utcnow()
    job.state = JobState.SCHEDULED.value
    job.run_at = run_at
    job.claimed_at = None
    if reason:
        job.reason = reason
    job.updated_at = now
    session.flush()


def backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff: 60s, 120s, 240s, ... capped at 15 minutes."""
    return timedelta(seconds=min(60 * (2 ** max(attempts - 1, 0)), 900))


def requeue(
    session: Session,
    job: RecoveryJob,
    order: Order,
    *,
    error_code: str | None,
    error_text: str | None,
    now: datetime | None = None,
    delay: timedelta | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RecoveryJob:
    """Retryable error: count the attempt and reschedule, re-checking expiry.

    Ends in ``failed`` (max attempts) or ``skipped`` (would fire after expiry).
    """
    now = now or clock.utcnow()
    job.attempts = (job.attempts or 0) + 1
    job.error_code = error_code
    job.error_text = error_text
    job.claimed_at = None
    job.updated_at = now
    if job.attempts >= max_attempts:
        job.state = JobState.FAILED.value
        job.reason = "max_attempts"
        session.flush()
        return job
    run_at = now + (delay if delay is not None else backoff_delay(job.attempts))
    if order.pix_expires_at is not None and run_at >= order.pix_expires_at - FIRE_EXPIRY_MARGIN:
        job.state = JobState.SKIPPED.value
        job.reason = "expires_too_soon"
        session.flush()
        return job
    job.state = JobState.SCHEDULED.value
    job.run_at = run_at
    job.reason = f"retry_{error_code}" if error_code else "retry"
    session.flush()
    return job
