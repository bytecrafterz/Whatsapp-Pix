"""Read-only query helpers for the panel and public pages.

Everything here is side-effect free and returns ORM objects or small
dataclasses; the panel/pages must not write SQL of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.format import SP_TZ
from app.models import (
    Contact,
    JobState,
    Message,
    Order,
    OrderStatus,
    RecoveryJob,
    TemplateStatus,
    WebhookEvent,
    WorkerHeartbeat,
)
from app.settings_store import SettingsStore


@dataclass
class DashboardCounts:
    pending_now: int
    scheduled: int
    sent_today: int
    sent_7d: int
    paid_after_reminder: int
    cancelled_paid: int
    expired: int
    failed: int


def _count(session: Session, stmt) -> int:
    return int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())


def start_of_today_sp(now: datetime) -> datetime:
    local = now.astimezone(SP_TZ)
    return datetime.combine(local.date(), time(0, 0), tzinfo=SP_TZ).astimezone(UTC)


def dashboard_counts(session: Session, *, now: datetime | None = None) -> DashboardCounts:
    now = now or clock.utcnow()
    today = start_of_today_sp(now)
    week = now - timedelta(days=7)
    pending = select(Order.id).where(
        Order.status == OrderStatus.PENDING.value,
        or_(Order.pix_expires_at.is_(None), Order.pix_expires_at > now),
    )
    scheduled = select(RecoveryJob.id).where(RecoveryJob.state == JobState.SCHEDULED.value)
    sent_today = select(RecoveryJob.id).where(
        RecoveryJob.state == JobState.SENT.value, RecoveryJob.sent_at >= today
    )
    sent_7d = select(RecoveryJob.id).where(
        RecoveryJob.state == JobState.SENT.value, RecoveryJob.sent_at >= week
    )
    paid_after = (
        select(RecoveryJob.id)
        .join(Order, Order.id == RecoveryJob.order_id)
        .where(
            RecoveryJob.state == JobState.SENT.value,
            Order.status == OrderStatus.PAID.value,
            Order.paid_at.is_not(None),
            Order.paid_at > RecoveryJob.sent_at,
        )
    )
    cancelled_paid = select(RecoveryJob.id).where(
        RecoveryJob.state == JobState.CANCELLED.value, RecoveryJob.reason == OrderStatus.PAID.value
    )
    expired = select(Order.id).where(Order.status == OrderStatus.EXPIRED.value)
    failed = select(RecoveryJob.id).where(RecoveryJob.state == JobState.FAILED.value)
    return DashboardCounts(
        pending_now=_count(session, pending),
        scheduled=_count(session, scheduled),
        sent_today=_count(session, sent_today),
        sent_7d=_count(session, sent_7d),
        paid_after_reminder=_count(session, paid_after),
        cancelled_paid=_count(session, cancelled_paid),
        expired=_count(session, expired),
        failed=_count(session, failed),
    )


def recent_orders(session: Session, limit: int = 50) -> list[tuple[Order, RecoveryJob | None]]:
    """Latest orders with their (optional) job, newest first."""
    rows = session.execute(
        select(Order, RecoveryJob)
        .outerjoin(RecoveryJob, RecoveryJob.order_id == Order.id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    ).all()
    return [(o, j) for o, j in rows]


def order_by_page_token(session: Session, token: str) -> Order | None:
    if not token:
        return None
    return session.execute(select(Order).where(Order.page_token == token)).scalar_one_or_none()


def order_by_sale_id(session: Session, sale_id: str) -> Order | None:
    return session.execute(select(Order).where(Order.sale_id == sale_id)).scalar_one_or_none()


def job_for_order(session: Session, order: Order) -> RecoveryJob | None:
    return session.execute(
        select(RecoveryJob).where(RecoveryJob.order_id == order.id)
    ).scalar_one_or_none()


def recent_events(
    session: Session, limit: int = 100, source: str | None = None
) -> list[WebhookEvent]:
    stmt = select(WebhookEvent).order_by(WebhookEvent.received_at.desc(), WebhookEvent.id.desc())
    if source:
        stmt = stmt.where(WebhookEvent.source == source)
    return list(session.execute(stmt.limit(limit)).scalars())


def contact_last_activity():
    """SQL expression for "the later of last_inbound_at / last_outbound_at".

    Deliberately NOT ``func.max(a, b)``: on SQLite that is a scalar two-argument
    function (so the tests passed), but on PostgreSQL ``max`` is a one-argument
    AGGREGATE — the panel's Conversas page raised
    ``function max(timestamptz, timestamptz) does not exist`` on the real database
    while every unit test stayed green. ``CASE``/``COALESCE`` mean the same thing
    on both backends. See ``tests/test_queries_dialect.py``.
    """
    return case(
        (
            Contact.last_inbound_at.is_(None),
            Contact.last_outbound_at,
        ),
        (
            Contact.last_outbound_at.is_(None),
            Contact.last_inbound_at,
        ),
        (
            Contact.last_inbound_at > Contact.last_outbound_at,
            Contact.last_inbound_at,
        ),
        else_=Contact.last_outbound_at,
    )


def conversations(session: Session, limit: int = 100) -> list[Contact]:
    """Contacts ordered by most recent activity (inbound or outbound)."""
    last = contact_last_activity()
    return list(
        session.execute(
            select(Contact).order_by(last.desc().nullslast(), Contact.wa_id).limit(limit)
        ).scalars()
    )


def conversation_messages(session: Session, wa_id: str, limit: int = 200) -> list[Message]:
    """Messages exchanged with ``wa_id`` (by wa_id or the same phone), oldest first."""
    contact = session.get(Contact, wa_id)
    conds = [Message.wa_id == wa_id]
    if contact and contact.phone:
        conds.append(Message.phone == contact.phone)
    return list(
        session.execute(
            select(Message)
            .where(or_(*conds))
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(limit)
        ).scalars()
    )


def last_message_for(session: Session, wa_id: str) -> Message | None:
    return session.execute(
        select(Message)
        .where(Message.wa_id == wa_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def template_status_for(session: Session, store: SettingsStore) -> TemplateStatus | None:
    return session.get(TemplateStatus, (store.template_name, store.template_language))


def worker_heartbeat_age(session: Session, *, now: datetime | None = None) -> float | None:
    """Seconds since the worker's last heartbeat, or ``None`` if it never beat."""
    now = now or clock.utcnow()
    row = session.get(WorkerHeartbeat, 1)
    if row is None or row.beat_at is None:
        return None
    return max(0.0, (now - row.beat_at).total_seconds())


def job_state_label(state: str | None) -> str:
    """pt-BR label for a job state (for the panel)."""
    return {
        None: "sem lembrete",
        JobState.SCHEDULED.value: "agendado",
        JobState.SENDING.value: "enviando",
        JobState.SENT.value: "enviado",
        JobState.CANCELLED.value: "cancelado",
        JobState.SKIPPED.value: "ignorado",
        JobState.FAILED.value: "falhou",
    }.get(state, state or "")


def order_status_label(status: str | None) -> str:
    return {
        OrderStatus.PENDING.value: "pendente",
        OrderStatus.PAID.value: "pago",
        OrderStatus.EXPIRED.value: "expirado",
        OrderStatus.REFUSED.value: "recusado",
        OrderStatus.REFUNDED.value: "reembolsado",
        OrderStatus.CHARGEBACK.value: "chargeback",
        OrderStatus.UNKNOWN.value: "desconhecido",
    }.get(status or "", status or "")


# Kept for callers that want a SQL expression of "job is active".
JOB_ACTIVE = case(
    (RecoveryJob.state.in_([JobState.SCHEDULED.value, JobState.SENDING.value]), 1), else_=0
)
