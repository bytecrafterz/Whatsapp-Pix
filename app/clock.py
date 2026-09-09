"""Single source of truth for "now".

Every module calls ``clock.utcnow()`` (module attribute access, not a bare import)
so tests can freeze time by monkeypatching ``app.clock.utcnow``. All datetimes in
the application are timezone-aware UTC; naive datetimes are a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_aware(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime (values coming back from SQLite) or normalise to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
