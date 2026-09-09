"""The 12-month retention the public privacy policy promises is actually performed.

`app/templates/pages/privacidade.html` §7 is published under a real CNPJ and states
that order and message data — consent evidence included — is deleted or anonymised
after 12 months. Nothing enforced it until `app/retention.py` existed; these tests
are what keep the page and the code honest.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.kirvano import handle_event
from app.models import Contact, Message, MessageDirection, OptOut, Order, WebhookEvent
from app.optout import add_opt_out
from app.retention import RETENTION_DAYS, purge
from tests.conftest import kirvano_payload


def _old_order(session, settings, frozen_clock, sale_id: str, age_days: int) -> Order:
    """An order (plus its raw event) created ``age_days`` ago."""
    frozen_clock.set(frozen_clock.now - timedelta(days=age_days))
    handle_event(session, kirvano_payload(sale_id=sale_id), settings=settings)
    order = session.execute(select(Order).where(Order.sale_id == sale_id)).scalar_one()
    frozen_clock.set(frozen_clock.now + timedelta(days=age_days))
    return order


def test_privacy_page_and_the_code_agree_on_12_months():
    from pathlib import Path

    page = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "pages" / "privacidade.html"
    ).read_text(encoding="utf-8")
    assert "12 meses" in page
    assert RETENTION_DAYS == 365


def test_purge_anonymises_old_orders_and_keeps_the_counters(session, settings, frozen_clock):
    old = _old_order(session, settings, frozen_clock, "OLD1", age_days=400)
    recent = _old_order(session, settings, frozen_clock, "NEW1", age_days=30)
    assert old.customer_name == "Fulano de Tal"
    old.consent_ip = "200.152.1.115"  # the conftest payload has no `ip`; the real one does
    old.consent_at = old.created_at
    session.flush()

    result = purge(session, now=frozen_clock.now)
    session.commit()

    session.refresh(old)
    session.refresh(recent)
    assert result.orders_anonymised == 1
    # Identifying columns gone...
    assert old.customer_name is None and old.customer_email is None
    assert old.phone_e164 is None and old.phone_alt is None and old.phone_raw is None
    assert old.wa_id is None and old.consent_ip is None and old.pix_code is None
    # ...but the row survives, so the Início counters keep working.
    assert old.sale_id == "OLD1" and old.amount_cents and old.status == "pending"
    # A recent order is untouched.
    assert recent.customer_name == "Fulano de Tal" and recent.phone_e164 == "5511987654321"


def test_purge_anonymises_messages_and_deletes_old_events(session, settings, frozen_clock):
    _old_order(session, settings, frozen_clock, "OLD2", age_days=400)
    old_ts = frozen_clock.now - timedelta(days=400)
    session.add(
        Message(
            direction=MessageDirection.IN.value,
            wa_id="5511987654321",
            phone="5511987654321",
            wa_message_id="wamid.OLD",
            kind="text",
            body="já paguei, obrigado",
            status="received",
            created_at=old_ts,
        )
    )
    session.add(
        Message(
            direction=MessageDirection.OUT.value,
            wa_id="5511987654321",
            phone="5511987654321",
            wa_message_id="wamid.RECENT",
            kind="template",
            body="lembrete",
            status="sent",
            created_at=frozen_clock.now - timedelta(days=2),
        )
    )
    session.add(Contact(wa_id="5511987654321", phone="5511987654321", profile_name="Fulano",
                        last_inbound_at=old_ts, last_outbound_at=old_ts))  # fmt: skip
    session.flush()

    result = purge(session, now=frozen_clock.now)
    session.commit()

    msgs = {m.wa_message_id: m for m in session.execute(select(Message)).scalars()}
    assert result.messages_anonymised == 1
    assert msgs["wamid.OLD"].body is None and msgs["wamid.OLD"].phone is None
    # The row itself stays: direction/kind/status feed the counters.
    assert msgs["wamid.OLD"].direction == "in" and msgs["wamid.OLD"].status == "received"
    assert msgs["wamid.RECENT"].body == "lembrete"

    contact = session.get(Contact, "5511987654321")
    assert result.contacts_anonymised == 1
    assert contact.profile_name is None and contact.phone is None

    # Raw vendor payloads are pure liability after a year.
    assert result.events_deleted == 1
    assert session.execute(select(func.count()).select_from(WebhookEvent)).scalar_one() == 0


def test_purge_never_removes_an_opt_out(session, settings, frozen_clock):
    """The opt-out list is what keeps a customer who said SAIR from being messaged."""
    add_opt_out(
        session,
        phone="5511987654321",
        wa_id=None,
        source="text",
        now=frozen_clock.now - timedelta(days=900),
    )
    session.flush()
    purge(session, now=frozen_clock.now)
    session.commit()
    assert session.execute(select(func.count()).select_from(OptOut)).scalar_one() == 2


def test_purge_is_idempotent_and_dry_run_writes_nothing(session, settings, frozen_clock):
    old = _old_order(session, settings, frozen_clock, "OLD3", age_days=400)

    dry = purge(session, now=frozen_clock.now, dry_run=True)
    assert dry.orders_anonymised == 1
    session.refresh(old)
    assert old.customer_name == "Fulano de Tal"  # nothing written

    first = purge(session, now=frozen_clock.now)
    session.commit()
    assert first.orders_anonymised == 1
    # A second run finds nothing left to do — no endless re-writing of the same rows.
    second = purge(session, now=frozen_clock.now)
    session.commit()
    assert second.total == 0


def test_purge_script_runs_end_to_end(session, settings, frozen_clock, engine, capsys):
    from scripts import purge_old_data

    _old_order(session, settings, frozen_clock, "OLD4", age_days=400)
    session.commit()

    assert purge_old_data.main(["--dry-run"], settings=settings) == 0
    assert "SIMULACAO" in capsys.readouterr().out
    assert purge_old_data.main([], settings=settings) == 0
    out = capsys.readouterr().out
    assert "1 pedidos anonimizados" in out
    assert purge_old_data.main(["--days", "0"], settings=settings) == 2
