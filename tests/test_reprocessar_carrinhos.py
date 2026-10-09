"""scripts/reprocessar_carrinhos.py: replaying the carts lost to the shared-checkout bug.

The lost events are stored exactly as production stored them before the fix: the real
ABANDONED_CART shape (``"checkout_id": "null"``), redacted, ``outcome="ignored"``, no cart.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.kirvano import handle_event, redact_payload
from app.models import Cart, CartJob, WebhookEvent
from app.optout import add_opt_out
from app.whatsapp import GraphClient
from app.worker import Worker
from scripts import reprocessar_carrinhos
from tests.conftest import DEFAULT_NOW, GRAPH_MESSAGES_URL, graph_success, kirvano_payload
from tests.test_cart import REAL_OFFER_URL, real_cart_body

_keys = itertools.count(1)

GILDETE = ("Gildete Bonfim", "5574999746627", "g@example.com")
ANA = ("Ana Souza", "5521988887777", "ana@example.com")
JOAO = ("João Lima", "5531977776666", "joao@example.com")


@pytest.fixture
def cart_on(session, store):
    store.set_many({"cart_enabled": True, "cart_coupon": "DESCONTO5"})
    session.commit()
    return store


def lost(session, customer: tuple[str, str, str], abandoned_at: datetime) -> WebhookEvent:
    """An ABANDONED_CART the old code stored as ``ignored`` a few seconds after it happened."""
    evt = WebhookEvent(
        source="kirvano",
        external_key=f"ABANDONED_CART|null|lost-{next(_keys)}",
        event="ABANDONED_CART",
        payload=redact_payload(real_cart_body(*customer, abandoned_at)),
        headers_meta={"header_names": ["content-type"]},
        received_at=abandoned_at + timedelta(seconds=3),
        processed_at=abandoned_at + timedelta(seconds=3),
        outcome="ignored",
    )
    session.add(evt)
    session.commit()
    return evt


def run(session, settings, capsys, *args: str) -> str:
    assert reprocessar_carrinhos.main(list(args), settings=settings, session=session) == 0
    return capsys.readouterr().out


def _carts(session) -> list[Cart]:
    return list(session.execute(select(Cart).order_by(Cart.id)).scalars())


def _jobs(session) -> list[CartJob]:
    return list(session.execute(select(CartJob).order_by(CartJob.id)).scalars())


def test_preview_lists_who_would_get_it_and_saves_nothing(
    session, settings, cart_on, frozen_clock, capsys
):
    first = lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=5))
    lost(session, ANA, DEFAULT_NOW - timedelta(hours=3))

    out = run(session, settings, capsys)

    assert out.count("-> RECEBE") == 2
    assert "2 carrinho(s) perdido(s) nas últimas 24 h: 2 recebem a mensagem, 0 pulado(s)." in out
    assert "0 contato(s) nas últimas 24 h + 2 novo(s) = 2 de 250" in out
    assert "PRÉVIA: nada foi salvo" in out
    # Recognisable, not exposed: first name and the last 4 digits only.
    assert "Gildete" in out and "…6627" in out
    assert "5574999746627" not in out and "Bonfim" not in out and "g@example.com" not in out
    assert _carts(session) == []
    session.refresh(first)
    assert first.outcome == "ignored" and "reprocessado_em" not in first.headers_meta


def test_enviar_creates_the_carts_and_the_first_message_goes_now(
    session, settings, cart_on, frozen_clock, capsys
):
    evt = lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=5))
    lost(session, ANA, DEFAULT_NOW - timedelta(hours=3))

    out = run(session, settings, capsys, "--enviar")

    assert "Gravado." in out
    carts = _carts(session)
    assert [c.phone_e164 for c in carts] == ["5574999746627", "5521988887777"]
    assert carts[0].abandoned_at == DEFAULT_NOW - timedelta(hours=5)  # the real abandonment
    assert carts[0].checkout_url == REAL_OFFER_URL and carts[0].checkout_id is None
    jobs = _jobs(session)
    assert [(j.step, j.state) for j in jobs] == [(1, "scheduled"), (1, "scheduled")]
    assert all(j.run_at == DEFAULT_NOW for j in jobs)  # 10 min after abandoning is long past
    session.refresh(evt)
    assert evt.outcome == "processed"
    assert evt.headers_meta == {
        "header_names": ["content-type"],
        "reprocessado_em": DEFAULT_NOW.isoformat(),
    }

    # A second run finds nothing left to do.
    again = run(session, settings, capsys, "--enviar")
    assert "0 carrinho(s) perdido(s)" in again
    assert len(_carts(session)) == 2 and len(_jobs(session)) == 2


@respx.mock
def test_the_worker_sends_a_replayed_cart_with_its_coupon_link(
    client, session, settings, cart_on, frozen_clock, capsys
):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(200, json=graph_success(message_id="wamid.REPLAY1"))
    )
    lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=5))
    run(session, settings, capsys, "--enviar")

    worker = Worker(
        settings, session_factory=lambda: session, client=GraphClient(settings), poll_seconds=0.01
    )
    assert worker.run_once() == 1

    body = json.loads(route.calls[0].request.content)
    assert body["to"] == GILDETE[1]
    [cart] = _carts(session)
    [job] = _jobs(session)
    assert job.state == "sent"
    r = client.get(f"/c/{cart.link_token}", follow_redirects=False)
    assert r.headers["location"].startswith(f"{REAL_OFFER_URL}?")
    assert r.headers["location"].endswith("&coupon=DESCONTO5")


def test_customers_who_must_not_get_it_are_skipped(
    session, settings, cart_on, frozen_clock, capsys
):
    abandoned = DEFAULT_NOW - timedelta(hours=6)
    # 1. Paid by card after abandoning (same phone).
    lost(session, GILDETE, abandoned)
    handle_event(
        session,
        kirvano_payload(
            "SALE_APPROVED",
            sale_id="CARD0001",
            phone=GILDETE[1],
            method="CREDIT_CARD",
            finished_at=abandoned + timedelta(minutes=20),
        ),
        settings=settings,
    )
    # 2. Paid with the same e-mail but another phone.
    lost(session, ("Paula Reis", "5541955554444", "fulano@example.com"), abandoned)
    handle_event(
        session,
        kirvano_payload(
            "SALE_APPROVED",
            sale_id="CARD0002",
            phone="5541900001111",
            method="CREDIT_CARD",
            finished_at=abandoned + timedelta(minutes=30),
        ),
        settings=settings,
    )
    # 3. Abandoned again after the fix: the live webhook already gave her a cart.
    lost(session, ANA, abandoned)
    handle_event(
        session, real_cart_body(*ANA, DEFAULT_NOW - timedelta(minutes=30)), settings=settings
    )
    # 4. Said SAIR.
    lost(session, JOAO, abandoned)
    add_opt_out(session, phone=JOAO[1], wa_id=None, source="inbound", now=abandoned)
    session.commit()
    # 5. Outside the window.
    lost(
        session,
        ("Rita Melo", "5551933332222", "rita@example.com"),
        DEFAULT_NOW - timedelta(hours=30),
    )

    out = run(session, settings, capsys, "--enviar")

    assert "pula: cliente comprou depois do abandono" in out
    assert "pula: comprou depois (mesmo e-mail, outro telefone)" in out
    assert "pula: já tem um carrinho mais recente" in out
    assert "pula: cliente pediu para não receber" in out
    assert "Rita" not in out
    assert "-> RECEBE" not in out
    assert "4 carrinho(s) perdido(s) nas últimas 24 h: 0 recebem a mensagem, 4 pulado(s)." in out
    # Only Ana's live cart has a message; her newer data was not overwritten.
    scheduled = [j for j in _jobs(session) if j.state == "scheduled"]
    assert len(scheduled) == 1
    ana = session.get(Cart, scheduled[0].cart_id)
    assert ana.phone_e164 == ANA[1] and ana.abandoned_at == DEFAULT_NOW - timedelta(minutes=30)


def test_one_customer_abandoning_twice_gets_one_message(
    session, settings, cart_on, frozen_clock, capsys
):
    lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=8))
    lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=2))

    out = run(session, settings, capsys, "--enviar")

    assert out.count("-> RECEBE") == 1
    assert "pula: sequência já iniciada" in out
    assert len(_carts(session)) == 1 and len(_jobs(session)) == 1


def test_over_the_daily_limit_is_announced(session, settings, cart_on, frozen_clock, capsys):
    cart_on.set_many({"daily_recipient_limit": 1})
    session.commit()
    lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=5))
    lost(session, ANA, DEFAULT_NOW - timedelta(hours=3))

    out = run(session, settings, capsys)

    assert "= 2 de 1." in out
    assert "ATENÇÃO: 1 ficariam sem mensagem (limite diário)" in out


def test_refuses_while_cart_recovery_is_off(session, settings, store, frozen_clock, capsys):
    lost(session, GILDETE, DEFAULT_NOW - timedelta(hours=5))
    assert reprocessar_carrinhos.main([], settings=settings, session=session) == 1
    assert "DESLIGADA" in capsys.readouterr().out
    assert _carts(session) == []
