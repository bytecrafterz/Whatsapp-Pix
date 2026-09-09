"""ORM models — every table from docs/SPEC.md "Data model" plus two small
operational tables (``worker_heartbeat`` for /health, ``alerts`` for the panel).

Datetime columns use :class:`UTCDateTime`, which guarantees that whatever the
backend (SQLite drops tzinfo; Postgres ``timestamptz`` keeps it) Python code
always sees **aware UTC** datetimes and never accepts naive ones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator[datetime]):
    """Aware-UTC datetime that round-trips identically on SQLite and Postgres."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime bound to UTCDateTime column")
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            # SQLite stores text; strip tzinfo so the stored string is a fixed-width
            # "YYYY-MM-DD HH:MM:SS.ffffff" that compares lexicographically == chronologically.
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect):  # type: ignore[override]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


# --- enums (stored as plain strings) ----------------------------------------------


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    REFUSED = "refused"
    REFUNDED = "refunded"
    CHARGEBACK = "chargeback"
    UNKNOWN = "unknown"


# Statuses that must never be downgraded by a late/duplicated PIX_GENERATED.
TERMINAL_PAID_STATUSES = frozenset(
    {OrderStatus.PAID.value, OrderStatus.REFUNDED.value, OrderStatus.CHARGEBACK.value}
)


class JobState(StrEnum):
    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    FAILED = "failed"


class MessageDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MessageKind(StrEnum):
    TEMPLATE = "template"
    TEXT = "text"


class MessageStatus(StrEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    RECEIVED = "received"


class OptOutSource(StrEnum):
    BUTTON = "button"
    TEXT = "text"
    MANUAL = "manual"
    META_131050 = "meta_131050"


# --- tables ---------------------------------------------------------------------------


class WebhookEvent(Base):
    """Every raw webhook payload we receive (Kirvano and Meta)."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        # Idempotency key: source + (event, sale_id, created_at) folded into one string,
        # so a NULL created_at cannot defeat the uniqueness check.
        UniqueConstraint("source", "external_key", name="uq_webhook_events_source_key"),
        Index("ix_webhook_events_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # kirvano | meta
    external_key: Mapped[str] = mapped_column(String(255))
    event: Mapped[str | None] = mapped_column(String(64), index=True)
    sale_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict | None] = mapped_column(JSONType)
    headers_meta: Mapped[dict | None] = mapped_column(JSONType)
    auth_debug: Mapped[dict | None] = mapped_column(JSONType)  # header NAMES only, never values
    received_at: Mapped[datetime]
    processed_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_phone_e164", "phone_e164"),
        Index("ix_orders_phone_alt", "phone_alt"),
        Index("ix_orders_wa_id", "wa_id"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[str] = mapped_column(String(64), unique=True)
    checkout_id: Mapped[str | None] = mapped_column(String(64))
    offer_id: Mapped[str | None] = mapped_column(String(64))
    product_name: Mapped[str | None] = mapped_column(String(255))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    customer_email: Mapped[str | None] = mapped_column(String(255))
    # LGPD data minimisation: `customer.document` in the Kirvano payload is the CPF.
    # The column exists because the spec's data model lists it, but NOTHING may write
    # to it — the parser drops the CPF before it ever reaches this layer, and
    # tests/test_kirvano_fixture.py asserts the column stays NULL. Do not populate it.
    customer_document: Mapped[str | None] = mapped_column(String(32))
    phone_raw: Mapped[str | None] = mapped_column(String(32))
    phone_e164: Mapped[str | None] = mapped_column(String(20))  # 13-digit BR form (with 9)
    phone_alt: Mapped[str | None] = mapped_column(String(20))  # 12-digit BR form (without 9)
    wa_id: Mapped[str | None] = mapped_column(String(20))  # canonical id returned by Meta
    amount_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="BRL")
    pix_code: Mapped[str | None] = mapped_column(Text)
    pix_qr_image_url: Mapped[str | None] = mapped_column(Text)
    pix_expires_at: Mapped[datetime | None]
    checkout_recovery_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.PENDING.value)
    paid_at: Mapped[datetime | None]
    # Opt-in evidence: the checkout IP Kirvano sends plus the event timestamp — proof of
    # which consent notice was shown, when and from where, if Meta or a customer ever
    # disputes consent. IPv6-safe length.
    consent_ip: Mapped[str | None] = mapped_column(String(45))
    consent_at: Mapped[datetime | None]
    page_token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    job: Mapped[RecoveryJob | None] = relationship(back_populates="order", uselist=False)

    @property
    def phone_variants(self) -> list[str]:
        return [p for p in (self.phone_e164, self.phone_alt) if p]


class RecoveryJob(Base):
    """At most ONE reminder per order, ever — enforced by the unique order_id."""

    __tablename__ = "recovery_jobs"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_recovery_jobs_order_id"),
        Index("ix_recovery_jobs_state_run_at", "state", "run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    run_at: Mapped[datetime]
    state: Mapped[str] = mapped_column(String(16), default=JobState.SCHEDULED.value)
    reason: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    sent_to: Mapped[str | None] = mapped_column(String(20))  # number actually used
    wa_message_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    order: Mapped[Order] = relationship(back_populates="job")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_wa_id_created", "wa_id", "created_at"),
        Index("ix_messages_order_id", "order_id"),
        Index("ix_messages_out_created", "direction", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    wa_id: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str | None] = mapped_column(String(20))
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # template | text
    body: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(16))
    status_updated_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class OptOut(Base):
    """One row per phone variant (13- and 12-digit) so lookups by either form hit."""

    __tablename__ = "opt_outs"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_opt_outs_phone"),
        Index("ix_opt_outs_wa_id", "wa_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_id: Mapped[str | None] = mapped_column(String(20))
    phone: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(16))  # button | text | manual | meta_131050
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class TemplateStatus(Base):
    """Last known Meta status/category of a template (from webhooks or the Graph API)."""

    __tablename__ = "template_status"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str | None] = mapped_column(String(32))  # APPROVED | PAUSED | ...
    category: Mapped[str | None] = mapped_column(String(32))  # UTILITY | MARKETING | ...
    reason: Mapped[str | None] = mapped_column(Text)
    template_id: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime]


class Contact(Base):
    __tablename__ = "contacts"

    wa_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    profile_name: Mapped[str | None] = mapped_column(String(255))
    last_inbound_at: Mapped[datetime | None]
    last_outbound_at: Mapped[datetime | None]


class WorkerHeartbeat(Base):
    """Singleton row (id=1) touched every worker loop; /health reports its age."""

    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    beat_at: Mapped[datetime]
    pid: Mapped[int | None] = mapped_column(Integer)
    hostname: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(String(255))


class Alert(Base):
    """Operator-facing alerts (token invalid, template paused, code bugs...)."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(8), default="error")  # info | warning | error
    code: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
