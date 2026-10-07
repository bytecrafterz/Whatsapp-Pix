"""Abandoned-cart recovery: Kirvano ``ABANDONED_CART`` → a short Marketing sequence.

Flow
----
1. :func:`handle_abandoned_cart` (called by ``kirvano.handle_event`` inside its
   transaction) upserts the cart — by ``checkout_id``, else the same phone's open cart
   from the last 24 h — and, unless something rules it out, creates one ``cart_jobs``
   row per enabled step. Kirvano repeating the event never starts a second sequence.
2. :func:`on_sale_event` (``PIX_GENERATED`` / ``SALE_APPROVED``, same transaction): the
   customer came back. Every matching open cart stops its sequence at once; a
   ``SALE_APPROVED`` also records the conversion, and whether it counts as recovered
   (bought after at least one cart message).
3. :func:`claim_due_cart_jobs` (worker) is the PIX claim protocol of
   ``app.scheduling`` with the CART row in the role of the order row: lock the cart,
   lock the job ``SKIP LOCKED``, re-check everything, flip to ``sending``, commit, and
   only then call Meta.

Lock order: the Kirvano webhook holds an ORDER lock and then takes CART locks; the
worker and the opt-out path take a CART lock and then that cart's JOB locks. Nothing
takes an order lock while holding a cart lock, so the graph has no cycle.

The two flows never stack on one customer: no cart message to someone with a PIX in
progress, a PIX reminder in the last 24 h, or the same product already paid
(:func:`phone_conflict`), and a PIX generated after the abandonment ends the cart.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.format import SP_TZ
from app.models import (
    Cart,
    CartJob,
    CartStatus,
    JobState,
    Order,
    OrderStatus,
    RecoveryJob,
    TemplateStatus,
)
from app.optout import is_opted_out
from app.phone import normalize_br
from app.scheduling import (
    DISABLED_RECHECK,
    STALE_SENDING_AFTER,
    TEMPLATE_BLOCKING_STATUSES,
    _recipient_key,
    backoff_delay,
    in_quiet_hours,
    mark_skipped,
    postpone,
    quiet_hours_end,
    recipients_last_24h,
)
from app.settings_store import CART_MAX_DELAY_MINUTES, CartStepConfig, SettingsStore

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime (kirvano imports us)
    from app.kirvano import EventResult, KirvanoPayload

log = logging.getLogger(__name__)

EVENT_ABANDONED_CART = "ABANDONED_CART"
CONVERSION_EVENTS = frozenset({"PIX_GENERATED", "SALE_APPROVED"})

# The same phone abandoning again inside this window continues the SAME sequence.
DEDUPE_WINDOW = timedelta(hours=24)
# A purchase this long after the abandonment is still credited to the cart.
ATTRIBUTION_WINDOW = timedelta(days=7)
# Never send a step later than this after the abandonment (the longest configurable
# delay plus a day of slack for quiet hours and retries).
MAX_CART_AGE = timedelta(minutes=CART_MAX_DELAY_MINUTES) + timedelta(days=1)
# Two messages of one sequence are never closer than this, whatever the timing.
MIN_STEP_GAP = timedelta(minutes=60)
# A step waiting for the previous one re-checks this often.
PREVIOUS_STEP_RECHECK = timedelta(minutes=5)
# A PIX order this recent means the PIX reminder owns the customer.
PIX_FLOW_WINDOW = timedelta(hours=24)
# A paid order for the same product in this window means they already bought.
PURCHASE_LOOKBACK = timedelta(days=30)

# Where Kirvano might put the link back to the checkout. The real ABANDONED_CART body
# has not been captured yet, so every plausible key is tried; only http(s) is kept.
CHECKOUT_URL_KEYS = (
    "checkout_url",
    "abandoned_checkout_url",
    "recovery_url",
    "checkout_link",
    "url",
    "link",
)
MAX_URL_LEN = 2000


# --- payload helpers ----------------------------------------------------------------


def _http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.lower().startswith(("http://", "https://")):
        return candidate[:MAX_URL_LEN]
    return None


def checkout_link_from(raw: Mapping) -> str | None:
    """The cart's own checkout link from an ``ABANDONED_CART`` body, if it has one."""
    for key in CHECKOUT_URL_KEYS:
        url = _http_url(raw.get(key))
        if url:
            return url
    nested = raw.get("checkout")
    if isinstance(nested, Mapping):
        for key in ("url", "link", "checkout_url"):
            url = _http_url(nested.get(key))
            if url:
                return url
    return None


def new_link_token() -> str:
    """URL-safe token for the public /c/ redirect (22 chars, 128 bits)."""
    return secrets.token_urlsafe(16)


def phone_forms(phones: Iterable[str | None]) -> list[str]:
    """Every dialable variant (13- and 12-digit) of the given numbers, sorted.

    Only NORMALISED forms: carts store nothing else, and a raw webhook value (up to 32
    characters) compared with the 20-character phone columns makes PostgreSQL raise
    ``value too long for type character varying(20)`` — which would fail the whole
    PIX_GENERATED event that triggered the comparison.
    """
    forms: set[str] = set()
    for p in phones:
        f = normalize_br(p) if p else None
        if f:
            forms.update(f.variants)
    return sorted(forms)


# --- locking ------------------------------------------------------------------------


def _lock_cart(session: Session, cart_id: int) -> Cart | None:
    """SELECT ... FOR UPDATE on a cart, re-read from the row (see kirvano._lock_order)."""
    return session.execute(
        select(Cart)
        .where(Cart.id == cart_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _locate_cart(
    session: Session, *, checkout_id: str | None, forms: list[str], now: datetime
) -> Cart | None:
    """The cart a new ABANDONED_CART belongs to: same checkout, else same phone recently."""
    cart_id: int | None = None
    if checkout_id:
        # Bounded in time: a checkout code seen again weeks later is a new abandonment,
        # not a repeat of the old one, and deserves its own sequence.
        cart_id = session.execute(
            select(Cart.id)
            .where(Cart.checkout_id == checkout_id, Cart.abandoned_at >= now - ATTRIBUTION_WINDOW)
            .order_by(Cart.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    if cart_id is None and forms:
        cart_id = session.execute(
            select(Cart.id)
            .where(
                Cart.status == CartStatus.OPEN.value,
                Cart.abandoned_at >= now - DEDUPE_WINDOW,
                or_(Cart.phone_e164.in_(forms), Cart.phone_alt.in_(forms)),
            )
            .order_by(Cart.abandoned_at.desc(), Cart.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    return _lock_cart(session, cart_id) if cart_id is not None else None


# --- who must NOT get a cart message ------------------------------------------------------


def phone_conflict(session: Session, cart: Cart, now: datetime) -> str | None:
    """Reason this customer must not get a cart message right now, or ``None``.

    Checked when the sequence starts AND before every send, because a PIX or a sale
    under another checkout (or another e-mail) is not always matched to the cart.
    """
    forms = phone_forms(cart.phone_variants)
    if not forms:
        return None
    rows = session.execute(
        select(Order.status, Order.created_at, Order.product_name, Order.offer_id).where(
            or_(Order.phone_e164.in_(forms), Order.phone_alt.in_(forms)),
            Order.created_at >= cart.abandoned_at - PURCHASE_LOOKBACK,
        )
    ).all()
    pix_flow_since = cart.abandoned_at - PIX_FLOW_WINDOW
    for status, created_at, product_name, offer_id in rows:
        if created_at >= cart.abandoned_at:
            # Came back after abandoning: the PIX flow (or the sale) owns them now.
            return "purchased_after" if status == OrderStatus.PAID.value else "pix_generated"
        if status == OrderStatus.PENDING.value and created_at >= pix_flow_since:
            return "pix_flow_active"
        same_product = (offer_id and offer_id == cart.offer_id) or (
            product_name and product_name == cart.product_name
        )
        if status == OrderStatus.PAID.value and same_product:
            return "already_purchased"
    reminded = session.execute(
        select(
            exists().where(
                RecoveryJob.order_id == Order.id,
                RecoveryJob.state == JobState.SENT.value,
                RecoveryJob.sent_at >= now - PIX_FLOW_WINDOW,
                or_(Order.phone_e164.in_(forms), Order.phone_alt.in_(forms)),
            )
        )
    ).scalar()
    return "pix_reminder_sent" if reminded else None


def start_blocker(session: Session, cart: Cart, store: SettingsStore, now: datetime) -> str | None:
    """Why no sequence may start for ``cart`` (stored on ``cart.reason``), or ``None``."""
    if not store.cart_enabled:
        return "disabled"
    if not cart.phone_variants:
        return "no_phone"
    if now - cart.abandoned_at > MAX_CART_AGE:
        return "cart_too_old"
    if is_opted_out(session, cart.phone_variants, cart.wa_id):
        return "opted_out"
    return phone_conflict(session, cart, now)


# --- scheduling ---------------------------------------------------------------------------


def compute_cart_run_at(
    abandoned_at: datetime,
    delay_minutes: int,
    now: datetime,
    store: SettingsStore,
    tz: ZoneInfo = SP_TZ,
) -> tuple[datetime, bool]:
    """``(run_at, postponed_for_quiet)``: abandonment + delay, never in the past or at night."""
    run_at = max(abandoned_at + timedelta(minutes=delay_minutes), now)
    if in_quiet_hours(run_at, store.quiet_start, store.quiet_end, tz):
        return quiet_hours_end(run_at, store.quiet_start, store.quiet_end, tz), True
    return run_at, False


def schedule_cart_jobs(
    session: Session, cart: Cart, store: SettingsStore, now: datetime, tz: ZoneInfo = SP_TZ
) -> list[CartJob]:
    """One scheduled job per enabled step (caller holds the cart lock). Does not commit."""
    jobs: list[CartJob] = []
    for cfg in store.cart_step_configs():
        run_at, quiet = compute_cart_run_at(cart.abandoned_at, cfg.delay_minutes, now, store, tz)
        job = CartJob(
            cart_id=cart.id,
            step=cfg.step,
            run_at=run_at,
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


def _apply_payload(cart: Cart, payload: KirvanoPayload) -> None:
    """Copy what the event carries onto the cart; never blank a field already known."""
    c = payload.customer
    if c.name:
        cart.customer_name = c.name
    if c.email:
        cart.customer_email = c.email
    if c.phone_number:
        cart.phone_raw = c.phone_number
        forms = normalize_br(c.phone_number)
        if forms:
            cart.phone_e164 = forms.primary
            cart.phone_alt = forms.alternate
    if payload.checkout_id:
        cart.checkout_id = payload.checkout_id
    if payload.offer_id:
        cart.offer_id = payload.offer_id
    if payload.product_name:
        cart.product_name = payload.product_name
    if payload.amount_cents is not None:
        cart.amount_cents = payload.amount_cents
    link = checkout_link_from(payload.raw)
    if link:
        cart.checkout_url = link
    if payload.ip:
        cart.consent_ip = payload.ip
        cart.consent_at = payload.created_at or cart.consent_at


def handle_abandoned_cart(
    session: Session,
    payload: KirvanoPayload,
    store: SettingsStore,
    now: datetime,
    result: EventResult,
) -> Cart:
    """Store the abandoned cart and start its sequence when allowed. Does not commit."""
    c = payload.customer
    normalised = normalize_br(c.phone_number) if c.phone_number else None
    forms = list(normalised.variants) if normalised else []
    abandoned_at = min(payload.created_at or now, now)

    cart = _locate_cart(session, checkout_id=payload.checkout_id, forms=forms, now=now)
    if cart is not None and cart.status != CartStatus.OPEN.value:
        # A repeat for a checkout that already converted: nothing to recover.
        result.outcome, result.reason, result.cart_id = "ignored", f"cart_{cart.status}", cart.id
        return cart
    if cart is None:
        cart = Cart(
            link_token=new_link_token(),
            status=CartStatus.OPEN.value,
            abandoned_at=abandoned_at,
            created_at=now,
            updated_at=now,
        )
        session.add(cart)
    _apply_payload(cart, payload)
    cart.updated_at = now
    session.flush()
    result.cart_id = cart.id
    result.outcome = "processed"

    has_jobs = session.execute(select(exists().where(CartJob.cart_id == cart.id))).scalar()
    if has_jobs:
        result.reason = "sequence_exists"
        return cart
    blocker = start_blocker(session, cart, store, now)
    if blocker:
        cart.reason = blocker
        result.reason = blocker
        log.info("cart %s stored without sequence: %s", cart.id, blocker)
        return cart
    jobs = schedule_cart_jobs(session, cart, store, now)
    cart.reason = None
    result.reason = f"scheduled_{len(jobs)}"
    log.info(
        "cart %s checkout=%s sequence scheduled steps=%s first=%s",
        cart.id,
        cart.checkout_id,
        len(jobs),
        jobs[0].run_at if jobs else None,
    )
    return cart


# --- cancellation -------------------------------------------------------------------------


def cancel_cart_jobs(
    session: Session, cart: Cart, reason: str, *, now: datetime | None = None
) -> list[CartJob]:
    """Cancel the cart's still-scheduled steps (caller holds the cart lock)."""
    now = now or clock.utcnow()
    jobs = list(
        session.execute(
            select(CartJob)
            .where(CartJob.cart_id == cart.id, CartJob.state == JobState.SCHEDULED.value)
            .order_by(CartJob.step)
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


def cancel_cart_jobs_for_phones(
    session: Session,
    phones: Iterable[str | None],
    wa_id: str | None,
    *,
    reason: str = "opted_out",
    now: datetime | None = None,
) -> list[CartJob]:
    """Cancel every scheduled cart step for this phone/wa_id (the opt-out path)."""
    now = now or clock.utcnow()
    forms = phone_forms([*phones, wa_id])
    conds = []
    if forms:
        conds += [Cart.phone_e164.in_(forms), Cart.phone_alt.in_(forms)]
    if wa_id:
        conds.append(Cart.wa_id == wa_id)
    if not conds:
        return []
    # Unlocked scan first, then CART lock before JOB lock — same reasoning as
    # optout.cancel_jobs_for_phones (a locking join would take the job lock first).
    candidates = session.execute(
        select(CartJob.id, CartJob.cart_id)
        .join(Cart, Cart.id == CartJob.cart_id)
        .where(CartJob.state == JobState.SCHEDULED.value, or_(*conds))
        .order_by(CartJob.cart_id, CartJob.id)
    ).all()
    cancelled: list[CartJob] = []
    for job_id, cart_id in candidates:
        session.execute(select(Cart.id).where(Cart.id == cart_id).with_for_update()).first()
        job = session.execute(
            select(CartJob)
            .where(CartJob.id == job_id)
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


# --- conversions --------------------------------------------------------------------------


def _matching_carts(session: Session, payload: KirvanoPayload, now: datetime) -> list[Cart]:
    """Locked carts (by id) this sale/PIX belongs to: same sale, checkout, phone or e-mail."""
    conds = []
    if payload.sale_id:
        conds.append(Cart.converted_sale_id == payload.sale_id)
    if payload.checkout_id:
        conds.append(Cart.checkout_id == payload.checkout_id)
    forms = phone_forms([payload.customer.phone_number])
    if forms:
        conds += [Cart.phone_e164.in_(forms), Cart.phone_alt.in_(forms)]
    email = (payload.customer.email or "").strip().lower()
    if email:
        conds.append(func.lower(Cart.customer_email) == email)
    if not conds:
        return []
    ids = session.execute(
        select(Cart.id)
        .where(
            or_(*conds),
            Cart.status.in_([CartStatus.OPEN.value, CartStatus.PIX_GENERATED.value]),
            Cart.abandoned_at >= now - ATTRIBUTION_WINDOW,
        )
        .order_by(Cart.id)
    ).scalars()
    return [cart for cid in list(ids) if (cart := _lock_cart(session, cid)) is not None]


def _step_sent_before(session: Session, cart: Cart, moment: datetime) -> bool:
    return bool(
        session.execute(
            select(
                exists().where(
                    CartJob.cart_id == cart.id,
                    CartJob.state == JobState.SENT.value,
                    CartJob.sent_at <= moment,
                )
            )
        ).scalar()
    )


def on_sale_event(session: Session, payload: KirvanoPayload, now: datetime) -> list[Cart]:
    """A PIX was generated or a sale approved: stop and/or close the matching carts."""
    if payload.event not in CONVERSION_EVENTS:
        return []
    carts = _matching_carts(session, payload, now)
    for cart in carts:
        if payload.event == "PIX_GENERATED":
            if cart.status != CartStatus.OPEN.value:
                continue
            cart.status = CartStatus.PIX_GENERATED.value
            cart.converted_sale_id = cart.converted_sale_id or payload.sale_id
            cancel_cart_jobs(session, cart, "pix_generated", now=now)
        else:  # SALE_APPROVED
            paid_at = payload.payment.finished_at or now
            cart.status = CartStatus.PURCHASED.value
            cart.converted_at = paid_at
            cart.converted_sale_id = payload.sale_id or cart.converted_sale_id
            cart.converted_amount_cents = payload.amount_cents
            cart.recovered = _step_sent_before(session, cart, paid_at)
            cancel_cart_jobs(session, cart, "purchased", now=now)
        cart.updated_at = now
        log.info(
            "cart %s %s by %s sale=%s recovered=%s",
            cart.id,
            cart.status,
            payload.event,
            payload.sale_id,
            cart.recovered,
        )
    session.flush()
    return carts


# --- worker side --------------------------------------------------------------------------


@dataclass
class ClaimedCartJob:
    job: CartJob
    cart: Cart
    step: CartStepConfig


def cart_template_ok(session: Session, step: CartStepConfig) -> tuple[bool, str | None]:
    """(ok, reason): a template name is set and Meta has not paused/rejected it."""
    if not step.template_name:
        return False, "template_not_configured"
    row = session.get(TemplateStatus, (step.template_name, step.language))
    if row is not None and row.status and row.status.upper() in TEMPLATE_BLOCKING_STATUSES:
        return False, "template_unavailable"
    return True, None


def _previous_step_gate(
    session: Session, job: CartJob, store: SettingsStore, now: datetime
) -> tuple[str, datetime | None] | None:
    """``None`` when step ``job.step`` may go now; else ``("skip"|"wait", until)``."""
    if job.step <= 1:
        return None
    prev = session.execute(
        select(CartJob).where(CartJob.cart_id == job.cart_id, CartJob.step == job.step - 1)
    ).scalar_one_or_none()
    if prev is None or prev.state in (
        JobState.CANCELLED.value,
        JobState.SKIPPED.value,
        JobState.FAILED.value,
    ):
        return "skip", None
    if prev.state != JobState.SENT.value or prev.sent_at is None:
        return "wait", max(prev.run_at, now) + PREVIOUS_STEP_RECHECK
    configured = timedelta(
        minutes=store.cart_step(job.step).delay_minutes
        - store.cart_step(job.step - 1).delay_minutes
    )
    earliest = prev.sent_at + max(configured, MIN_STEP_GAP)
    return ("wait", earliest) if now < earliest else None


def claim_due_cart_jobs(
    session: Session,
    store: SettingsStore,
    *,
    now: datetime | None = None,
    limit: int = 20,
    tz: ZoneInfo = SP_TZ,
) -> list[ClaimedCartJob]:
    """Claim due cart steps under lock (see module docstring). Commits the claim."""
    now = now or clock.utcnow()
    candidates = session.execute(
        select(CartJob.id, CartJob.cart_id)
        .where(CartJob.state == JobState.SCHEDULED.value, CartJob.run_at <= now)
        .order_by(CartJob.run_at, CartJob.id)  # total order: no cross-worker deadlock
        .limit(limit)
    ).all()
    if not candidates:
        session.commit()
        return []

    recipients = recipients_last_24h(session, now)
    daily_limit = store.daily_recipient_limit
    claimed: list[ClaimedCartJob] = []

    for job_id, cart_id in candidates:
        cart = _lock_cart(session, cart_id)
        job = session.execute(
            select(CartJob)
            .where(CartJob.id == job_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if job is None or cart is None:
            continue
        if job.state != JobState.SCHEDULED.value or job.run_at > now:
            continue

        too_old = now - cart.abandoned_at > MAX_CART_AGE
        if not store.cart_enabled:
            if too_old:
                mark_skipped(session, job, "cart_too_old", now=now)
            else:
                postpone(session, job, now + DISABLED_RECHECK, reason="disabled", now=now)
            continue
        if cart.status != CartStatus.OPEN.value:
            mark_skipped(session, job, f"cart_{cart.status}", now=now)
            continue
        if too_old:
            mark_skipped(session, job, "cart_too_old", now=now)
            continue
        if job.step > store.cart_steps:
            mark_skipped(session, job, "step_disabled", now=now)
            continue
        if is_opted_out(session, cart.phone_variants, cart.wa_id):
            mark_skipped(session, job, "opted_out", now=now)
            cancel_cart_jobs(session, cart, "opted_out", now=now)
            continue
        conflict = phone_conflict(session, cart, now)
        if conflict:
            mark_skipped(session, job, conflict, now=now)
            cancel_cart_jobs(session, cart, conflict, now=now)
            continue
        if in_quiet_hours(now, store.quiet_start, store.quiet_end, tz):
            until = quiet_hours_end(now, store.quiet_start, store.quiet_end, tz)
            postpone(session, job, until, reason="quiet_hours", now=now)
            continue
        step = store.cart_step(job.step)
        ok, why = cart_template_ok(session, step)
        if not ok:
            mark_skipped(session, job, why or "template_unavailable", now=now)
            continue
        gate = _previous_step_gate(session, job, store, now)
        if gate is not None:
            action, until = gate
            if action == "skip":
                mark_skipped(session, job, "previous_not_sent", now=now)
            else:
                postpone(session, job, until or now, reason="waiting_previous", now=now)
            continue
        to = cart.phone_e164 or cart.phone_alt
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
        claimed.append(ClaimedCartJob(job=job, cart=cart, step=step))

    session.commit()
    return claimed


def requeue_cart_job(
    session: Session,
    job: CartJob,
    cart: Cart,
    *,
    error_code: str | None,
    error_text: str | None,
    now: datetime,
    max_attempts: int,
) -> CartJob:
    """Retryable error: count the attempt and reschedule, unless the cart got too old."""
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
        if run_at - cart.abandoned_at > MAX_CART_AGE:
            job.state = JobState.SKIPPED.value
            job.reason = "cart_too_old"
        else:
            job.state = JobState.SCHEDULED.value
            job.run_at = run_at
            job.reason = f"retry_{error_code}" if error_code else "retry"
    session.flush()
    return job


def reap_stale_cart_sending(session: Session, *, now: datetime | None = None) -> int:
    """Fail cart steps stuck in ``sending`` (worker died mid-request). Commits.

    Same rule as ``scheduling.reap_stale_sending``: Meta may already have delivered
    it, so a stuck step is failed, never resent.
    """
    now = now or clock.utcnow()
    stale = (
        session.execute(
            select(CartJob)
            .where(
                CartJob.state == JobState.SENDING.value,
                CartJob.claimed_at < now - STALE_SENDING_AFTER,
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


def register_click(session: Session, cart: Cart, now: datetime) -> None:
    """Count a tap on the message button (the /c/ redirect). Does not commit."""
    cart.clicks = (cart.clicks or 0) + 1
    if cart.first_click_at is None:
        cart.first_click_at = now
    session.flush()
