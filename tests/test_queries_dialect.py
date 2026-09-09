"""Every statement the panel and the worker run must compile for BOTH dialects.

The whole suite runs on SQLite, which is far more permissive than PostgreSQL: it
accepts a two-argument ``max()`` (Postgres has only the one-argument aggregate), it
ignores VARCHAR lengths and it has no ``FOR UPDATE``. That gap shipped a Conversas
page that 500'd on the server with ``function max(timestamptz, timestamptz) does not
exist`` while every test was green. These tests compile the statements against the
PostgreSQL dialect so a dialect-specific mistake cannot ship untested again.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Contact, JobState, Order, RecoveryJob
from app.queries import contact_last_activity


def _pg(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_conversations_order_by_compiles_for_postgres():
    sql = _pg(select(Contact).order_by(contact_last_activity().desc().nullslast(), Contact.wa_id))
    # The bug was `ORDER BY max(coalesce(...), coalesce(...)) DESC`: `max` is a
    # one-argument AGGREGATE on Postgres, and an aggregate in ORDER BY without a
    # GROUP BY is invalid anyway.
    assert "max(" not in sql.lower()
    assert "CASE WHEN" in sql
    assert "ORDER BY" in sql and "NULLS LAST" in sql


def test_conversations_ordering_is_correct_on_sqlite(session, frozen_clock):
    from datetime import timedelta

    from app.queries import conversations

    now = frozen_clock.now
    session.add_all(
        [
            Contact(wa_id="55A", last_inbound_at=now - timedelta(hours=5), last_outbound_at=None),
            Contact(wa_id="55B", last_inbound_at=None, last_outbound_at=now - timedelta(hours=1)),
            Contact(
                wa_id="55C",
                last_inbound_at=now - timedelta(days=3),
                last_outbound_at=now - timedelta(minutes=2),
            ),
            Contact(wa_id="55D", last_inbound_at=None, last_outbound_at=None),
        ]
    )
    session.flush()
    # C (2 min ago, via outbound) → B (1 h) → A (5 h) → D (never, NULLS LAST).
    assert [c.wa_id for c in conversations(session)] == ["55C", "55B", "55A", "55D"]


@pytest.mark.parametrize(
    "stmt",
    [
        # The worker's candidate scan: needs a TOTAL order so concurrent workers walk
        # the same rows in the same sequence.
        select(RecoveryJob.id, RecoveryJob.order_id)
        .where(RecoveryJob.state == JobState.SCHEDULED.value)
        .order_by(RecoveryJob.run_at, RecoveryJob.id),
        # The claim protocol: order first (plain FOR UPDATE), then the job (SKIP LOCKED).
        select(Order).where(Order.id == 1).with_for_update(),
        select(RecoveryJob).where(RecoveryJob.id == 1).with_for_update(skip_locked=True),
    ],
)
def test_worker_statements_compile_for_postgres(stmt):
    assert _pg(stmt)


def test_optout_cancel_never_locks_a_join():
    """The opt-out path must lock ORDER first, then the job — never a bare join."""
    import inspect

    from app import optout

    source = inspect.getsource(optout.cancel_jobs_for_phones)
    # A `.join(...).with_for_update()` locks BOTH tables and, because the join drives
    # from recovery_jobs, takes the job lock first: an ABBA deadlock against the worker.
    candidate_scan = source.split("candidates = ")[1].split("jobs:")[0]
    assert "with_for_update" not in candidate_scan
    # In the per-row loop the ORDER lock must be taken before the job lock.
    lock_section = source.split("for job_id, order_id in candidates:")[1]
    assert lock_section.index("select(Order.id)") < lock_section.index("select(RecoveryJob)")
