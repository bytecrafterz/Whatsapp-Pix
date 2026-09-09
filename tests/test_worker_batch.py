"""Batch-level worker behaviour: kill switches and per-job fault isolation.

Everything here is about what happens to the OTHER jobs of a batch that has already
been claimed (and committed as ``sending``) when one job goes wrong. Getting this
wrong is expensive in both directions: burning ``worker_batch_size`` authenticated
failures against Meta per tick, or silently destroying up to 20 reminders that were
never even attempted — and a reminder is never resent.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import func, select

from app.kirvano import handle_event
from app.models import Alert, JobState, RecoveryJob, TemplateStatus
from app.whatsapp import GraphClient
from app.worker import Worker
from tests.conftest import GRAPH_MESSAGES_URL, graph_error, graph_success, kirvano_payload


def _unique_success():
    """respx side effect handing every call its own wamid (messages.wa_message_id is unique)."""
    counter = itertools.count(1)
    return lambda request: Response(200, json=graph_success(message_id=f"wamid.OK{next(counter)}"))


def _worker(settings, session) -> Worker:
    return Worker(
        settings, session_factory=lambda: session, client=GraphClient(settings), poll_seconds=0.01
    )


def _due_batch(session, settings, frozen_clock, count: int) -> list[RecoveryJob]:
    """`count` orders, each with a job due right now (distinct phones)."""
    for i in range(count):
        handle_event(
            session,
            kirvano_payload(sale_id=f"BATCH{i:03d}", phone=f"551198765{i:04d}"),
            settings=settings,
        )
    frozen_clock.advance(minutes=10)
    jobs = list(session.execute(select(RecoveryJob).order_by(RecoveryJob.id)).scalars())
    assert len(jobs) == count
    return jobs


@respx.mock
def test_token_invalid_stops_the_rest_of_the_batch(session, settings, frozen_clock):
    """190 on the first job → exactly ONE Graph call and ONE alert for the whole batch."""
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(401, json=graph_error(190, "Invalid OAuth access token"))
    )
    jobs = _due_batch(session, settings, frozen_clock, 5)
    worker = _worker(settings, session)
    assert worker.run_once(frozen_clock.now) == 5

    # The guard used to be `if self._stop and self.paused_until`, and `_stop` is only
    # set by SIGTERM — so all 5 jobs POSTed with a token Meta had already rejected.
    assert route.call_count == 1
    assert worker.paused_until == frozen_clock.now + timedelta(minutes=10)
    for job in jobs:
        session.refresh(job)
        # Every job is back in `scheduled` (nothing stranded in `sending`) and waits
        # for the pause to end.
        assert job.state == JobState.SCHEDULED.value, job.id
        assert job.reason == "token_invalid"
    assert jobs[1].run_at == worker.paused_until

    # One dead token is one problem: not one alert row per claimed job.
    alerts = list(session.execute(select(Alert)).scalars())
    assert len(alerts) == 1 and alerts[0].code == "token_invalid"


@respx.mock
def test_template_unavailable_stops_the_rest_of_the_batch(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(132015, "Template paused"))
    )
    jobs = _due_batch(session, settings, frozen_clock, 4)
    worker = _worker(settings, session)
    worker.run_once(frozen_clock.now)

    # `template_available` is evaluated once BEFORE the loop, so without the in-batch
    # kill switch the other 3 jobs kept posting to a template Meta had just disabled.
    assert route.call_count == 1
    assert session.get(TemplateStatus, ("pix_pendente_v2", "pt_BR")).status == "PAUSED"
    session.refresh(jobs[0])
    assert jobs[0].state == JobState.FAILED.value and jobs[0].reason == "template_paused"
    for job in jobs[1:]:
        session.refresh(job)
        assert job.state == JobState.SCHEDULED.value
        assert job.reason == "template_unavailable"
    assert session.execute(select(func.count()).select_from(Alert)).scalar_one() == 1

    # The flag is per-tick: the next tick re-reads template_status (now PAUSED), so
    # claim_due_jobs skips the jobs instead of the worker trusting a stale flag.
    assert worker.template_blocked is True
    frozen_clock.advance(minutes=1)
    worker.run_once(frozen_clock.now)
    assert worker.template_blocked is False
    assert route.call_count == 1
    session.refresh(jobs[1])
    assert jobs[1].state == JobState.SKIPPED.value and jobs[1].reason == "template_unavailable"


@respx.mock
def test_a_recording_failure_does_not_abandon_the_batch(
    session, settings, frozen_clock, monkeypatch
):
    """mark_sent blowing up on job 1 must not strand jobs 2..n in `sending`."""
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    jobs = _due_batch(session, settings, frozen_clock, 3)

    import app.worker as worker_module

    real_mark_sent = worker_module.mark_sent
    calls = {"n": 0}

    def flaky_mark_sent(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")
        return real_mark_sent(*args, **kwargs)

    monkeypatch.setattr(worker_module, "mark_sent", flaky_mark_sent)
    _worker(settings, session).run_once(frozen_clock.now)

    # Before the fix the exception escaped the `for item in claimed` loop and jobs 2
    # and 3 were left `sending` with ZERO attempts — the reaper then failed them and,
    # since a reminder is never resent, they were lost.
    assert route.call_count == 3
    session.refresh(jobs[0])
    # Job 1's request DID reach Meta, so it must NOT be released for a retry.
    assert jobs[0].state == JobState.SENDING.value
    for job in jobs[1:]:
        session.refresh(job)
        assert job.state == JobState.SENT.value


@respx.mock
def test_stop_flag_releases_untouched_claims(session, settings, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    jobs = _due_batch(session, settings, frozen_clock, 3)
    worker = _worker(settings, session)

    original = worker._process

    def process_then_stop(*args, **kwargs):
        original(*args, **kwargs)
        worker.stop()  # SIGTERM arrives while the batch is running

    worker._process = process_then_stop  # type: ignore[method-assign]
    worker.run_once(frozen_clock.now)

    session.refresh(jobs[0])
    assert jobs[0].state == JobState.SENT.value
    for job in jobs[1:]:
        session.refresh(job)
        # Back in the queue for the restarted worker — not stranded in `sending`,
        # where reap_stale_sending would have failed them for good.
        assert job.state == JobState.SCHEDULED.value and job.reason == "worker_stopping"


@respx.mock
def test_worker_opt_out_cancels_other_jobs_for_the_number(session, settings, frozen_clock):
    """131050 must cancel this customer's other scheduled jobs, like the inbound path."""
    respx.post(GRAPH_MESSAGES_URL).mock(return_value=Response(400, json=graph_error(131050)))
    handle_event(session, kirvano_payload(sale_id="OPT1", phone="5511987654321"), settings=settings)
    frozen_clock.advance(minutes=10)
    # A second order for the SAME customer, still in the future.
    handle_event(session, kirvano_payload(sale_id="OPT2", phone="5511987654321"), settings=settings)

    _worker(settings, session).run_once(frozen_clock.now)
    # The fired job failed as opted_out; the other one is cancelled right away instead
    # of showing "agendado" in the panel until its own run_at.
    jobs = list(session.execute(select(RecoveryJob).order_by(RecoveryJob.id)).scalars())
    assert jobs[0].state == JobState.FAILED.value and jobs[0].reason == "opted_out"
    assert jobs[1].state == JobState.CANCELLED.value and jobs[1].reason == "opted_out"


@pytest.mark.parametrize("code", [190, 401])
def test_repeated_token_alerts_are_deduped(session, settings, frozen_clock, code):
    from app.alerts import open_alerts, record_alert

    for _ in range(5):
        record_alert(session, "token_invalid", f"token #{code}", dedupe=True, now=frozen_clock.now)
    rows = open_alerts(session)
    assert len(rows) == 1 and rows[0].message == f"token #{code}"
    # A different code still gets its own row.
    record_alert(session, "template_unavailable", "modelo", dedupe=True, now=frozen_clock.now)
    assert len(open_alerts(session)) == 2
