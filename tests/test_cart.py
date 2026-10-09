"""Abandoned-cart recovery: Kirvano ABANDONED_CART → a short Marketing sequence.

The promises under test are the ones sold to the client: messages go out at the
configured time with the customer's name, product and coupon; a PIX or a purchase stops
the sequence at once; Kirvano repeating the event never sends anything twice; the two
flows (PIX reminder and cart) never stack on one customer; and the panel's numbers
(sent, delivered, read, clicks, recovered sales and value) add up.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx
from httpx import Response
from sqlalchemy import func, select

from app.cart import MAX_CART_AGE
from app.inbound import handle_meta_webhook
from app.kirvano import handle_event
from app.models import Cart, CartJob, Message, Order, RecoveryJob, WebhookEvent
from app.optout import add_opt_out
from app.queries import cart_metrics
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
    sp_str,
)

AUTH = ("admin", "panel-pw")
ORIGIN = {"Origin": "http://testserver"}
PHONE = "5511987654321"
CHECKOUT_LINK = "https://pay.kirvano.com/checkout/abc-123"
# What the button adds to every checkout link by default (coupon from the cart_on fixture).
TRACKING = (
    "utm_source=whatsapp&utm_medium=recuperacao&utm_campaign=carrinho_abandonado&coupon=VOLTA10"
)


def cart_payload(
    *,
    checkout_id: str = "CK7Q2W9E",
    phone: str | None = PHONE,
    name: str = "Maria Souza",
    email: str = "maria@example.com",
    total: str = "R$ 97,00",
    created_at: datetime | None = None,
    checkout_url: str | None = CHECKOUT_LINK,
) -> dict[str, Any]:
    """Our best model of the (not yet captured) ABANDONED_CART body: the keys the
    other Kirvano events use, which is what the tolerant parser relies on."""
    customer: dict[str, Any] = {"name": name, "document": "12345678900", "email": email}
    if phone:
        customer["phone_number"] = phone
    body: dict[str, Any] = {
        "event": "ABANDONED_CART",
        "event_description": "Carrinho abandonado",
        "checkout_id": checkout_id,
        "total_price": total,
        "created_at": sp_str(created_at or DEFAULT_NOW),
        "customer": customer,
        "products": [
            {
                "id": "prod-uuid",
                "offer_id": "offer-uuid",
                "name": "Jornada com Meu Anjo",
                "price": total,
                "is_order_bump": False,
            }
        ],
        "ip": "200.152.1.115",
        "cookies": {"fbp": "fb.1.123"},
    }
    if checkout_url:
        body["checkout_url"] = checkout_url
    return body


@pytest.fixture
def cart_on(session, store):
    """Cart recovery switched on with a coupon (it ships switched off)."""
    store.set_many({"cart_enabled": True, "cart_coupon": "VOLTA10"})
    session.commit()
    return store


def _carts(session) -> list[Cart]:
    return list(session.execute(select(Cart).order_by(Cart.id)).scalars())


def _jobs(session) -> list[CartJob]:
    return list(session.execute(select(CartJob).order_by(CartJob.cart_id, CartJob.step)).scalars())


def _worker(settings, session) -> Worker:
    return Worker(
        settings, session_factory=lambda: session, client=GraphClient(settings), poll_seconds=0.01
    )


def _unique_success():
    counter = itertools.count(1)
    return lambda request: Response(
        200, json=graph_success(message_id=f"wamid.CART{next(counter)}")
    )


# --- starting the sequence ------------------------------------------------------------


def test_abandoned_cart_schedules_the_first_message_after_the_delay(
    session, settings, cart_on, frozen_clock
):
    res = handle_event(session, cart_payload(), settings=settings)
    assert res.outcome == "processed" and res.reason == "scheduled_1"

    [cart] = _carts(session)
    assert cart.status == "open" and cart.reason is None
    assert cart.phone_e164 == PHONE
    assert cart.product_name == "Jornada com Meu Anjo"
    assert cart.amount_cents == 9700
    assert cart.checkout_url == CHECKOUT_LINK
    assert cart.consent_ip == "200.152.1.115"
    [job] = _jobs(session)
    assert job.step == 1 and job.state == "scheduled"
    assert job.run_at == DEFAULT_NOW + timedelta(minutes=60)
    # LGPD: the CPF and the ad cookies never reach the disk, for carts as for orders.
    stored = session.execute(select(WebhookEvent)).scalar_one().payload
    assert "document" not in stored["customer"] and "cookies" not in stored


def test_cart_recovery_ships_switched_off(session, settings, frozen_clock):
    """No message before the client approves a template and turns it on."""
    res = handle_event(session, cart_payload(), settings=settings)
    assert res.outcome == "processed" and res.reason == "disabled"
    [cart] = _carts(session)
    assert cart.reason == "disabled"
    assert _jobs(session) == []


def test_kirvano_repeating_the_event_never_starts_a_second_sequence(
    session, settings, cart_on, frozen_clock
):
    handle_event(session, cart_payload(), settings=settings)
    # exact redelivery
    assert handle_event(session, cart_payload(), settings=settings).outcome == "duplicate"
    # same checkout, new timestamp
    frozen_clock.advance(minutes=3)
    later = cart_payload(created_at=frozen_clock.now)
    assert handle_event(session, later, settings=settings).reason == "sequence_exists"
    # same phone abandoning another checkout the same day: still the same sequence
    other = cart_payload(checkout_id="ZX81KL0P", created_at=frozen_clock.now)
    assert handle_event(session, other, settings=settings).reason == "sequence_exists"
    assert len(_carts(session)) == 1
    assert len(_jobs(session)) == 1


def test_cart_without_phone_is_stored_but_gets_no_message(session, settings, cart_on, frozen_clock):
    res = handle_event(session, cart_payload(phone=None), settings=settings)
    assert res.reason == "no_phone"
    assert _carts(session)[0].reason == "no_phone"
    assert _jobs(session) == []


def test_opted_out_customer_gets_no_sequence(session, settings, cart_on, frozen_clock):
    add_opt_out(session, phone=PHONE, wa_id=None, source="text")
    session.commit()
    res = handle_event(session, cart_payload(), settings=settings)
    assert res.reason == "opted_out"
    assert _jobs(session) == []


def test_quiet_hours_push_the_message_to_the_morning(session, settings, cart_on, frozen_clock):
    # 21:30 in São Paulo: +60 min lands at 22:30, inside 22:00–08:00.
    evening = datetime(2026, 9, 9, 0, 30, tzinfo=UTC)
    frozen_clock.set(evening)
    handle_event(session, cart_payload(created_at=evening), settings=settings)
    [job] = _jobs(session)
    assert job.reason == "quiet_hours"
    assert job.run_at == datetime(2026, 9, 9, 11, 0, tzinfo=UTC)  # 08:00 in São Paulo


def test_customer_with_a_pix_in_progress_gets_no_cart_message(
    session, settings, cart_on, frozen_clock
):
    """The PIX reminder owns them: two nudges for one purchase is how numbers get banned."""
    handle_event(session, kirvano_payload(phone=PHONE), settings=settings)
    frozen_clock.advance(minutes=5)
    res = handle_event(session, cart_payload(created_at=frozen_clock.now), settings=settings)
    assert res.reason == "pix_flow_active"
    assert _jobs(session) == []


# Kirvano sends ONE checkout_id for many customers (the checkout page's code). In
# production every cart after the first was matched to that first cart, which had
# already bought, and all of them were dropped as "ignored".
SHARED_CHECKOUT = "SHARED01"


def test_customers_sharing_a_checkout_code_get_their_own_carts(
    session, settings, cart_on, frozen_clock
):
    first = cart_payload(checkout_id=SHARED_CHECKOUT, phone="5511911110001", email="a@example.com")
    handle_event(session, first, settings=settings)
    sale = kirvano_payload(
        "SALE_APPROVED", sale_id="CARDA001", phone="5511911110001", method="CREDIT_CARD"
    )
    sale["checkout_id"] = SHARED_CHECKOUT
    sale["customer"]["email"] = "a@example.com"
    handle_event(session, sale, settings=settings)  # the first customer buys

    frozen_clock.advance(minutes=30)
    results = [
        handle_event(
            session,
            cart_payload(
                checkout_id=SHARED_CHECKOUT,
                phone=phone,
                email=email,
                created_at=frozen_clock.now,  # same second: must not look like a repeat
            ),
            settings=settings,
        )
        for phone, email in [("5511922220002", "b@example.com"), ("5511933330003", "c@example.com")]
    ]
    assert [(r.outcome, r.reason) for r in results] == [
        ("processed", "scheduled_1"),
        ("processed", "scheduled_1"),
    ]
    carts = _carts(session)
    assert [c.status for c in carts] == ["purchased", "open", "open"]
    assert len({c.id for c in carts}) == 3


def test_a_sale_never_closes_another_customers_cart_with_the_same_checkout_code(
    session, settings, cart_on, frozen_clock
):
    handle_event(
        session,
        cart_payload(checkout_id=SHARED_CHECKOUT, phone="5511922220002", email="b@example.com"),
        settings=settings,
    )
    sale = kirvano_payload(
        "SALE_APPROVED", sale_id="CARDA001", phone="5511911110001", method="CREDIT_CARD"
    )
    sale["checkout_id"] = SHARED_CHECKOUT
    sale["customer"]["email"] = "a@example.com"
    handle_event(session, sale, settings=settings)
    [cart] = _carts(session)
    assert cart.status == "open" and cart.recovered is False
    assert _jobs(session)[0].state == "scheduled"


def test_a_card_purchase_reported_before_the_cart_event_blocks_the_message(
    session, settings, cart_on, frozen_clock
):
    """Kirvano can report the abandonment after the customer already paid by card."""
    sale = kirvano_payload("SALE_APPROVED", sale_id="CARDB001", phone=PHONE, method="CREDIT_CARD")
    sale["customer"]["email"] = "other@example.com"
    handle_event(session, sale, settings=settings)
    late = cart_payload(created_at=DEFAULT_NOW - timedelta(minutes=5))
    res = handle_event(session, late, settings=settings)
    assert res.reason == "purchased_after"
    assert _jobs(session) == []


# --- stopping it -------------------------------------------------------------------------


def test_generating_a_pix_cancels_the_sequence(session, settings, cart_on, frozen_clock):
    handle_event(session, cart_payload(), settings=settings)
    frozen_clock.advance(minutes=20)
    handle_event(
        session,
        kirvano_payload(sale_id="SALE0001", phone=PHONE, created_at=frozen_clock.now),
        settings=settings,
    )
    [cart] = _carts(session)
    assert cart.status == "pix_generated" and cart.converted_sale_id == "SALE0001"
    [job] = _jobs(session)
    assert job.state == "cancelled" and job.reason == "pix_generated"
    # ... and the PIX reminder itself is scheduled as usual.
    assert session.execute(select(RecoveryJob)).scalar_one().state == "scheduled"


def test_purchase_before_any_message_is_not_counted_as_recovered(
    session, settings, cart_on, frozen_clock
):
    handle_event(session, cart_payload(), settings=settings)
    frozen_clock.advance(minutes=30)
    # A credit-card sale we never saw as a PIX, matched by e-mail only.
    sale = kirvano_payload(
        "SALE_APPROVED",
        sale_id="CARD0001",
        phone="5521999990000",
        method="CREDIT_CARD",
        created_at=frozen_clock.now,
        finished_at=frozen_clock.now,
    )
    sale["customer"]["email"] = "MARIA@example.com"
    res = handle_event(session, sale, settings=settings)
    assert res.outcome == "ignored"  # nothing for the PIX flow...
    [cart] = _carts(session)
    assert cart.status == "purchased"  # ...but the cart is closed
    assert cart.recovered is False
    assert _jobs(session)[0].state == "cancelled"
    metrics = cart_metrics(session)
    assert metrics.recovered == 0 and metrics.purchased_without_message == 1


def test_unrelated_sale_leaves_the_cart_alone(session, settings, cart_on, frozen_clock):
    handle_event(session, cart_payload(), settings=settings)
    sale = kirvano_payload("SALE_APPROVED", sale_id="OTHER001", phone="5521999990000")
    sale["checkout_id"] = "OTHERCK1"
    sale["customer"]["email"] = "someone.else@example.com"
    handle_event(session, sale, settings=settings)
    assert _carts(session)[0].status == "open"
    assert _jobs(session)[0].state == "scheduled"


def test_saying_sair_cancels_the_cart_messages(session, settings, cart_on, frozen_clock):
    handle_event(session, cart_payload(), settings=settings)
    handle_meta_webhook(session, meta_text_payload(PHONE, "SAIR"))
    [job] = _jobs(session)
    assert job.state == "cancelled" and job.reason == "opted_out"


# --- sending -----------------------------------------------------------------------------


@respx.mock
def test_worker_sends_the_message_and_a_later_sale_counts_as_recovered(
    session, settings, cart_on, frozen_clock
):
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, cart_payload(), settings=settings)
    frozen_clock.advance(minutes=60)
    assert _worker(settings, session).run_once() == 1

    body = json.loads(route.calls[0].request.content)
    template = body["template"]
    assert body["to"] == PHONE
    assert template["name"] == "carrinho_abandonado_v1"
    assert template["language"] == {"code": "pt_BR"}
    params = [p["text"] for p in template["components"][0]["parameters"]]
    assert params == ["Maria", "Jornada com Meu Anjo", "VOLTA10"]
    [cart] = _carts(session)
    button = template["components"][1]
    assert button["sub_type"] == "url" and button["index"] == "0"
    assert button["parameters"] == [{"type": "text", "text": cart.link_token}]

    [job] = _jobs(session)
    assert job.state == "sent" and job.wa_message_id == "wamid.CART1"
    msg = session.execute(select(Message)).scalar_one()
    assert msg.template_name == "carrinho_abandonado_v1" and msg.order_id is None

    # Meta reports delivery and reading on the same message row.
    handle_meta_webhook(session, meta_status_payload("wamid.CART1", "delivered"))
    handle_meta_webhook(session, meta_status_payload("wamid.CART1", "read"))

    frozen_clock.advance(hours=2)
    paid = kirvano_payload(
        "SALE_APPROVED",
        sale_id="CARD0002",
        phone=PHONE,
        method="CREDIT_CARD",
        total="R$ 87,30",
        created_at=frozen_clock.now,
        finished_at=frozen_clock.now,
    )
    handle_event(session, paid, settings=settings)
    session.refresh(cart)
    assert cart.status == "purchased" and cart.recovered is True
    assert cart.converted_amount_cents == 8730

    m = cart_metrics(session)
    assert (m.abandoned, m.sent, m.delivered, m.read) == (1, 1, 1, 1)
    assert (m.recovered, m.recovered_cents, m.sent_carts) == (1, 8730, 1)
    assert m.recovery_rate == 100.0


@respx.mock
def test_a_pix_under_another_checkout_is_caught_at_send_time(
    session, settings, cart_on, frozen_clock
):
    """Belt and braces: even when the PIX event did not match the cart, the worker's
    re-check sees the new order for that phone and sends nothing."""
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, cart_payload(), settings=settings)
    session.add(
        Order(
            sale_id="UNMATCHED",
            phone_e164=PHONE,
            phone_alt="551187654321",
            status="pending",
            page_token="tok-unmatched",
            created_at=DEFAULT_NOW + timedelta(minutes=10),
            updated_at=DEFAULT_NOW + timedelta(minutes=10),
        )
    )
    session.commit()
    frozen_clock.advance(minutes=60)
    _worker(settings, session).run_once()
    assert route.call_count == 0
    [job] = _jobs(session)
    assert job.state == "skipped" and job.reason == "pix_generated"


@respx.mock
def test_second_message_waits_for_the_first_and_is_skipped_if_it_failed(
    session, settings, cart_on, frozen_clock
):
    cart_on.set_many(
        {
            "cart_steps": 2,
            "cart_step2_template": "carrinho_lembrete_v1",
            "cart_step2_delay_minutes": 180,
        }
    )
    session.commit()
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(131026, "not on WhatsApp"))
    )
    handle_event(session, cart_payload(), settings=settings)
    assert [j.step for j in _jobs(session)] == [1, 2]

    frozen_clock.advance(minutes=60)
    _worker(settings, session).run_once()
    assert route.call_count == 2  # both number forms, once each
    frozen_clock.advance(minutes=120)
    _worker(settings, session).run_once()
    assert route.call_count == 2  # step 2 never posted

    first, second = _jobs(session)
    assert first.state == "failed" and first.reason == "not_on_whatsapp"
    assert second.state == "skipped" and second.reason == "previous_not_sent"


@respx.mock
def test_second_message_goes_out_at_its_own_time(session, settings, cart_on, frozen_clock):
    cart_on.set_many(
        {
            "cart_steps": 2,
            "cart_step2_template": "carrinho_lembrete_v1",
            "cart_step2_params": "first_name",
            "cart_step2_delay_minutes": 180,
        }
    )
    session.commit()
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, cart_payload(), settings=settings)
    frozen_clock.advance(minutes=60)
    _worker(settings, session).run_once()
    frozen_clock.advance(minutes=60)  # 120 min: step 2 is not due yet
    _worker(settings, session).run_once()
    assert route.call_count == 1
    frozen_clock.advance(minutes=60)  # 180 min
    _worker(settings, session).run_once()
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)["template"]
    assert second["name"] == "carrinho_lembrete_v1"
    assert [j.state for j in _jobs(session)] == ["sent", "sent"]


@respx.mock
def test_marketing_limit_131049_is_not_retried(session, settings, cart_on, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(131049, "healthy ecosystem"))
    )
    handle_event(session, cart_payload(), settings=settings)
    frozen_clock.advance(minutes=60)
    _worker(settings, session).run_once()
    frozen_clock.advance(days=1)
    _worker(settings, session).run_once()
    assert route.call_count == 1
    [job] = _jobs(session)
    assert job.state == "failed" and job.reason == "marketing_limit_24h"


@respx.mock
def test_daily_contact_limit_is_shared_with_the_pix_reminders(
    session, settings, cart_on, frozen_clock
):
    cart_on.set("daily_recipient_limit", 1)
    session.commit()
    respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(
        session, kirvano_payload(sale_id="PIXA0001", phone="5521911112222"), settings=settings
    )
    frozen_clock.advance(minutes=10)
    _worker(settings, session).run_once()  # the PIX reminder uses the only slot
    handle_event(session, cart_payload(created_at=frozen_clock.now), settings=settings)
    frozen_clock.advance(minutes=60)
    _worker(settings, session).run_once()
    [job] = _jobs(session)
    assert job.state == "skipped" and job.reason == "daily_limit"


@respx.mock
def test_switching_off_holds_the_messages_until_they_are_too_old(
    session, settings, cart_on, frozen_clock
):
    route = respx.post(GRAPH_MESSAGES_URL).mock(side_effect=_unique_success())
    handle_event(session, cart_payload(), settings=settings)
    cart_on.set("cart_enabled", False)
    session.commit()
    frozen_clock.advance(minutes=60)
    _worker(settings, session).run_once()
    [job] = _jobs(session)
    assert job.state == "scheduled" and job.reason == "disabled"
    frozen_clock.set(DEFAULT_NOW + MAX_CART_AGE + timedelta(minutes=1))
    _worker(settings, session).run_once()
    session.refresh(job)
    assert job.state == "skipped" and job.reason == "cart_too_old"
    assert route.call_count == 0


# --- the button ---------------------------------------------------------------------------


def test_button_redirects_to_the_checkout_and_counts_the_click(
    client, session, settings, cart_on, frozen_clock
):
    handle_event(session, cart_payload(), settings=settings)
    [cart] = _carts(session)
    r = client.get(f"/c/{cart.link_token}", follow_redirects=False)
    # Coupon already applied (the public is elderly: nothing to type) and the sale
    # tagged as coming from the WhatsApp recovery.
    assert r.status_code == 302 and r.headers["location"] == f"{CHECKOUT_LINK}?{TRACKING}"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    # Template approved with "{{1}}" typed into the URL field: Meta sends it literally.
    r = client.get(f"/c/%7B%7B1%7D%7D{cart.link_token}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{CHECKOUT_LINK}?{TRACKING}"
    session.refresh(cart)
    assert cart.clicks == 2 and cart.first_click_at is not None
    assert cart_metrics(session).clicked == 1


def test_button_falls_back_to_the_panel_link_and_rejects_unsafe_urls(
    client, session, settings, cart_on, frozen_clock
):
    cart_on.set("cart_checkout_url", "https://pay.kirvano.com/fallback")
    session.commit()
    handle_event(session, cart_payload(checkout_url="javascript:alert(1)"), settings=settings)
    [cart] = _carts(session)
    assert cart.checkout_url is None
    r = client.get(f"/c/{cart.link_token}", follow_redirects=False)
    assert r.headers["location"] == f"https://pay.kirvano.com/fallback?{TRACKING}"
    assert client.get("/c/does-not-exist", follow_redirects=False).status_code == 404


def test_button_link_replaces_the_ad_utms_and_keeps_other_parameters(
    client, session, settings, cart_on, frozen_clock
):
    link = "https://pay.kirvano.com/checkout/abc?src=ad1&utm_source=FB&utm_content=Video+233"
    handle_event(session, cart_payload(checkout_url=link), settings=settings)
    [cart] = _carts(session)
    r = client.get(f"/c/{cart.link_token}", follow_redirects=False)
    assert r.headers["location"] == f"https://pay.kirvano.com/checkout/abc?src=ad1&{TRACKING}"

    # No UTM configured and no coupon: the link goes out exactly as Kirvano sent it.
    cart_on.set_many({"cart_link_utm": "", "cart_coupon": ""})
    session.commit()
    r = client.get(f"/c/{cart.link_token}", follow_redirects=False)
    assert r.headers["location"] == link


def test_each_product_goes_to_its_own_checkout(client, session, settings, cart_on, frozen_clock):
    """Two products, one coupon: the button follows the product of the abandoned cart."""
    cart_on.set_many(
        {
            "cart_checkout_url": "https://pay.kirvano.com/reserva",
            "cart_product_links": (
                "jornada com meu anjo | https://pay.kirvano.com/produto-1\n"
                "Oração Diária | https://pay.kirvano.com/produto-2\n"
                "https://pay.kirvano.com/offer-3"
            ),
        }
    )
    session.commit()
    for i, (product, offer) in enumerate(
        [("Jornada com Meu Anjo", "offer-1"), ("Oracao  diaria", "offer-2"),
         ("Terceiro", "offer-3"), ("Não listado", "offer-9")]
    ):  # fmt: skip
        body = cart_payload(checkout_id=f"CK{i}", phone=f"551199999000{i}", checkout_url=None)
        body["products"][0].update(name=product, offer_id=offer)
        handle_event(session, body, settings=settings)
    links = [
        client.get(f"/c/{cart.link_token}", follow_redirects=False).headers["location"]
        for cart in _carts(session)
    ]
    assert links == [
        f"https://pay.kirvano.com/produto-1?{TRACKING}",  # by name, any case
        f"https://pay.kirvano.com/produto-2?{TRACKING}",
        f"https://pay.kirvano.com/offer-3?{TRACKING}",  # by the offer id inside the link
        f"https://pay.kirvano.com/reserva?{TRACKING}",  # not listed: the fallback
    ]


# --- panel --------------------------------------------------------------------------------


def _nonce(client) -> str:
    import re

    page = client.get("/painel/carrinho", auth=AUTH)
    assert page.status_code == 200, page.text[:300]
    return re.search(r'name="nonce" value="([0-9a-f]{64})"', page.text).group(1)


def _form(nonce: str, **overrides: str) -> dict[str, str]:
    data = {
        "nonce": nonce,
        "cart_enabled": "true",
        "cart_steps": "1",
        "cart_coupon": "VOLTA10",
        "cart_template_language": "pt_BR",
        "cart_checkout_url": "https://pay.kirvano.com/fallback",
        "cart_link_utm": "utm_source=whatsapp&utm_medium=recuperacao",
    }
    for i, (delay, name, params) in enumerate(
        [("60", "carrinho_abandonado_v1", "first_name,product,coupon"), ("1440", "", ""),
         ("2880", "", "")],
        start=1,
    ):  # fmt: skip
        data[f"cart_step{i}_delay_minutes"] = delay
        data[f"cart_step{i}_template"] = name
        data[f"cart_step{i}_url_button_index"] = "0"
        data[f"cart_step{i}_params"] = params
    data.update(overrides)
    return data


def test_carrinho_page_shows_numbers_and_recent_carts(client, session, settings, cart_on):
    handle_event(session, cart_payload(), settings=settings)
    page = client.get("/painel/carrinho", auth=AUTH)
    assert page.status_code == 200
    assert "carrinhos abandonados" in page.text and "vendas recuperadas" in page.text
    assert "Jornada com Meu Anjo" in page.text and PHONE in page.text
    assert "/c/{{1}}" in page.text  # the button URL the template must use
    # Configurações keeps only the PIX settings.
    assert "cart_coupon" not in client.get("/painel/configuracoes", auth=AUTH).text


def test_carrinho_settings_are_saved(client, session, store):
    r = client.post(
        "/painel/carrinho",
        auth=AUTH,
        data=_form(_nonce(client), cart_coupon="VOLTA15"),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    store.refresh()
    assert store.cart_enabled is True and store.cart_coupon == "VOLTA15"
    assert store.cart_link_utm == [("utm_source", "whatsapp"), ("utm_medium", "recuperacao")]
    assert store.cart_step(1).params == ("first_name", "product", "coupon")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cart_coupon": ""}, "informe o cupom"),
        ({"cart_step1_template": ""}, "Informe o nome do modelo"),
        ({"cart_step1_params": "first_name,sale_id"}, "Parâmetro desconhecido"),
        (
            {
                "cart_steps": "2",
                "cart_step2_template": "lembrete_v1",
                "cart_step2_delay_minutes": "90",
            },
            "pelo menos 60 minutos",
        ),
        ({"cart_checkout_url": ""}, "Informe o link do checkout"),
        ({"cart_coupon": "VOLTA 10!"}, "Use só letras"),
        ({"cart_link_utm": "utm_source"}, "Rastreamento inválido"),
        ({"cart_link_utm": "utm_source=whatsapp&coupon=X"}, "Não coloque coupon"),
        ({"cart_product_links": "Produto sem link"}, "Linha inválida"),
        ({"cart_product_links": "Produto | javascript:alert(1)"}, "Linha inválida"),
    ],
)
def test_carrinho_settings_are_validated(client, session, store, overrides, message):
    r = client.post(
        "/painel/carrinho", auth=AUTH, data=_form(_nonce(client), **overrides), headers=ORIGIN
    )
    assert r.status_code == 400
    assert message in r.text
    store.refresh()
    assert store.cart_enabled is False  # nothing saved


def test_product_links_can_replace_the_fallback_link(client, session, store):
    # A browser sends textarea lines with CRLF; blank lines are dropped.
    links = "Produto A | https://pay.kirvano.com/a\r\n\r\nProduto B|https://pay.kirvano.com/b"
    r = client.post(
        "/painel/carrinho",
        auth=AUTH,
        data=_form(_nonce(client), cart_checkout_url="", cart_product_links=links),
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    store.refresh()
    assert store.get("cart_product_links") == (
        "Produto A | https://pay.kirvano.com/a\nProduto B | https://pay.kirvano.com/b"
    )
    assert "<textarea" in client.get("/painel/carrinho", auth=AUTH).text


def test_carrinho_form_needs_the_nonce(client, session, store):
    r = client.post("/painel/carrinho", auth=AUTH, data=_form("0" * 64), headers=ORIGIN)
    assert r.status_code == 403


# --- retention ----------------------------------------------------------------------------


def test_purge_anonymises_old_carts_and_keeps_the_numbers(session, settings, cart_on, frozen_clock):
    old = DEFAULT_NOW - timedelta(days=400)
    frozen_clock.set(old)
    handle_event(session, cart_payload(created_at=old), settings=settings)
    frozen_clock.set(DEFAULT_NOW)
    result = purge(session, now=DEFAULT_NOW)
    session.commit()
    assert result.carts_anonymised == 1
    [cart] = _carts(session)
    assert cart.customer_name is None and cart.phone_e164 is None and cart.checkout_url is None
    assert cart.amount_cents == 9700  # the counters survive
    assert session.execute(select(func.count()).select_from(Cart)).scalar_one() == 1
