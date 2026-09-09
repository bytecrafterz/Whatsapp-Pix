"""Engine and session management.

* SQLite (tests / dev): ``check_same_thread=False`` because FastAPI's threadpool
  hands requests to arbitrary threads; WAL journal so the worker and the API can
  share a file DB; in-memory URLs use a ``StaticPool`` so every session sees the
  same connection (otherwise each connection would be a fresh empty database).
* PostgreSQL (server): plain ``QueuePool`` with ``pool_pre_ping`` so a restarted
  Postgres does not leave stale sockets in the pool.

``expire_on_commit=False`` everywhere: handlers return ORM objects after commit
and the worker keeps using claimed rows after the claim transaction commits.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

log = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def make_engine(url: str, *, echo: bool = False) -> Engine:
    """Build an engine with the dialect-specific tweaks described in the module docstring."""
    if url.startswith("sqlite"):
        kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, echo=echo, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")  # no-op ("memory") for :memory:
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=5000")
            finally:
                cur.close()

        return engine
    return create_engine(url, echo=echo, pool_pre_ping=True)


def configure(engine: Engine) -> None:
    """Install ``engine`` as the process-wide engine (tests call this with in-memory SQLite)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


def get_engine() -> Engine:
    if _engine is None:
        configure(make_engine(get_settings().database_url))
    assert _engine is not None
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, closed afterwards.

    Handlers commit explicitly; anything left open is rolled back on close.
    """
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager used by the worker and scripts: commit on success, rollback on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create every table (idempotent). Called at API startup and by the worker."""
    from app.models import Base  # local import: models import db types

    Base.metadata.create_all(engine or get_engine())
