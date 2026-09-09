"""Read-only queries used exclusively by the client panel.

``app/queries.py`` is the architect's shared read-helper module; panel-only
reads live here so that the panel, the public pages and the deploy scripts can
be built in parallel without three agents editing the same file. Everything in
here is side-effect free — no writes, no HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.inbound import window_closes_at, window_open
from app.models import Contact, Message, TemplateStatus
from app.queries import conversations, last_message_for


@dataclass(frozen=True)
class ConversationRow:
    """One line of the Conversas list: contact + its last message + window state."""

    contact: Contact
    last_message: Message | None
    window_open: bool
    window_closes_at: datetime | None


def conversation_rows(
    session: Session, *, limit: int = 100, now: datetime | None = None
) -> list[ConversationRow]:
    """Contacts (most recent first) with the last message and the 24 h window state.

    The per-contact "last message" is a separate small query: the panel shows at
    most ``limit`` (default 100) contacts, so N+1 here costs less than the window
    function needed to do it in one statement — and it keeps the SQL portable
    between SQLite (tests) and Postgres (server).
    """
    now = now or clock.utcnow()
    rows: list[ConversationRow] = []
    for contact in conversations(session, limit=limit):
        rows.append(
            ConversationRow(
                contact=contact,
                last_message=last_message_for(session, contact.wa_id),
                window_open=window_open(contact, now),
                window_closes_at=window_closes_at(contact),
            )
        )
    return rows


def all_template_status(session: Session) -> list[TemplateStatus]:
    """Every template we have ever seen a status for, most recently updated first."""
    return list(
        session.execute(
            select(TemplateStatus).order_by(TemplateStatus.updated_at.desc(), TemplateStatus.name)
        ).scalars()
    )
