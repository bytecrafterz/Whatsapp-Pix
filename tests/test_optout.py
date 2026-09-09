"""Opt-out via text and via button: cancels jobs, blocks scheduling, replies once."""

import respx
from httpx import Response
from sqlalchemy import func, select

from app.inbound import OPT_OUT_REPLY, handle_meta_webhook, is_opt_out_text, normalize_text
from app.kirvano import handle_event
from app.models import Message, OptOut, Order, RecoveryJob
from app.optout import is_opted_out, remove_opt_out
from app.whatsapp import GraphClient
from tests.conftest import (
    GRAPH_MESSAGES_URL,
    graph_success,
    kirvano_payload,
    meta_button_payload,
    meta_text_payload,
)


def test_opt_out_text_detection():
    for text in (
        "SAIR",
        "sair",
        "Parar",
        "pare",
        "STOP!",
        "cancelar",
        "Não quero",
        "nao quero mais",
        "NÃO QUERO RECEBER",
        "descadastrar",
        " sair. ",
        # SPEC line 80 asks for KEYWORD matching: the keyword counts anywhere in the
        # sentence. These were false negatives while the match required the keyword to
        # be the WHOLE message — we kept messaging people who had asked us to stop.
        "pare de me enviar mensagens",
        "quero sair da lista",
        "me descadastre",
        "quero sair da fila amanhã",
        "parar de pagar?",
        "nao quero mais receber nada",
    ):
        assert is_opt_out_text(text), text
    for text in (
        "quero pagar",
        "ok",
        "obrigado",
        "",
        None,
        # Negated verbs mean the OPPOSITE: these used to opt the customer out because
        # "nao quero" was matched as a bare substring.
        "nao quero cancelar meu pedido",
        "não quero perder essa oferta",
        "nao quero errar o pagamento",
        "nao vou cancelar",
        # Word boundaries: a keyword must be a whole word.
        "separar os produtos",
        "cancelamento?",
    ):
        assert not is_opt_out_text(text), text
    assert normalize_text("Não Quero!") == "nao quero"


@respx.mock
def test_opt_out_via_text_cancels_job_and_replies_once(session, settings, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(200, json=graph_success(message_id="wamid.REPLY"))
    )
    handle_event(session, kirvano_payload(phone="5511987654321"), settings=settings)
    client = GraphClient(settings)

    result = handle_meta_webhook(session, meta_text_payload("5511987654321", "SAIR"), client=client)
    assert result.messages_stored == 1 and result.opt_outs == 1 and result.replies_sent == 1
    assert route.call_count == 1
    body = route.calls.last.request.content.decode()
    assert OPT_OUT_REPLY in body
    assert set(session.execute(select(OptOut.phone)).scalars()) == {"5511987654321", "551187654321"}
    job = session.execute(select(RecoveryJob)).scalar_one()
    assert job.state == "cancelled" and job.reason == "opted_out"
    msgs = session.execute(select(Message).order_by(Message.id)).scalars().all()
    assert [m.direction for m in msgs] == ["in", "out"]
    assert msgs[0].body == "SAIR" and msgs[0].order_id is not None
    assert msgs[1].body == OPT_OUT_REPLY and msgs[1].wa_message_id == "wamid.REPLY"

    # A second SAIR: stored, no new opt-out row, no second reply.
    result = handle_meta_webhook(
        session, meta_text_payload("5511987654321", "sair", message_id="wamid.IN2"), client=client
    )
    assert result.opt_outs == 0 and result.replies_sent == 0
    assert route.call_count == 1
    # Duplicate delivery of the same message id is ignored.
    result = handle_meta_webhook(
        session, meta_text_payload("5511987654321", "sair", message_id="wamid.IN2"), client=client
    )
    assert result.messages_duplicate == 1


@respx.mock
def test_opt_out_via_button_uses_12_digit_wa_id(session, settings, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(return_value=Response(200, json=graph_success()))
    handle_event(session, kirvano_payload(phone="5511987654321"), settings=settings)
    # Meta may know the customer by the 12-digit form.
    result = handle_meta_webhook(
        session,
        meta_button_payload("551187654321", "Não quero receber"),
        client=GraphClient(settings),
    )
    assert result.opt_outs == 1
    row = session.execute(select(OptOut).where(OptOut.phone == "5511987654321")).scalar_one()
    assert row.source == "button" and row.wa_id == "551187654321"
    assert session.execute(select(RecoveryJob)).scalar_one().state == "cancelled"
    order = session.execute(select(Order)).scalar_one()
    assert order.wa_id == "551187654321"


def test_opt_out_blocks_future_scheduling_until_removed(session, settings, frozen_clock):
    handle_meta_webhook(session, meta_text_payload("5511987654321", "parar"), client=None)
    assert is_opted_out(session, ["5511987654321"])
    res = handle_event(
        session, kirvano_payload(sale_id="NEW1", phone="11987654321"), settings=settings
    )
    assert res.reason == "opted_out"
    assert session.execute(select(func.count()).select_from(RecoveryJob)).scalar_one() == 0

    assert remove_opt_out(session, "551187654321") == 2
    session.commit()
    assert not is_opted_out(session, ["5511987654321"], "5511987654321")
    res = handle_event(
        session, kirvano_payload(sale_id="NEW2", phone="11987654321"), settings=settings
    )
    assert res.reason == "created"


def test_non_opt_out_inbound_is_stored_and_linked(session, settings, frozen_clock):
    handle_event(session, kirvano_payload(), settings=settings)
    result = handle_meta_webhook(
        session, meta_text_payload("5511987654321", "já paguei"), client=None
    )
    assert result.messages_stored == 1 and result.opt_outs == 0
    msg = session.execute(select(Message)).scalar_one()
    assert msg.direction == "in" and msg.status == "received"
    assert msg.order_id == session.execute(select(Order)).scalar_one().id
    assert session.execute(select(func.count()).select_from(OptOut)).scalar_one() == 0
    assert session.execute(select(RecoveryJob)).scalar_one().state == "scheduled"
