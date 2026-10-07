"""Opt-out helpers shared by the Meta webhook handler, the worker and the panel.

An opt-out is stored once per phone *variant* (13- and 12-digit BR forms) and
carries the ``wa_id`` when known, so ``is_opted_out`` hits regardless of which
form a future order or inbound message uses.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.models import CartJob, JobState, OptOut, Order, PostSaleJob, RecoveryJob
from app.phone import normalize_br

log = logging.getLogger(__name__)


def _all_forms(phones: Iterable[str | None], wa_id: str | None) -> set[str]:
    """Expand every given phone/wa_id into all of its dialable variants."""
    forms: set[str] = set()
    for p in list(phones) + [wa_id]:
        if not p:
            continue
        f = normalize_br(p)
        if f:
            forms.update(f.variants)
        else:
            forms.add(p)
    return forms


def is_opted_out(session: Session, phones: Iterable[str | None], wa_id: str | None = None) -> bool:
    """True when any phone variant or the wa_id is in ``opt_outs``."""
    forms = _all_forms(phones, wa_id)
    conds = []
    if forms:
        conds.append(OptOut.phone.in_(sorted(forms)))
    if wa_id:
        conds.append(OptOut.wa_id == wa_id)
    if not conds:
        return False
    return bool(session.execute(select(exists().where(or_(*conds)))).scalar())


def add_opt_out(
    session: Session,
    *,
    phone: str | None,
    wa_id: str | None,
    source: str,
    note: str | None = None,
    now: datetime | None = None,
) -> list[OptOut]:
    """Insert opt-out rows for every variant of ``phone``/``wa_id``.

    Returns the rows that were newly created (empty when already opted out),
    which lets callers "reply once" instead of on every repeated SAIR.
    """
    now = now or clock.utcnow()
    forms = _all_forms([phone], wa_id)
    if not forms:
        return []
    existing = set(
        session.execute(select(OptOut.phone).where(OptOut.phone.in_(sorted(forms)))).scalars()
    )
    created: list[OptOut] = []
    for form in sorted(forms):
        if form in existing:
            continue
        row = OptOut(wa_id=wa_id, phone=form, source=source, note=note, created_at=now)
        session.add(row)
        created.append(row)
    session.flush()
    if created:
        log.info("opt-out recorded source=%s forms=%s", source, sorted(forms))
    return created


def remove_opt_out(session: Session, phone_or_wa_id: str) -> int:
    """Delete opt-out rows for all variants of the number; returns rows removed."""
    forms = _all_forms([phone_or_wa_id], None)
    if not forms:
        return 0
    result = session.execute(
        delete(OptOut).where(or_(OptOut.phone.in_(sorted(forms)), OptOut.wa_id == phone_or_wa_id))
    )
    session.flush()
    return int(result.rowcount or 0)


def list_opt_outs(session: Session, limit: int = 500) -> list[OptOut]:
    return list(
        session.execute(select(OptOut).order_by(OptOut.created_at.desc()).limit(limit)).scalars()
    )


def cancel_jobs_for_phones(
    session: Session,
    phones: Iterable[str | None],
    wa_id: str | None,
    *,
    reason: str = "opted_out",
    now: datetime | None = None,
) -> list[RecoveryJob | CartJob | PostSaleJob]:
    """Cancel every *scheduled* job for this phone/wa_id: PIX reminders, cart, post-sale."""
    now = now or clock.utcnow()
    phones = list(phones)  # read once per flow below
    forms = _all_forms(phones, wa_id)
    conds = []
    if forms:
        conds.append(Order.phone_e164.in_(sorted(forms)))
        conds.append(Order.phone_alt.in_(sorted(forms)))
    if wa_id:
        conds.append(Order.wa_id == wa_id)
    if not conds:
        return []
    # Step 1: find the candidates with NO lock. A bare `FOR UPDATE` on the join below
    # would lock rows in BOTH tables, and because the join drives from recovery_jobs it
    # would take the JOB lock before the ORDER lock — the reverse of what every other
    # path does (scheduling.claim_due_jobs, scheduling.cancel_for_order and
    # kirvano._lock_order all lock the order row first). That inversion is a textbook
    # ABBA deadlock between this handler and a concurrent worker tick on Postgres.
    candidates = session.execute(
        select(RecoveryJob.id, RecoveryJob.order_id)
        .join(Order, Order.id == RecoveryJob.order_id)
        .where(RecoveryJob.state == JobState.SCHEDULED.value, or_(*conds))
        # Total order so two concurrent opt-outs walk the same rows in the same
        # sequence and cannot deadlock against each other either.
        .order_by(RecoveryJob.order_id, RecoveryJob.id)
    ).all()

    jobs: list[RecoveryJob] = []
    for job_id, order_id in candidates:
        # Step 2: ORDER first, then the job — the shared lock protocol.
        session.execute(select(Order.id).where(Order.id == order_id).with_for_update()).first()
        job = session.execute(
            select(RecoveryJob)
            .where(RecoveryJob.id == job_id)
            .with_for_update()
            # The state must be re-read under the lock: the worker may have flipped it
            # to `sending` between the unlocked scan and this lock, and a message
            # already in flight cannot be recalled.
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if job is None or job.state != JobState.SCHEDULED.value:
            continue
        job.state = JobState.CANCELLED.value
        job.reason = reason
        job.updated_at = now
        jobs.append(job)
    session.flush()
    # An opt-out stops the abandoned-cart and post-sale sequences too. Local imports:
    # both modules import this one (is_opted_out). Appended after the PIX jobs.
    from app.cart import cancel_cart_jobs_for_phones
    from app.postsale import cancel_post_sale_jobs_for_phones

    cart_jobs = cancel_cart_jobs_for_phones(session, phones, wa_id, reason=reason, now=now)
    post_jobs = cancel_post_sale_jobs_for_phones(session, phones, wa_id, reason=reason, now=now)
    return [*jobs, *cart_jobs, *post_jobs]
