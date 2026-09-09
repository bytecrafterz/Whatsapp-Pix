"""Graph error mapping and the worker's delivery/recording flow (HTTP mocked with respx)."""

import json
from datetime import timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.kirvano import handle_event
from app.models import Alert, Contact, Message, OptOut, Order, RecoveryJob, TemplateStatus
from app.whatsapp import ErrorAction, GraphClient, GraphError, classify_error, fail_reason_for
from app.worker import Worker
from tests.conftest import GRAPH_MESSAGES_URL, graph_error, graph_success, kirvano_payload


@pytest.mark.parametrize(
    ("code", "action"),
    [
        (131009, ErrorAction.SANITIZE_RETRY),
        (131026, ErrorAction.RETRY_ALT_NUMBER),
        (132000, ErrorAction.FAIL),
        (132001, ErrorAction.FAIL),
        (132012, ErrorAction.SANITIZE_RETRY),
        (132018, ErrorAction.SANITIZE_RETRY),
        (132015, ErrorAction.TEMPLATE_UNAVAILABLE),
        (132016, ErrorAction.TEMPLATE_UNAVAILABLE),
        (131047, ErrorAction.WINDOW_CLOSED),
        (131049, ErrorAction.NO_RETRY_24H),
        (131050, ErrorAction.OPT_OUT),
        (130429, ErrorAction.BACKOFF),
        (131056, ErrorAction.BACKOFF),
        (80007, ErrorAction.BACKOFF),
        (190, ErrorAction.TOKEN_INVALID),
        (401, ErrorAction.TOKEN_INVALID),
        (999999, ErrorAction.FAIL),
    ],
)
def test_classify_error_table(code, action):
    assert classify_error(GraphError(code=code, message="m", http_status=400)) == action


def test_classify_error_edge_cases():
    assert classify_error(None) == ErrorAction.FAIL
    assert classify_error(GraphError(code=None, message="timeout")) == ErrorAction.BACKOFF
    assert (
        classify_error(GraphError(code=None, message="500", http_status=500)) == ErrorAction.BACKOFF
    )
    assert (
        classify_error(GraphError(code=12345, message="x", http_status=503)) == ErrorAction.BACKOFF
    )
    assert fail_reason_for(GraphError(code=131026, message="")) == "not_on_whatsapp"
    assert fail_reason_for(GraphError(code=131049, message="")) == "marketing_limit_24h"
    assert fail_reason_for(None) == "unknown_error"


# --- worker flow -----------------------------------------------------------------------------


def _due_job(session, settings, frozen_clock, **kw) -> tuple[Order, RecoveryJob]:
    handle_event(session, kirvano_payload(**kw), settings=settings)
    order = session.execute(select(Order)).scalar_one()
    job = session.execute(select(RecoveryJob)).scalar_one()
    frozen_clock.advance(minutes=10)
    return order, job


def _worker(settings, session) -> Worker:
    return Worker(
        settings, session_factory=lambda: session, client=GraphClient(settings), poll_seconds=0.01
    )


def _sent_to(route, i: int) -> str:
    return json.loads(route.calls[i].request.content)["to"]


@respx.mock
def test_success_records_job_message_contact(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(
            200, json=graph_success(wa_id="551187654321", message_id="wamid.SENT")
        )
    )
    order, job = _due_job(session, settings, frozen_clock)
    assert _worker(settings, session).run_once(frozen_clock.now) == 1
    assert route.call_count == 1
    req = json.loads(route.calls[0].request.content)
    assert req["to"] == "5511987654321"
    assert route.calls[0].request.headers["authorization"] == "Bearer meta-test-token"
    session.refresh(job)
    session.refresh(order)
    assert (
        job.state == "sent" and job.wa_message_id == "wamid.SENT" and job.sent_to == "5511987654321"
    )
    assert job.sent_at == frozen_clock.now and job.attempts == 1
    assert order.wa_id == "551187654321"  # canonical id from Meta may differ from what we sent
    msg = session.execute(select(Message)).scalar_one()
    assert msg.direction == "out" and msg.kind == "template" and msg.wa_message_id == "wamid.SENT"
    assert msg.order_id == order.id and msg.template_name == "pix_pendente_v2"
    assert "Fulano" in msg.body
    contact = session.get(Contact, "551187654321")
    assert contact is not None and contact.last_outbound_at == frozen_clock.now


@respx.mock
def test_131026_retries_alternate_number_then_succeeds(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        side_effect=[
            Response(400, json=graph_error(131026, "Message Undeliverable")),
            Response(200, json=graph_success(wa_id="551187654321", message_id="wamid.ALT")),
        ]
    )
    order, job = _due_job(session, settings, frozen_clock)
    _worker(settings, session).run_once(frozen_clock.now)
    assert route.call_count == 2
    assert _sent_to(route, 0) == "5511987654321"
    assert _sent_to(route, 1) == "551187654321"
    session.refresh(job)
    assert (
        job.state == "sent" and job.sent_to == "551187654321" and job.wa_message_id == "wamid.ALT"
    )


@respx.mock
def test_131026_on_both_numbers_fails_not_on_whatsapp(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(131026))
    )
    order, job = _due_job(session, settings, frozen_clock)
    _worker(settings, session).run_once(frozen_clock.now)
    assert route.call_count == 2
    session.refresh(job)
    assert job.state == "failed" and job.reason == "not_on_whatsapp" and job.error_code == "131026"
    assert session.execute(select(Message)).scalar_one_or_none() is None


@respx.mock
def test_131049_marketing_limit_no_retry_within_24h(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(131049))
    )
    order, job = _due_job(session, settings, frozen_clock)
    _worker(settings, session).run_once(frozen_clock.now)
    assert route.call_count == 1
    session.refresh(job)
    assert job.state != "sent" and job.attempts == 1 and job.error_code == "131049"
    if job.state == "scheduled":
        assert job.run_at >= frozen_clock.now + timedelta(hours=24)
    else:
        assert job.state == "skipped" and job.reason == "expires_too_soon"
    # Nothing is claimed again in the next 24h.
    frozen_clock.advance(hours=23)
    assert _worker(settings, session).run_once(frozen_clock.now) == 0
    assert route.call_count == 1


@respx.mock
def test_131009_sanitize_retry_uses_hard_mode(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        side_effect=[
            Response(400, json=graph_error(131009, "Parameter value is not valid", 2494073)),
            Response(200, json=graph_success()),
        ]
    )
    order, job = _due_job(session, settings, frozen_clock, name="Joãozinho\tda   Silva")
    _worker(settings, session).run_once(frozen_clock.now)
    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)["template"]["components"][0]["parameters"][
        0
    ]["text"]
    second = json.loads(route.calls[1].request.content)["template"]["components"][0]["parameters"][
        0
    ]["text"]
    assert first == "Joãozinho" and second == "Joaozinho"
    session.refresh(job)
    assert job.state == "sent"


@respx.mock
def test_rate_limit_backoff_requeues(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(429, json=graph_error(130429, "Rate limit"))
    )
    order, job = _due_job(
        session, settings, frozen_clock, expires_at=frozen_clock.now + timedelta(hours=3)
    )
    _worker(settings, session).run_once(frozen_clock.now)
    session.refresh(job)
    assert job.state == "scheduled" and job.attempts == 1
    assert job.run_at == frozen_clock.now + timedelta(seconds=60)
    assert job.reason == "retry_130429"
    assert route.call_count == 1


@respx.mock
def test_network_error_backs_off(session, settings, frozen_clock):
    import httpx

    respx.post(GRAPH_MESSAGES_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    order, job = _due_job(
        session, settings, frozen_clock, expires_at=frozen_clock.now + timedelta(hours=3)
    )
    _worker(settings, session).run_once(frozen_clock.now)
    session.refresh(job)
    assert job.state == "scheduled" and job.attempts == 1 and job.error_code == "network"


@respx.mock
def test_token_invalid_pauses_worker_and_alerts(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(401, json=graph_error(190, "Invalid OAuth access token"))
    )
    order, job = _due_job(
        session, settings, frozen_clock, expires_at=frozen_clock.now + timedelta(hours=3)
    )
    worker = _worker(settings, session)
    worker.run_once(frozen_clock.now)
    session.refresh(job)
    assert job.state == "scheduled" and job.reason == "token_invalid" and job.attempts == 0
    assert worker.paused_until == frozen_clock.now + timedelta(minutes=10)
    alert = session.execute(select(Alert)).scalar_one()
    assert alert.code == "token_invalid"
    frozen_clock.advance(minutes=5)
    assert worker.run_once(frozen_clock.now) == 0  # paused: no claim, no HTTP
    assert route.call_count == 1


@respx.mock
def test_template_disabled_marks_template_and_fails(session, settings, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(132016, "Template disabled"))
    )
    order, job = _due_job(session, settings, frozen_clock)
    _worker(settings, session).run_once(frozen_clock.now)
    session.refresh(job)
    assert job.state == "failed" and job.reason == "template_disabled"
    row = session.get(TemplateStatus, ("pix_pendente_v2", "pt_BR"))
    assert row.status == "DISABLED"
    assert session.execute(select(Alert)).scalar_one().code == "template_unavailable"


@respx.mock
def test_131050_records_opt_out(session, settings, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(return_value=Response(400, json=graph_error(131050)))
    order, job = _due_job(session, settings, frozen_clock)
    _worker(settings, session).run_once(frozen_clock.now)
    session.refresh(job)
    assert job.state == "failed" and job.reason == "opted_out"
    assert {r.phone for r in session.execute(select(OptOut)).scalars()} == {
        "5511987654321",
        "551187654321",
    }


@respx.mock
def test_param_count_mismatch_fails_with_alert(session, settings, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(return_value=Response(400, json=graph_error(132000)))
    order, job = _due_job(session, settings, frozen_clock)
    _worker(settings, session).run_once(frozen_clock.now)
    session.refresh(job)
    assert job.state == "failed" and job.reason == "param_count_mismatch"
    assert session.execute(select(Alert)).scalar_one().code == "graph_132000"


def test_worker_without_token_does_not_claim(session, settings, frozen_clock, monkeypatch):
    monkeypatch.setattr(settings, "meta_access_token", None)
    order, job = _due_job(session, settings, frozen_clock)
    assert _worker(settings, session).run_once(frozen_clock.now) == 0
    session.refresh(job)
    assert job.state == "scheduled"


def test_worker_heartbeat_is_written(session, settings, frozen_clock, monkeypatch):
    from app.models import WorkerHeartbeat

    monkeypatch.setattr(settings, "meta_access_token", None)
    _worker(settings, session).run_once(frozen_clock.now)
    hb = session.get(WorkerHeartbeat, 1)
    assert hb is not None and hb.beat_at == frozen_clock.now
