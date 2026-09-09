"""Side effects of Meta-originated events: template status gating, 131050, batching.

Three defects live here:

* ``FLAGGED``/``PENDING_DELETION`` blocked every send although Meta still delivers on
  them — a single quality-warning webhook silently killed the recovery flow;
* the two Meta-originated opt-out paths (a ``failed`` status with 131050, and the
  worker's own 131050) recorded the opt-out but left the customer's other jobs showing
  "agendado" until their run_at;
* a duplicate inbound message rolled back the WHOLE transaction, discarding the
  delivery statuses Meta had batched into the same request.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.inbound import apply_template_status, handle_meta_webhook, record_outbound_message
from app.kirvano import handle_event
from app.models import Alert, JobState, Message, MessageStatus, OptOut, RecoveryJob, TemplateStatus
from app.scheduling import TEMPLATE_BLOCKING_STATUSES, TEMPLATE_WARNING_STATUSES, template_available
from app.whatsapp import TemplateStatusUpdate
from tests.conftest import kirvano_payload, meta_status_payload


def _status_update(event: str) -> TemplateStatusUpdate:
    return TemplateStatusUpdate(
        event=event, name="pix_pendente_v2", language="pt_BR", reason="quality"
    )


@pytest.mark.parametrize("event", sorted(TEMPLATE_WARNING_STATUSES))
def test_warning_statuses_do_not_block_sending(session, store, frozen_clock, event):
    """FLAGGED is a quality WARNING and PENDING_DELETION still sends until deleted."""
    apply_template_status(session, _status_update(event), now=frozen_clock.now)
    session.flush()
    ok, reason = template_available(session, store)
    assert ok is True and reason is None
    # Still stored and shown in the panel, and still worth an alert — just not blocking.
    assert session.get(TemplateStatus, ("pix_pendente_v2", "pt_BR")).status == event
    alert = session.execute(select(Alert)).scalar_one()
    assert alert.code == "template_status" and alert.level == "warning"


@pytest.mark.parametrize("event", sorted(TEMPLATE_BLOCKING_STATUSES))
def test_blocking_statuses_stop_sending(session, store, frozen_clock, event):
    apply_template_status(session, _status_update(event), now=frozen_clock.now)
    session.flush()
    ok, reason = template_available(session, store)
    assert ok is False and reason == "template_unavailable"
    assert session.execute(select(Alert)).scalar_one().level == "error"


def test_flagged_template_still_sends_the_scheduled_reminder(
    session, settings, store, frozen_clock
):
    """End to end: a FLAGGED webhook on an ad launch day must not kill the flow."""
    from app.scheduling import claim_due_jobs

    handle_event(session, kirvano_payload(), settings=settings)
    apply_template_status(session, _status_update("FLAGGED"), now=frozen_clock.now)
    session.commit()
    frozen_clock.advance(minutes=10)

    claimed = claim_due_jobs(session, store, now=frozen_clock.now)
    assert len(claimed) == 1
    assert claimed[0].job.state == JobState.SENDING.value


def test_131050_status_webhook_cancels_the_customers_other_jobs(session, settings, frozen_clock):
    handle_event(session, kirvano_payload(sale_id="S1", phone="5511987654321"), settings=settings)
    record_outbound_message(
        session,
        wa_id="5511987654321",
        phone="5511987654321",
        message_id="wamid.OUT1",
        kind="template",
        body="lembrete",
        now=frozen_clock.now,
    )
    session.commit()

    payload = meta_status_payload(
        "wamid.OUT1",
        "failed",
        recipient="5511987654321",
        error={"code": 131050, "title": "Recipient has opted out"},
    )
    result = handle_meta_webhook(session, payload, client=None)
    assert result.statuses_updated == 1

    # The opt-out is recorded (both phone variants)...
    assert {r.phone for r in session.execute(select(OptOut)).scalars()} == {
        "5511987654321",
        "551187654321",
    }
    # ...and the scheduled job is cancelled NOW, not left showing "agendado" in the
    # panel until claim_due_jobs would have skipped it at fire time.
    job = session.execute(select(RecoveryJob)).scalar_one()
    assert job.state == JobState.CANCELLED.value and job.reason == "opted_out"


def test_duplicate_message_does_not_discard_the_batched_statuses(
    session, settings, frozen_clock, monkeypatch
):
    """Meta batches statuses and messages; a redelivered message must not undo them.

    The unique-violation branch is concurrency-only (a cheap existence check catches
    ordinary redeliveries), so the race is simulated: a competing INSERT of the same
    ``wa_message_id`` lands between our check and our own INSERT.
    """
    record_outbound_message(
        session,
        wa_id="5511987654321",
        phone="5511987654321",
        message_id="wamid.OUT2",
        kind="template",
        body="lembrete",
        now=frozen_clock.now,
    )
    session.commit()

    import app.inbound as inbound_module

    real_upsert = inbound_module.upsert_contact
    fired = {"once": False}

    def racing_upsert(sess, wa_id, **kwargs):
        contact = real_upsert(sess, wa_id, **kwargs)
        if not fired["once"]:
            fired["once"] = True
            sess.add(
                Message(
                    direction="in",
                    wa_id=wa_id,
                    phone=wa_id,
                    wa_message_id="wamid.IN9",
                    kind="text",
                    body="obrigado",
                    status="received",
                    status_updated_at=frozen_clock.now,
                    created_at=frozen_clock.now,
                )
            )
            sess.flush()
        return contact

    monkeypatch.setattr(inbound_module, "upsert_contact", racing_upsert)

    # One webhook body carrying BOTH a delivery status and the (now duplicate) message.
    payload = meta_status_payload("wamid.OUT2", "delivered", recipient="5511987654321")
    payload["entry"][0]["changes"][0]["value"]["contacts"] = [
        {"profile": {"name": "Fulano"}, "wa_id": "5511987654321"}
    ]
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": "5511987654321",
            "id": "wamid.IN9",  # duplicate
            "timestamp": "1757343600",
            "type": "text",
            "text": {"body": "obrigado"},
        }
    ]
    result = handle_meta_webhook(session, payload, client=None)

    assert result.messages_duplicate == 1
    assert result.statuses_updated == 1
    out = session.execute(select(Message).where(Message.wa_message_id == "wamid.OUT2")).scalar_one()
    # Before the savepoint fix this was still "sent": the duplicate's rollback threw
    # away the status the response had just reported as applied.
    assert out.status == MessageStatus.DELIVERED.value
