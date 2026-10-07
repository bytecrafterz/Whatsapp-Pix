"""Read-only query helpers for the panel and public pages.

Everything here is side-effect free and returns ORM objects or small
dataclasses; the panel/pages must not write SQL of their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import clock
from app.format import SP_TZ
from app.models import (
    Cart,
    CartJob,
    CartStatus,
    Contact,
    JobState,
    Message,
    MessageDirection,
    MessageStatus,
    Order,
    OrderStatus,
    PostSale,
    PostSaleJob,
    PostSaleStatus,
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


# --- abandoned carts ---------------------------------------------------------------------


@dataclass
class CartMetrics:
    """The Carrinho page's numbers for one period (carts abandoned since ``since``)."""

    abandoned: int  # carts Kirvano reported
    reachable: int  # ... with a usable phone number
    in_sequence: int  # ... for which a message sequence was started
    sent: int  # cart messages Meta accepted
    delivered: int  # ... that reached the phone (delivered or read)
    read: int  # ... that were opened
    clicked: int  # carts whose button was tapped at least once
    recovered: int  # carts bought AFTER receiving a message
    recovered_cents: int  # what those purchases were worth
    purchased_without_message: int  # bought before any message (not counted as recovered)
    scheduled: int  # messages waiting to go out right now
    sent_carts: int  # carts that received at least one message

    @property
    def recovery_rate(self) -> float:
        """Recovered carts over carts that got at least one message (0–100)."""
        return 100.0 * self.recovered / self.sent_carts if self.sent_carts else 0.0


def cart_metrics(
    session: Session, *, since: datetime | None = None, now: datetime | None = None
) -> CartMetrics:
    """Counters for carts abandoned since ``since`` (all time when ``None``)."""
    now = now or clock.utcnow()
    carts = select(Cart.id)
    if since is not None:
        carts = carts.where(Cart.abandoned_at >= since)
    cart_ids = carts.subquery()
    in_period = Cart.id.in_(select(cart_ids.c.id))
    job_in_period = CartJob.cart_id.in_(select(cart_ids.c.id))

    sent_jobs = select(CartJob.id).where(job_in_period, CartJob.state == JobState.SENT.value)
    delivered = (
        select(CartJob.id)
        .join(Message, Message.wa_message_id == CartJob.wa_message_id)
        .where(
            job_in_period,
            CartJob.state == JobState.SENT.value,
            Message.status.in_([MessageStatus.DELIVERED.value, MessageStatus.READ.value]),
        )
    )
    read = (
        select(CartJob.id)
        .join(Message, Message.wa_message_id == CartJob.wa_message_id)
        .where(
            job_in_period,
            CartJob.state == JobState.SENT.value,
            Message.status == MessageStatus.READ.value,
        )
    )
    recovered_cents = session.execute(
        select(func.coalesce(func.sum(Cart.converted_amount_cents), 0)).where(
            in_period, Cart.recovered.is_(True)
        )
    ).scalar_one()
    sent_carts = (
        select(CartJob.cart_id)
        .where(job_in_period, CartJob.state == JobState.SENT.value)
        .distinct()
    )
    return CartMetrics(
        abandoned=_count(session, select(Cart.id).where(in_period)),
        reachable=_count(session, select(Cart.id).where(in_period, Cart.phone_e164.is_not(None))),
        in_sequence=_count(session, select(CartJob.cart_id).where(job_in_period).distinct()),
        sent=_count(session, sent_jobs),
        delivered=_count(session, delivered),
        read=_count(session, read),
        clicked=_count(session, select(Cart.id).where(in_period, Cart.clicks > 0)),
        recovered=_count(session, select(Cart.id).where(in_period, Cart.recovered.is_(True))),
        recovered_cents=int(recovered_cents or 0),
        purchased_without_message=_count(
            session,
            select(Cart.id).where(
                in_period,
                Cart.status == CartStatus.PURCHASED.value,
                Cart.recovered.is_(False),
            ),
        ),
        scheduled=_count(
            session,
            select(CartJob.id).where(job_in_period, CartJob.state == JobState.SCHEDULED.value),
        ),
        sent_carts=_count(session, sent_carts),
    )


def recent_carts(session: Session, limit: int = 50) -> list[Cart]:
    """Latest carts, newest first; ``cart.jobs`` is loaded in step order."""
    return list(
        session.execute(
            select(Cart)
            .options(selectinload(Cart.jobs))
            .order_by(Cart.abandoned_at.desc(), Cart.id.desc())
            .limit(limit)
        ).scalars()
    )


def cart_by_link_token(session: Session, token: str) -> Cart | None:
    if not token:
        return None
    return session.execute(select(Cart).where(Cart.link_token == token)).scalar_one_or_none()


def cart_status_label(status: str | None) -> str:
    return {
        CartStatus.OPEN.value: "abandonado",
        CartStatus.PIX_GENERATED.value: "gerou PIX",
        CartStatus.PURCHASED.value: "comprou",
    }.get(status or "", status or "")


# --- post-sale follow-up -------------------------------------------------------------

# A customer message this soon after the first post-sale message counts as a reply.
REPLY_WINDOW = timedelta(hours=72)


@dataclass
class PostSaleMetrics:
    """The Pós-venda page's numbers for one period (sales approved since ``since``)."""

    sales: int  # approved sales Kirvano reported
    reachable: int  # ... with a usable phone number
    in_sequence: int  # ... for which the follow-up was started
    sent: int  # post-sale messages Meta accepted
    delivered: int  # ... that reached the phone (delivered or read)
    read: int  # ... that were opened
    replied: int  # sales whose customer wrote back within 72 h of the first message
    failed: int  # messages that could not be delivered
    scheduled: int  # messages waiting to go out right now
    reversed: int  # sales later refunded or charged back
    sent_sales: int  # sales that received at least one message

    @property
    def read_rate(self) -> float:
        """Read messages over sent messages (0–100)."""
        return 100.0 * self.read / self.sent if self.sent else 0.0


def post_sale_metrics(
    session: Session, *, since: datetime | None = None, now: datetime | None = None
) -> PostSaleMetrics:
    """Counters for sales approved since ``since`` (all time when ``None``)."""
    now = now or clock.utcnow()
    sales = select(PostSale.id)
    if since is not None:
        sales = sales.where(PostSale.paid_at >= since)
    sale_ids = sales.subquery()
    in_period = PostSale.id.in_(select(sale_ids.c.id))
    job_in_period = PostSaleJob.post_sale_id.in_(select(sale_ids.c.id))
    sent_job = (job_in_period, PostSaleJob.state == JobState.SENT.value)

    def with_status(*statuses: str):
        return (
            select(PostSaleJob.id)
            .join(Message, Message.wa_message_id == PostSaleJob.wa_message_id)
            .where(*sent_job, Message.status.in_(statuses))
        )

    return PostSaleMetrics(
        sales=_count(session, select(PostSale.id).where(in_period)),
        reachable=_count(
            session, select(PostSale.id).where(in_period, PostSale.phone_e164.is_not(None))
        ),
        in_sequence=_count(
            session, select(PostSaleJob.post_sale_id).where(job_in_period).distinct()
        ),
        sent=_count(session, select(PostSaleJob.id).where(*sent_job)),
        delivered=_count(
            session, with_status(MessageStatus.DELIVERED.value, MessageStatus.READ.value)
        ),
        read=_count(session, with_status(MessageStatus.READ.value)),
        replied=_post_sale_replies(session, job_in_period),
        failed=_count(
            session,
            select(PostSaleJob.id).where(job_in_period, PostSaleJob.state == JobState.FAILED.value),
        ),
        scheduled=_count(
            session,
            select(PostSaleJob.id).where(
                job_in_period, PostSaleJob.state == JobState.SCHEDULED.value
            ),
        ),
        reversed=_count(
            session,
            select(PostSale.id).where(
                in_period,
                PostSale.status.in_(
                    [PostSaleStatus.REFUNDED.value, PostSaleStatus.CHARGEBACK.value]
                ),
            ),
        ),
        sent_sales=_count(session, select(PostSaleJob.post_sale_id).where(*sent_job).distinct()),
    )


def _post_sale_replies(session: Session, job_in_period) -> int:
    """Sales whose customer sent us a message within REPLY_WINDOW of the first one.

    Done in Python: a sale is matched to an inbound message by any of three number
    forms, and the volumes (one shop's sales) are small.
    """
    rows = session.execute(
        select(
            PostSale.id,
            PostSale.phone_e164,
            PostSale.phone_alt,
            PostSale.wa_id,
            func.min(PostSaleJob.sent_at),
        )
        .join(PostSaleJob, PostSaleJob.post_sale_id == PostSale.id)
        .where(job_in_period, PostSaleJob.state == JobState.SENT.value)
        .group_by(PostSale.id, PostSale.phone_e164, PostSale.phone_alt, PostSale.wa_id)
    ).all()
    first_sent: dict[int, datetime] = {}
    by_key: dict[str, list[int]] = {}
    for sale_id, e164, alt, wa_id, sent_at in rows:
        if sent_at is None:
            continue
        first_sent[sale_id] = sent_at
        for key in {k for k in (e164, alt, wa_id) if k}:
            by_key.setdefault(key, []).append(sale_id)
    if not first_sent:
        return 0
    keys = sorted(by_key)
    inbound = session.execute(
        select(Message.phone, Message.wa_id, Message.created_at).where(
            Message.direction == MessageDirection.IN.value,
            Message.created_at >= min(first_sent.values()),
            or_(Message.phone.in_(keys), Message.wa_id.in_(keys)),
        )
    ).all()
    replied: set[int] = set()
    for phone, wa_id, created_at in inbound:
        for key in {k for k in (phone, wa_id) if k}:
            for sale_id in by_key.get(key, ()):
                start = first_sent[sale_id]
                if start <= created_at <= start + REPLY_WINDOW:
                    replied.add(sale_id)
    return len(replied)


def recent_post_sales(session: Session, limit: int = 50) -> list[PostSale]:
    """Latest approved sales, newest first; ``sale.jobs`` is loaded in step order."""
    return list(
        session.execute(
            select(PostSale)
            .options(selectinload(PostSale.jobs))
            .order_by(PostSale.paid_at.desc(), PostSale.id.desc())
            .limit(limit)
        ).scalars()
    )


def post_sale_by_sale_id(session: Session, sale_id: str) -> PostSale | None:
    if not sale_id:
        return None
    return session.execute(select(PostSale).where(PostSale.sale_id == sale_id)).scalar_one_or_none()


def post_sale_status_label(status: str | None) -> str:
    return {
        PostSaleStatus.ACTIVE.value: "venda aprovada",
        PostSaleStatus.REFUNDED.value: "reembolsada",
        PostSaleStatus.CHARGEBACK.value: "chargeback",
    }.get(status or "", status or "")


# Kept for callers that want a SQL expression of "job is active".
JOB_ACTIVE = case(
    (RecoveryJob.state.in_([JobState.SCHEDULED.value, JobState.SENDING.value]), 1), else_=0
)
