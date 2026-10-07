"""Kirvano webhook: payload parsing, token acceptance and event handling.

Everything here is DB-only work (no outbound HTTP) so the endpoint can answer
200 quickly. See ``scheduling`` for the locking protocol that makes the
"paid in the same minute" race impossible on Postgres.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.cart import CONVERSION_EVENTS, EVENT_ABANDONED_CART, handle_abandoned_cart, on_sale_event
from app.config import Settings, get_settings
from app.models import (
    TERMINAL_PAID_STATUSES,
    Order,
    OrderStatus,
    WebhookEvent,
)
from app.phone import normalize_br
from app.scheduling import cancel_for_order, schedule_for_order
from app.settings_store import SettingsStore

log = logging.getLogger(__name__)

EVENT_PIX_GENERATED = "PIX_GENERATED"
# Events that change the order status (and cancel the reminder).
STATUS_EVENTS: dict[str, str] = {
    "SALE_APPROVED": OrderStatus.PAID.value,
    "PIX_EXPIRED": OrderStatus.EXPIRED.value,
    "SALE_REFUSED": OrderStatus.REFUSED.value,
    "SALE_REFUNDED": OrderStatus.REFUNDED.value,
    "SALE_CHARGEBACK": OrderStatus.CHARGEBACK.value,
}
# ABANDONED_CART is handled by app.cart (it used to be ignored here).
IGNORED_EVENT_PREFIXES = ("BANK_SLIP_", "SUBSCRIPTION_")

# Token transport is undocumented; accept any of these (spec).
TOKEN_HEADERS = (
    "x-kirvano-token",
    "token",
    "x-token",
    "x-webhook-token",
    "security-token",
    "authorization",
)
TOKEN_BODY_FIELDS = ("token", "security_token")


# --- parsing helpers ---------------------------------------------------------------

_AMOUNT_KEEP = re.compile(r"[^0-9,.\-]")
_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def parse_amount_brl(text: str | int | float | Decimal | None) -> Decimal | None:
    """Parse ``"R$ 1.169,80"`` / ``"R$ 169,80"`` / ``"169.80"`` / ``169.8`` → ``Decimal``."""
    if text is None:
        return None
    if isinstance(text, bool):
        return None
    if isinstance(text, int | float | Decimal):
        return Decimal(str(text)).quantize(Decimal("0.01"))
    s = _AMOUNT_KEEP.sub("", str(text)).strip()
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:
        # The LAST separator is the decimal one; the other is thousands.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif "." in s and _THOUSANDS_DOT.match(s):
        s = s.replace(".", "")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def brl_to_cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int((value * 100).to_integral_value())


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_naive_local(text: str | None, tz_name: str = "America/Sao_Paulo") -> datetime | None:
    """Kirvano sends naive local ``YYYY-MM-DD HH:MM:SS``; interpret in ``tz_name`` → aware UTC.

    Tolerant: ISO strings with an explicit offset/``Z`` are honoured as-is.
    """
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        iso = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if iso.tzinfo is not None:
            return iso.astimezone(UTC)
        naive = iso
    except ValueError:
        naive = None
        for fmt in _DT_FORMATS:
            try:
                naive = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if naive is None:
            log.warning("unparseable Kirvano timestamp %r", s)
            return None
    return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


# --- typed payload ---------------------------------------------------------------------


@dataclass
class KirvanoCustomer:
    """Customer fields we are allowed to keep.

    ``customer.document`` (the CPF) is deliberately NOT a field: LGPD data
    minimisation means it is dropped at parse time and never reaches the ORM,
    the logs or the stored raw payload (see :func:`redact_payload`).
    """

    name: str | None = None
    email: str | None = None
    phone_number: str | None = None


@dataclass
class KirvanoPayment:
    method: str | None = None
    qrcode: str | None = None
    qrcode_image: str | None = None
    expires_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at_raw: str | None = None
    finished_at_raw: str | None = None

    @property
    def qrcode_image_url(self) -> str | None:
        """``qrcode_image`` only when it really is a URL.

        The REAL payload (tests/fixtures/kirvano_pix_generated.json) repeats the
        EMV copia-e-cola string in ``qrcode_image``, so putting it in an
        ``<img src>`` would render a broken image. The PIX page renders the QR
        server-side from ``qrcode``; we only keep this field when a future
        Kirvano version actually sends an http(s) URL.
        """
        value = (self.qrcode_image or "").strip()
        return value if value.lower().startswith(("http://", "https://")) else None


@dataclass
class KirvanoProduct:
    id: str | None = None
    offer_id: str | None = None
    name: str | None = None
    price: str | None = None
    is_order_bump: bool = False


@dataclass
class KirvanoPayload:
    event: str
    event_description: str | None
    checkout_id: str | None
    sale_id: str | None
    status: str | None
    payment_method: str | None
    type: str | None
    total_price_raw: str | None
    amount_cents: int | None
    created_at: datetime | None
    created_at_raw: str | None
    customer: KirvanoCustomer
    payment: KirvanoPayment
    products: list[KirvanoProduct] = field(default_factory=list)
    checkout_url: str | None = None
    utm: dict | None = None
    ip: str | None = None  # checkout IP — opt-in evidence, stored on the order
    fiscal: dict | None = None
    raw: dict = field(default_factory=dict)

    @property
    def main_product(self) -> KirvanoProduct | None:
        for p in self.products:
            if not p.is_order_bump:
                return p
        return self.products[0] if self.products else None

    @property
    def product_name(self) -> str | None:
        p = self.main_product
        return p.name if p else None

    @property
    def offer_id(self) -> str | None:
        p = self.main_product
        return p.offer_id if p else None

    @property
    def is_pix(self) -> bool:
        m = (self.payment.method or self.payment_method or "").upper()
        return m == "PIX"

    def idempotency_key(self) -> str:
        """(event, sale_id, created_at) folded into one nullable-safe string."""
        ident = self.sale_id or self.checkout_id
        if not ident:
            digest = hashlib.sha256(
                json.dumps(self.raw, sort_keys=True, default=str).encode()
            ).hexdigest()
            ident = f"sha:{digest[:32]}"
        return f"{self.event}|{ident}|{self.created_at_raw or ''}"


# Column widths from app/models.py. SQLite ignores VARCHAR lengths, so an over-long
# value only blows up on PostgreSQL — as a DataError, which is NOT an IntegrityError
# and would escape the duplicate guard below and 500 a publicly reachable endpoint.
# Clipping at parse time keeps the "always answer 200" contract on both backends.
MAX_ID_LEN = 64
MAX_NAME_LEN = 255
MAX_PHONE_RAW_LEN = 32
MAX_IP_LEN = 45


def _str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _clip(value: str | None, max_len: int) -> str | None:
    """Trim a payload string to the width of the column that will hold it."""
    if value is None:
        return None
    return value[:max_len]


def amount_cents_from(raw: Mapping) -> int | None:
    """Amount in cents, preferring the numeric ``fiscal.total_value``.

    The real payload carries BOTH ``fiscal.total_value`` (a NUMBER, 97) and
    ``total_price`` (the formatted string "R$ 97,00"). The number needs no
    locale parsing, so it wins; the string is the fallback for payloads (and
    older events) that have no ``fiscal`` block.
    """
    fiscal = raw.get("fiscal")
    if isinstance(fiscal, Mapping):
        value = fiscal.get("total_value")
        if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
            cents = brl_to_cents(parse_amount_brl(value))
            if cents is not None and cents > 0:
                return cents
    return brl_to_cents(parse_amount_brl(raw.get("total_price")))


# Keys removed from every stored payload. LGPD data minimisation (spec): the CPF and
# the advertising cookies have no use for us and must not sit in the database.
# `token`/`security_token` are the SHARED SECRET when Kirvano transports it in the body
# (one of the two transports extract_token accepts): storing it verbatim would put the
# credential that authenticates PIX events into every DB backup and print it in the
# panel's Eventos page, which renders webhook_events.payload raw.
REDACT_TOP_LEVEL = ("cookies", *TOKEN_BODY_FIELDS)
REDACT_CUSTOMER = ("document",)


def redact_payload(raw: Mapping) -> dict:
    """JSON-safe deep copy of ``raw`` with the CPF, ad cookies and token removed.

    ``webhook_events.payload`` keeps everything else verbatim (the spec wants the
    raw event for debugging), but these fields are dropped BEFORE the row is
    written, so neither the CPF nor the webhook secret ever touches the disk.
    ``check_token`` runs against the ORIGINAL body, so acceptance is unaffected.
    """
    data = json.loads(json.dumps(raw, default=str, ensure_ascii=False))
    if not isinstance(data, dict):
        return {}
    for key in REDACT_TOP_LEVEL:
        data.pop(key, None)
    customer = data.get("customer")
    if isinstance(customer, dict):
        for key in REDACT_CUSTOMER:
            customer.pop(key, None)
    return data


def parse_payload(raw: Mapping, tz_name: str = "America/Sao_Paulo") -> KirvanoPayload:
    """Tolerantly map a Kirvano JSON body into :class:`KirvanoPayload`."""
    raw = dict(raw or {})
    customer_raw = raw.get("customer") or {}
    payment_raw = raw.get("payment") or {}
    products_raw = raw.get("products") or []
    if not isinstance(customer_raw, Mapping):
        customer_raw = {}
    if not isinstance(payment_raw, Mapping):
        payment_raw = {}
    if not isinstance(products_raw, list):
        products_raw = []

    products = [
        KirvanoProduct(
            id=_clip(_str(p.get("id")), MAX_ID_LEN),
            offer_id=_clip(_str(p.get("offer_id")), MAX_ID_LEN),
            name=_clip(_str(p.get("name")), MAX_NAME_LEN),
            price=_str(p.get("price")),
            is_order_bump=bool(p.get("is_order_bump", False)),
        )
        for p in products_raw
        if isinstance(p, Mapping)
    ]
    created_raw = _str(raw.get("created_at"))
    return KirvanoPayload(
        event=((_str(raw.get("event")) or "UNKNOWN").upper())[:MAX_ID_LEN],
        event_description=_str(raw.get("event_description")),
        checkout_id=_clip(_str(raw.get("checkout_id")), MAX_ID_LEN),
        sale_id=_clip(_str(raw.get("sale_id")), MAX_ID_LEN),
        status=_str(raw.get("status")),
        payment_method=_str(raw.get("payment_method")),
        type=_str(raw.get("type")),
        total_price_raw=_str(raw.get("total_price")),
        amount_cents=amount_cents_from(raw),
        created_at=parse_naive_local(created_raw, tz_name),
        created_at_raw=created_raw,
        customer=KirvanoCustomer(
            # customer["document"] (CPF) is intentionally not read: LGPD minimisation.
            name=_clip(_str(customer_raw.get("name")), MAX_NAME_LEN),
            email=_clip(_str(customer_raw.get("email")), MAX_NAME_LEN),
            phone_number=_clip(
                _str(customer_raw.get("phone_number") or customer_raw.get("phone")),
                MAX_PHONE_RAW_LEN,
            ),
        ),
        payment=KirvanoPayment(
            method=_str(payment_raw.get("method")),
            qrcode=_str(payment_raw.get("qrcode")),
            qrcode_image=_str(payment_raw.get("qrcode_image")),
            expires_at=parse_naive_local(_str(payment_raw.get("expires_at")), tz_name),
            finished_at=parse_naive_local(_str(payment_raw.get("finished_at")), tz_name),
            expires_at_raw=_str(payment_raw.get("expires_at")),
            finished_at_raw=_str(payment_raw.get("finished_at")),
        ),
        products=products,
        checkout_url=_str(raw.get("checkout_url")),
        utm=raw.get("utm") if isinstance(raw.get("utm"), Mapping) else None,
        ip=_clip(_str(raw.get("ip")), MAX_IP_LEN),
        fiscal=dict(raw["fiscal"]) if isinstance(raw.get("fiscal"), Mapping) else None,
        raw=raw,
    )


# --- token handling --------------------------------------------------------------------


def _lower_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def extract_token(
    headers: Mapping[str, str] | None, body: Mapping | None
) -> tuple[str | None, str]:
    """Return ``(token, source)`` where source names the header/field it came from."""
    h = _lower_headers(headers)
    for name in TOKEN_HEADERS:
        value = h.get(name)
        if value:
            value = value.strip()
            if name == "authorization" and value.lower().startswith("bearer "):
                value = value[7:].strip()
            if value:
                return value, f"header:{name}"
    if isinstance(body, Mapping):
        for name in TOKEN_BODY_FIELDS:
            value = body.get(name)
            if value:
                return str(value).strip(), f"body:{name}"
    return None, "none"


@dataclass(frozen=True)
class TokenCheck:
    accepted: bool
    mode: str
    source: str
    reason: str | None = None


def check_token(
    settings: Settings, headers: Mapping[str, str] | None, body: Mapping | None
) -> TokenCheck:
    """Log mode: always accept. Enforce mode: constant-time compare against the configured token."""
    token, source = extract_token(headers, body)
    if settings.kirvano_token_mode == "log":
        return TokenCheck(True, "log", source)
    expected = settings.kirvano_webhook_token
    if not expected:
        return TokenCheck(False, "enforce", source, "no_token_configured")
    if token is None:
        return TokenCheck(False, "enforce", source, "missing")
    if hmac.compare_digest(token.encode(), expected.encode()):
        return TokenCheck(True, "enforce", source)
    return TokenCheck(False, "enforce", source, "mismatch")


def auth_debug_info(headers: Mapping[str, str] | None, body: Mapping | None) -> dict:
    """What we record in log mode: header NAMES and which known fields exist — never values."""
    h = _lower_headers(headers)
    return {
        "header_names": sorted(h.keys()),
        "token_headers_present": [n for n in TOKEN_HEADERS if n in h],
        "token_body_fields_present": [
            n for n in TOKEN_BODY_FIELDS if isinstance(body, Mapping) and body.get(n)
        ],
    }


# --- event handling --------------------------------------------------------------------


@dataclass
class EventResult:
    outcome: str  # processed | duplicate | ignored | unknown | error
    event: str
    sale_id: str | None = None
    order_id: int | None = None
    job_id: int | None = None
    reason: str | None = None
    webhook_event_id: int | None = None
    cart_id: int | None = None


def new_page_token() -> str:
    """URL-safe token for the public PIX page (22 chars, 128 bits)."""
    return secrets.token_urlsafe(16)


def _lock_order(session: Session, sale_id: str) -> Order | None:
    """SELECT ... FOR UPDATE on the order row — the first half of the anti-race protocol.

    ``populate_existing`` forces the attributes to be re-read from the row we just
    locked, so a status written by another process (or an earlier request handled by
    this same session) can never be missed.
    """
    return session.execute(
        select(Order)
        .where(Order.sale_id == sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _insert_or_lock_order(session: Session, sale_id: str, now: datetime, **fields: object) -> Order:
    """Create the order for ``sale_id``, or lock the one a concurrent delivery just made.

    ``_lock_order`` cannot lock a row that does not exist yet, so two simultaneous
    first-time deliveries for the same sale both see ``None`` and both INSERT — one of
    them violating ``orders.sale_id``. A SAVEPOINT keeps that failure local: the loser
    rolls back only its own INSERT (the webhook_events row and everything else in the
    transaction survive) and then locks the winner's row instead of failing the delivery.
    """
    order = Order(
        sale_id=sale_id, page_token=new_page_token(), currency="BRL", created_at=now,
        updated_at=now, **fields,
    )  # fmt: skip
    try:
        with session.begin_nested():
            session.add(order)
            session.flush()
        return order
    except IntegrityError:
        log.info("concurrent insert for sale %s — using the existing row", sale_id)
    existing = _lock_order(session, sale_id)
    if existing is None:  # pragma: no cover - only if the row vanished again
        raise RuntimeError(f"order {sale_id} disappeared after a duplicate-key insert")
    return existing


def _apply_customer(order: Order, payload: KirvanoPayload) -> None:
    c = payload.customer
    if c.name:
        order.customer_name = c.name
    if c.email:
        order.customer_email = c.email
    # NOTE: order.customer_document (CPF) is never written — see models.Order.
    if c.phone_number:
        order.phone_raw = c.phone_number
        forms = normalize_br(c.phone_number)
        if forms:
            order.phone_e164 = forms.primary
            order.phone_alt = forms.alternate
    if payload.checkout_id:
        order.checkout_id = payload.checkout_id
    if payload.offer_id:
        order.offer_id = payload.offer_id
    if payload.product_name:
        order.product_name = payload.product_name
    if payload.amount_cents is not None:
        order.amount_cents = payload.amount_cents
    if payload.ip:
        # Opt-in evidence: where and when the customer accepted the checkout notice.
        order.consent_ip = payload.ip
        order.consent_at = payload.created_at or order.consent_at


def _fallback_expiry(payload: KirvanoPayload, settings: Settings, now: datetime) -> datetime | None:
    """When ``payment.expires_at`` is absent, derive it from the configured checkout expiry."""
    if settings.kirvano_pix_expiry_minutes:
        base = payload.created_at or now
        return base + timedelta(minutes=settings.kirvano_pix_expiry_minutes)
    return None


def _handle_pix_generated(
    session: Session,
    payload: KirvanoPayload,
    settings: Settings,
    store: SettingsStore,
    now: datetime,
    result: EventResult,
) -> None:
    assert payload.sale_id
    order = _lock_order(session, payload.sale_id)
    if order is not None and order.status in TERMINAL_PAID_STATUSES:
        result.outcome = "ignored"
        result.reason = f"order_already_{order.status}"
        result.order_id = order.id
        return
    created = order is None
    if order is None:
        order = _insert_or_lock_order(session, payload.sale_id, now)
        if order.status in TERMINAL_PAID_STATUSES:
            # The concurrent winner may already have been marked paid.
            result.outcome = "ignored"
            result.reason = f"order_already_{order.status}"
            result.order_id = order.id
            return
    _apply_customer(order, payload)
    order.status = OrderStatus.PENDING.value
    order.pix_code = payload.payment.qrcode or order.pix_code
    # Real payloads repeat the EMV string in qrcode_image; only a genuine URL is kept
    # (the PIX page renders the QR server-side from pix_code either way).
    order.pix_qr_image_url = payload.payment.qrcode_image_url or order.pix_qr_image_url
    order.pix_expires_at = payload.payment.expires_at or _fallback_expiry(payload, settings, now)
    order.updated_at = now
    session.flush()
    result.order_id = order.id

    outcome = schedule_for_order(session, order, store, now=now)
    result.outcome = "processed"
    result.reason = outcome.reason or outcome.action
    result.job_id = outcome.job.id if outcome.job else None
    log.info(
        "PIX_GENERATED sale=%s order=%s created=%s schedule=%s reason=%s",
        payload.sale_id,
        order.id,
        created,
        outcome.action,
        outcome.reason,
    )


def _handle_status_event(
    session: Session,
    payload: KirvanoPayload,
    new_status: str,
    now: datetime,
    result: EventResult,
) -> None:
    assert payload.sale_id
    order = _lock_order(session, payload.sale_id)
    if order is None:
        if not payload.is_pix and payload.event != "PIX_EXPIRED":
            # e.g. a credit-card SALE_APPROVED for a sale we never saw: nothing to recover.
            result.outcome = "ignored"
            result.reason = "unknown_order_not_pix"
            return
        order = _insert_or_lock_order(session, payload.sale_id, now, status=new_status)
        _apply_customer(order, payload)
        session.flush()

    if order.status in TERMINAL_PAID_STATUSES and new_status in (
        OrderStatus.EXPIRED.value,
        OrderStatus.REFUSED.value,
    ):
        # A late "expired" after payment must not undo the paid state.
        result.outcome = "ignored"
        result.reason = f"order_already_{order.status}"
        result.order_id = order.id
        return

    _apply_customer(order, payload)
    order.status = new_status
    if new_status == OrderStatus.PAID.value:
        order.paid_at = payload.payment.finished_at or now
    if payload.checkout_url:
        order.checkout_recovery_url = payload.checkout_url
    order.updated_at = now
    session.flush()

    job = cancel_for_order(session, order, new_status, now=now)
    result.outcome = "processed"
    result.order_id = order.id
    result.job_id = job.id if job else None
    result.reason = "job_cancelled" if job else "no_scheduled_job"
    log.info(
        "%s sale=%s order=%s cancelled_job=%s", payload.event, payload.sale_id, order.id, bool(job)
    )


def handle_event(
    session: Session,
    raw: Mapping,
    headers: Mapping[str, str] | None = None,
    *,
    settings: Settings | None = None,
    store: SettingsStore | None = None,
    now: datetime | None = None,
) -> EventResult:
    """Store the raw event (idempotently), then apply it. Commits on success.

    Duplicate deliveries (same event + sale_id + created_at) are stored once and
    return ``outcome="duplicate"`` without touching orders or jobs.
    """
    now = now or clock.utcnow()
    settings = settings or get_settings()
    store = store or SettingsStore(session, settings)
    payload = parse_payload(raw, settings.kirvano_tz)
    # What actually goes to disk: everything except the CPF and the ad cookies.
    safe_payload = redact_payload(raw)
    result = EventResult(outcome="unknown", event=payload.event, sale_id=payload.sale_id)

    auth_debug = None
    if settings.kirvano_token_mode == "log":
        n = session.execute(
            select(func.count())
            .select_from(WebhookEvent)
            .where(WebhookEvent.source == "kirvano", WebhookEvent.auth_debug.is_not(None))
        ).scalar_one()
        if n < settings.auth_debug_max_rows:
            auth_debug = auth_debug_info(headers, raw)

    evt = WebhookEvent(
        source="kirvano",
        external_key=payload.idempotency_key()[:255],
        event=payload.event,
        sale_id=payload.sale_id,
        payload=safe_payload,
        headers_meta={"header_names": sorted(_lower_headers(headers).keys())},
        auth_debug=auth_debug,
        received_at=now,
    )
    session.add(evt)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        log.info("duplicate Kirvano event %s sale=%s", payload.event, payload.sale_id)
        result.outcome = "duplicate"
        return result
    except DatabaseError as exc:
        # Anything the database refuses that is NOT a duplicate (a DataError from an
        # over-long value, a dropped connection...). The endpoint is public and the
        # spec says "always answer 200 quickly", so this must not escape as a 500.
        session.rollback()
        log.exception("could not store Kirvano event %s", payload.event)
        result.outcome, result.reason = "error", type(exc).__name__
        return result
    result.webhook_event_id = evt.id

    try:
        if payload.event == EVENT_PIX_GENERATED:
            if not payload.sale_id:
                result.outcome, result.reason = "ignored", "no_sale_id"
            else:
                _handle_pix_generated(session, payload, settings, store, now, result)
        elif payload.event == EVENT_ABANDONED_CART:
            handle_abandoned_cart(session, payload, store, now, result)
        elif payload.event in STATUS_EVENTS:
            if not payload.sale_id:
                result.outcome, result.reason = "ignored", "no_sale_id"
            else:
                _handle_status_event(session, payload, STATUS_EVENTS[payload.event], now, result)
        elif payload.event.startswith(IGNORED_EVENT_PREFIXES):
            result.outcome, result.reason = "ignored", "event_not_relevant"
        else:
            result.outcome, result.reason = "unknown", "unknown_event"
            log.warning("unknown Kirvano event %r stored (id=%s)", payload.event, evt.id)
        if payload.event in CONVERSION_EVENTS:
            # Runs even when the order path ignored the event (a credit-card sale we
            # never saw as a PIX): that sale still ends — and may recover — a cart.
            # The order lock (if any) is already held, so cart locks come second.
            on_sale_event(session, payload, now)
        evt.processed_at = now
        evt.outcome = result.outcome
        session.commit()
    except Exception as exc:  # noqa: BLE001 - we must still answer 200 and keep the raw event
        log.exception("error processing Kirvano event %s", payload.event)
        session.rollback()
        # The event row was rolled back with everything else: store it again with the error.
        session.add(
            WebhookEvent(
                source="kirvano",
                external_key=evt.external_key,
                event=payload.event,
                sale_id=payload.sale_id,
                payload=safe_payload,
                headers_meta=evt.headers_meta,
                auth_debug=auth_debug,
                received_at=now,
                processed_at=now,
                outcome="error",
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
        )
        try:
            session.commit()
        except DatabaseError:
            # A concurrent delivery of the same key may have committed it while this
            # transaction was rolling back. Losing the error row is acceptable (the
            # exception is already in the log); breaking the always-200 contract in
            # the very path that exists to preserve it is not.
            session.rollback()
            log.info("error event not stored (already present): %s", evt.external_key)
        result.outcome = "error"
        result.reason = type(exc).__name__
    return result
