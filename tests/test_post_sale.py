"""Post-sale follow-up: Kirvano SALE_APPROVED → 1–3 messages after the purchase.

The promises under test: every approved sale (card or PIX) gets the configured
messages at the configured times with the customer's name and product; Kirvano
repeating the event never sends anything twice; a refund, a chargeback or SAIR stops
what is still waiting; a message whose moment has passed is dropped instead of sent
days late; and the panel's numbers (sent, delivered, read, replies) add up.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.inbound import handle_meta_webhook
from app.kirvano import handle_event
from app.models import Alert, Message, Order, PostSale, PostSaleJob, RecoveryJob
from app.optout import add_opt_out
from app.postsale import LATE_TOLERANCE
from app.queries import post_sale_metrics
from app.retention import purge
from app.whatsapp import GraphClient
from app.worker import Worker
from tests.conftest import (
    DEFAULT_NOW,
    GRAPH_MESSAGES_URL,
    graph_error,
    graph_success,
    kirvano_payload,
    meta_status_payload,
    meta_text_payload,
)

AUTH = ("admin", "panel-pw")
ORIGIN = {"Origin": "http://testserver"}
PHONE = "5511987654321"
ACCESS = (
    "A Jornada com Meu Anjo | https://membros.example.com/jornada\n"
    "Oração de Santo Antônio | https://membros.example.com/santo-antonio"
)


def sale_payload(
    event: str = "SALE_APPROVED",
    *,
    sale_id: str = "CARD0001",
    phone: str = PHONE,
    method: str = "CREDIT_CARD",
    at: datetime | None = None,
) -> dict:
    at = at or DEFAULT_NOW
    body = kirvano_payload(
        event, sale_id=sale_id, phone=phone, method=method, created_at=at, finished_at=at
    )
    body["customer"]["name"] = "Maria Souza"
    body["customer"]["email"] = "maria@example.com"
    return body


@pytest.fixture
def post_on(session, store):
    """Post-sale messages switched on (they ship switched off)."""
    store.set("post_enabled", True)
    session.commit()
    return store


def _sales(session) -> list[PostSale]:
    return list(session.execute(select(PostSale).order_by(PostSale.id)).scalars())


def _jobs(session) -> list[PostSaleJob]:
    return list(
        session.execute(
            select(PostSaleJob).order_by(PostSaleJob.post_sale_id, PostSaleJob.step)
        ).scalars()
    )


def _worker(settings, session) -> Worker:
    return Worker(
        settings, session_factory=lambda: session, client=GraphClient(settings), poll_seconds=0.01
    )


def _unique_success():
    counter = itertools.count(1)
    return lambda request: Response(
        200, json=graph_success(message_id=f"wamid.POST{next(counter)}")
    )


# --- starting the sequence ------------------------------------------------------------


def test_card_sale_schedules_the_follow_up(session, settings, post_on, frozen_clock):
    res = handle_event(session, sale_payload(), settings=settings)
    # The PIX flow has no use for a card sale, but the follow-up is what happened to it.
    assert res.outcome == "processed" and res.reason == "post_sale_scheduled_1"

    [sale] = _sales(session)
    assert sale.sale_id == "CARD0001" and sale.status == "active" and sale.reason is None
    assert sale.phone_e164 == PHONE and sale.payment_method == "CREDIT_CARD"
    assert sale.product_name == "Jornada com Meu Anjo" and sale.amount_cents == 16980
    assert sale.paid_at == DEFAULT_NOW
    [job] = _jobs(session)
    assert job.step == 1 and job.state == "scheduled"
    assert job.run_at == DEFAULT_NOW + timedelta(minutes=10)
    assert job.deadline_at == DEFAULT_NOW + timedelta(minutes=10) + LATE_TOLERANCE


def test_post_sale_ships_switched_off(session, settings, frozen_clock):
    res = handle_event(session, sale_payload(), settings=settings)
    assert res.outcome == "ignored"  # exactly as before this feature existed
    [sale] = _sales(session)
    assert sale.reason == "disabled"
    assert _jobs(session) == []


def test_kirvano_repeating_the_event_never_starts_a_second_sequence(
    session, settings, post_on, frozen_clock
):
    handle_event(session, sale_payload(), settings=settings)
    assert handle_event(session, sale_payload(), settings=settings).outcome == "duplicate"
    frozen_clock.advance(minutes=2)
    handle_event(session, sale_payload(at=frozen_clock.now), settings=settings)
    assert len(_sales(session)) == 1
    assert len(_jobs(session)) == 1


def test_pix_sale_gets_the_follow_up_and_the_reminder_is_cancelled(
    session, settings, post_on, frozen_clock
):
    handle_event(session, kirvano_payload(sale_id="PIX00001", phone=PHONE), settings=settings)
    frozen_clock.advance(minutes=5)
    res = handle_event(
        session, sale_payload(sale_id="PIX00001", method="PIX", at=frozen_clock.now),
        settings=settings,
    )  # fmt: skip
    assert res.outcome == "processed" and res.reason == "job_cancelled"  # the PIX flow's answer
    assert session.execute(select(RecoveryJob)).scalar_one().state == "cancelled"
    [job] = _jobs(session)
    assert job.state == "scheduled"


def test_sale_without_phone_or_opted_out_gets_nothing(session, settings, post_on, frozen_clock):
    add_opt_out(session, phone=PHONE, wa_id=None, source="text")
    session.commit()
    handle_event(session, sale_payload(), settings=settings)
    no_phone = sale_payload(sale_id="CARD0002")
    del no_phone["customer"]["phone_number"]
    handle_event(session, no_phone, settings=settings)
    assert [s.reason for s in _sales(session)] == ["opted_out", "no_phone"]
    assert _jobs(session) == []


def test_quiet_hours_push_the_message_to_the_morning(session, settings, post_on, frozen_clock):
    late_evening = datetime(2026, 9, 9, 1, 55, tzinfo=UTC)  # 22:55 in São Paulo
    frozen_clock.set(late_evening)
    handle_event(session, sale_payload(at=late_evening), settings=settings)
    [job] = _jobs(session)
    assert job.reason == "quiet_hours"
    assert job.run_at == datetime(2026, 9, 9, 11, 0, tzinfo=UTC)  # 08:00 in São Paulo


def test_an_event_that_arrives_late_drops_the_steps_whose_time_passed(
    session, settings, post_on, frozen_clock
):
    post_on.set_many(
        {"post_steps": 2, "post_step2_template": "pos_venda_acompanhamento_v1"}
    )  # step 2: 3 days after the sale
    session.commit()
    approved = DEFAULT_NOW - timedelta(days=2)
    handle_event(session, sale_payload(at=approved), settings=settings)
    [job] = _jobs(session)  # step 1 (10 min after) would be two days late
    assert job.step == 2 and job.run_at == approved + timedelta(days=3)

    post_on.set("post_steps", 1)
    session.commit()
    handle_event(session, sale_payload(sale_id="CARD0002", at=approved), settings=settings)
    assert _sales(session)[1].reason == "sale_too_old"


# --- stopping it -------------------------------------------------------------------------


def test_refund_and_chargeback_cancel_what_is_still_waiting(
    session, settings, post_on, frozen_clock
):
    post_on.set_many({"post_steps": 2, "post_step2_template": "pos_venda_acompanhamento_v1"})
    session.commit()
    handle_event(session, sale_payload(), settings=settings)
    handle_event(
        session, sale_payload(sale_id="CARD0002", phone="5521999990000"), settings=settings
    )
    frozen_clock.advance(hours=1)
    handle_event(session, sale_payload("SALE_REFUNDED", at=frozen_clock.now), settings=settings)
    handle_event(
        session,
        sale_payload("SALE_CHARGEBACK", sale_id="CARD0002", phone="5521999990000",
                     at=frozen_clock.now),
        settings=settings,
    )  # fmt: skip
    first, second = _sales(session)
    assert (first.status, second.status) == ("refunded", "chargeback")
    assert {(j.state, j.reason) for j in first.jobs} == {("cancelled", "refunded")}
    assert {(j.state, j.reason) for j in second.jobs} == {("cancelled", "chargeback")}
    assert post_sale_metrics(session).reversed == 2


def test_saying_sair_cancels_the_post_sale_messages(session, settings, post_on, frozen_clock):
    handle_event(session, sale_payload(), settings=settings)
    handle_meta_webhook(session, meta_text_payload(PHONE, "SAIR"))
    [job] = _jobs(session)
    assert job.state == "cancelled" and job.reason == "opted_out"


# --- sending -----------------------------------------------------------------------------


@respx.mock
def test_worker_sends_the_message_and_the_numbers_add_up(session, settings, post_on, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, sale_payload(), settings=settings)
    frozen_clock.advance(minutes=9)
    assert _worker(settings, session).run_once() == 0  # not yet
    frozen_clock.advance(minutes=1)
    assert _worker(settings, session).run_once() == 1

    body = json.loads(route.calls[0].request.content)
    template = body["template"]
    assert body["to"] == PHONE
    assert template["name"] == "pos_venda_v1" and template["language"] == {"code": "pt_BR"}
    body_part, button = template["components"]
    assert [p["text"] for p in body_part["parameters"]] == ["Maria", "Jornada com Meu Anjo"]
    # The access button carries the sale code; /a/ turns it into the product's members area.
    assert button["sub_type"] == "url" and button["index"] == "0"
    assert button["parameters"] == [{"type": "text", "text": "CARD0001"}]
    [job] = _jobs(session)
    assert job.state == "sent" and job.wa_message_id == "wamid.POST1"
    msg = session.execute(select(Message)).scalar_one()
    assert msg.template_name == "pos_venda_v1" and msg.order_id is None  # card: no order row

    handle_meta_webhook(session, meta_status_payload("wamid.POST1", "delivered"))
    handle_meta_webhook(session, meta_status_payload("wamid.POST1", "read"))
    frozen_clock.advance(hours=5)
    handle_meta_webhook(
        session, meta_text_payload(PHONE, "Obrigada!", ts=int(frozen_clock.now.timestamp()))
    )

    m = post_sale_metrics(session)
    assert (m.sales, m.reachable, m.in_sequence) == (1, 1, 1)
    assert (m.sent, m.delivered, m.read, m.replied) == (1, 1, 1, 1)
    assert (m.failed, m.scheduled, m.sent_sales) == (0, 0, 1)
    assert m.read_rate == 100.0


@respx.mock
def test_pix_sale_message_is_linked_to_its_order(session, settings, post_on, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, kirvano_payload(sale_id="PIX00001", phone=PHONE), settings=settings)
    handle_event(session, sale_payload(sale_id="PIX00001", method="PIX"), settings=settings)
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()
    order = session.execute(select(Order)).scalar_one()
    msg = session.execute(select(Message)).scalar_one()
    assert msg.order_id == order.id


@respx.mock
def test_later_steps_go_at_their_own_time_even_if_the_first_failed(
    session, settings, post_on, frozen_clock
):
    post_on.set_many(
        {
            "post_steps": 2,
            "post_step2_template": "pos_venda_acompanhamento_v1",
            "post_step2_params": "first_name",
        }
    )
    session.commit()
    responses = iter(
        [Response(400, json=graph_error(131000, "something went wrong"))]
        + [Response(200, json=graph_success(message_id="wamid.POST2"))]
    )
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=lambda request: next(responses))
    handle_event(session, sale_payload(), settings=settings)
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()
    frozen_clock.advance(days=3)
    _worker(settings, session).run_once()
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)["template"]
    assert second["name"] == "pos_venda_acompanhamento_v1"
    assert [j.state for j in _jobs(session)] == ["failed", "sent"]
    assert post_sale_metrics(session).failed == 1


@respx.mock
def test_switching_off_holds_the_messages_until_their_time_has_passed(
    session, settings, post_on, frozen_clock
):
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, sale_payload(), settings=settings)
    post_on.set("post_enabled", False)
    session.commit()
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()
    [job] = _jobs(session)
    assert job.state == "scheduled" and job.reason == "disabled"
    frozen_clock.set(job.deadline_at + timedelta(minutes=1))
    _worker(settings, session).run_once()
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "too_late"
    assert route.call_count == 0


@respx.mock
def test_paused_template_is_reported_and_not_retried(session, settings, post_on, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(132015, "template paused"))
    )
    handle_event(session, sale_payload(), settings=settings)
    handle_event(
        session, sale_payload(sale_id="CARD0002", phone="5521999990000"), settings=settings
    )
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()
    assert route.call_count == 1  # the second sale's message was held back, not posted
    first, second = _jobs(session)
    assert first.state == "failed"
    assert second.state == "scheduled" and second.reason == "template_unavailable"
    alert = session.execute(select(Alert)).scalar_one()
    assert alert.code == "post_sale_template_unavailable" and "pos_venda_v1" in alert.message


@respx.mock
def test_daily_contact_limit_is_shared_with_the_other_flows(
    session, settings, post_on, frozen_clock
):
    post_on.set("daily_recipient_limit", 1)
    session.commit()
    respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(
        session, kirvano_payload(sale_id="PIXA0001", phone="5521911112222"), settings=settings
    )
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()  # the PIX reminder uses the only slot
    handle_event(session, sale_payload(at=frozen_clock.now), settings=settings)
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()
    [job] = _jobs(session)
    assert job.state == "skipped" and job.reason == "daily_limit"


def test_access_button_leads_to_the_members_area_of_the_product_bought(
    client, session, settings, post_on, frozen_clock
):
    """Two products, two members areas, one template: the sale code picks the link."""
    post_on.set_many(
        {"post_access_links": ACCESS, "post_access_url": "https://membros.example.com/geral"}
    )
    session.commit()
    for sale_id, product in [
        ("CARD0001", "A Jornada com meu Anjo"),
        ("CARD0002", "Oracao de Santo Antonio"),  # accents and case do not matter
        ("CARD0003", "Outro produto"),
    ]:
        body = sale_payload(sale_id=sale_id)
        body["products"][0]["name"] = product
        handle_event(session, body, settings=settings)

    def location(sale_id: str) -> str:
        r = client.get(f"/a/{sale_id}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
        return r.headers["location"]

    assert location("CARD0001") == "https://membros.example.com/jornada"
    assert location("CARD0002") == "https://membros.example.com/santo-antonio"
    assert location("CARD0003") == "https://membros.example.com/geral"  # not listed
    # "{{1}}" typed into the template's URL field arrives literally in front of the code.
    assert location("%7B%7B1%7D%7DCARD0001") == "https://membros.example.com/jornada"
    assert client.get("/a/NOPE0000", follow_redirects=False).status_code == 404


def test_link_parameter_is_the_access_url(session, settings, store):
    from app.whatsapp import build_post_sale_params

    store.set("post_step1_params", "first_name,link")
    sale = PostSale(sale_id="CARD0001", customer_name="Maria Souza", paid_at=DEFAULT_NOW)
    params = build_post_sale_params(sale, store.post_step(1), settings=settings)
    assert params == ["Maria", "https://api.test.local/a/CARD0001"]


# --- panel --------------------------------------------------------------------------------


def _nonce(client) -> str:
    page = client.get("/painel/pos-venda", auth=AUTH)
    assert page.status_code == 200, page.text[:300]
    return re.search(r'name="nonce" value="([0-9a-f]{64})"', page.text).group(1)


def _form(nonce: str, **overrides: str) -> dict[str, str]:
    data = {
        "nonce": nonce,
        "post_enabled": "true",
        "post_steps": "1",
        "post_template_language": "pt_BR",
    }
    for i, (delay, name) in enumerate(
        [("10", "pos_venda_v1"), ("4320", ""), ("10080", "")], start=1
    ):
        data[f"post_step{i}_delay_minutes"] = delay
        data[f"post_step{i}_template"] = name
        data[f"post_step{i}_params"] = "first_name,product"
        data[f"post_step{i}_url_button_index"] = "0" if i == 1 else "-1"
    data["post_access_links"] = ACCESS
    data["post_access_url"] = ""
    data.update(overrides)
    return data


def test_pos_venda_page_shows_numbers_and_recent_sales(client, session, settings, post_on):
    handle_event(session, sale_payload(), settings=settings)
    page = client.get("/painel/pos-venda", auth=AUTH)
    assert page.status_code == 200
    assert "vendas aprovadas" in page.text and "clientes responderam" in page.text
    assert "Jornada com Meu Anjo" in page.text and PHONE in page.text
    assert 'href="/painel/pos-venda"' in client.get("/painel", auth=AUTH).text
    # Configurações keeps only the PIX settings.
    assert "post_enabled" not in client.get("/painel/configuracoes", auth=AUTH).text


def test_pos_venda_settings_are_saved(client, session, store):
    r = client.post(
        "/painel/pos-venda",
        auth=AUTH,
        data=_form(_nonce(client), post_step1_params="first_name,email"),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    store.refresh()
    assert store.post_enabled is True
    assert store.post_step(1).params == ("first_name", "email")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"post_step1_template": ""}, "Informe o nome do modelo"),
        ({"post_step1_params": "first_name,coupon"}, "Parâmetro desconhecido"),
        (
            {
                "post_steps": "2",
                "post_step2_template": "pos_venda_v2",
                "post_step2_delay_minutes": "30",
            },
            "pelo menos 60 minutos",
        ),
        ({"post_step1_delay_minutes": "99999"}, "Use um número entre 0 e 43200"),
        ({"post_access_links": ""}, "Informe os links de acesso"),
        ({"post_access_links": "Produto sem link"}, "Linha inválida"),
    ],
)
def test_pos_venda_settings_are_validated(client, session, store, overrides, message):
    r = client.post(
        "/painel/pos-venda", auth=AUTH, data=_form(_nonce(client), **overrides), headers=ORIGIN
    )
    assert r.status_code == 400
    assert message in r.text
    store.refresh()
    assert store.post_enabled is False  # nothing saved


def test_pos_venda_form_needs_the_nonce(client, session, store):
    r = client.post("/painel/pos-venda", auth=AUTH, data=_form("0" * 64), headers=ORIGIN)
    assert r.status_code == 403


# --- retention ----------------------------------------------------------------------------


def test_purge_anonymises_old_sales_and_keeps_the_numbers(session, settings, post_on, frozen_clock):
    old = DEFAULT_NOW - timedelta(days=400)
    frozen_clock.set(old)
    handle_event(session, sale_payload(at=old), settings=settings)
    frozen_clock.set(DEFAULT_NOW)
    result = purge(session, now=DEFAULT_NOW)
    session.commit()
    assert result.post_sales_anonymised == 1
    [sale] = _sales(session)
    assert sale.customer_name is None and sale.phone_e164 is None and sale.customer_email is None
    assert sale.sale_id == "CARD0001" and sale.amount_cents == 16980
