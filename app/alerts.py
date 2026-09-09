"""Operator alerts: persisted in the ``alerts`` table and logged.

The spec asks to "alert" on template/token/code-bug failures. There is no
external pager; the panel lists open alerts and the log carries them too.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Alert

log = logging.getLogger("app.alerts")


def record_alert(
    session: Session,
    code: str,
    message: str,
    *,
    level: str = "error",
    context: dict | None = None,
    now: datetime | None = None,
    dedupe: bool = False,
) -> Alert:
    """Persist and log an alert. Never commits — caller owns the transaction.

    ``dedupe=True`` refreshes the existing OPEN alert with the same code instead of
    inserting a second one. Use it for conditions that describe a single broken
    thing rather than a single failed job (an expired token, a paused template):
    without it, a dead token wrote one row per claimed job and the panel's "alertas
    abertos" list filled up with ``worker_batch_size`` copies of the same sentence.
    """
    now = now or clock.utcnow()
    if dedupe:
        existing = session.execute(
            select(Alert)
            .where(Alert.code == code, Alert.resolved_at.is_(None))
            .order_by(Alert.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            existing.level = level
            existing.message = message
            existing.context = context
            existing.created_at = now  # "last seen": the panel sorts by this
            session.flush()
            log.log(
                logging.ERROR if level == "error" else logging.WARNING,
                "ALERT %s (repetido): %s",
                code,
                message,
            )
            return existing
    row = Alert(level=level, code=code, message=message, context=context, created_at=now)
    session.add(row)
    session.flush()
    log.log(logging.ERROR if level == "error" else logging.WARNING, "ALERT %s: %s", code, message)
    return row


def open_alerts(session: Session, limit: int = 50) -> list[Alert]:
    return list(
        session.execute(
            select(Alert)
            .where(Alert.resolved_at.is_(None))
            .order_by(Alert.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def resolve_alert(session: Session, alert_id: int, *, now: datetime | None = None) -> bool:
    row = session.get(Alert, alert_id)
    if row is None:
        return False
    row.resolved_at = now or clock.utcnow()
    session.flush()
    return True
