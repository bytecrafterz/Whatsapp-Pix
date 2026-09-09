"""The Kirvano endpoint must answer 200 no matter what the body or the DB does.

SPEC "Kirvano webhooks": *always answer 200 quickly*, *store raw, log, 200* for
unknown events, *assume duplicates*. The endpoint is publicly reachable and, in the
shipped ``KIRVANO_TOKEN_MODE=log``, unauthenticated — so every one of these paths is
reachable from the internet.

SQLite ignores VARCHAR lengths, which is why the over-long-value cases below could
only ever fail on the production PostgreSQL: there they raise ``DataError``, which is
**not** an ``IntegrityError`` and used to escape the duplicate guard as a 500.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError

from app import kirvano
from app.kirvano import handle_event, parse_payload
from app.models import Order, WebhookEvent
from tests.conftest import kirvano_payload


def test_over_long_values_are_clipped_to_the_column_widths(session, settings, frozen_clock):
    payload = kirvano_payload(
        sale_id="S" * 300,
        name="N" * 900,
        phone="5511987654321" + "9" * 60,
        extra={"checkout_id": "C" * 300, "ip": "2001:db8::" + "f" * 200},
    )
    payload["customer"]["email"] = "e" * 400 + "@example.com"
    payload["products"][0]["name"] = "P" * 800
    payload["products"][0]["offer_id"] = "O" * 300

    result = handle_event(session, payload, settings=settings)
    assert result.outcome == "processed"

    order = session.execute(select(Order)).scalar_one()
    assert len(order.sale_id) <= 64
    assert len(order.checkout_id) <= 64
    assert len(order.offer_id) <= 64
    assert len(order.customer_name) <= 255
    assert len(order.customer_email) <= 255
    assert len(order.product_name) <= 255
    assert len(order.phone_raw) <= 32
    assert len(order.consent_ip) <= 45

    evt = session.execute(select(WebhookEvent)).scalar_one()
    assert len(evt.event) <= 64 and len(evt.sale_id) <= 64 and len(evt.external_key) <= 255


def test_unknown_event_name_is_clipped_and_still_answers_200(session, settings, frozen_clock):
    payload = kirvano_payload(event="X" * 500)
    assert parse_payload(payload).event == "X" * 64
    result = handle_event(session, payload, settings=settings)
    assert result.outcome == "unknown" and result.reason == "unknown_event"


def test_a_database_error_on_the_event_insert_does_not_raise(
    session, settings, frozen_clock, monkeypatch
):
    """A DataError is not an IntegrityError; it used to escape as a 500."""
    real_flush = session.flush
    calls = {"n": 0}

    def flaky_flush(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DataError("value too long", None, Exception("varchar(64)"))
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", flaky_flush)
    result = handle_event(session, kirvano_payload(), settings=settings)
    assert result.outcome == "error" and result.reason == "DataError"


def test_error_path_survives_a_concurrent_duplicate(session, settings, frozen_clock, monkeypatch):
    """The recovery insert must not turn a handled error into a 500."""

    def boom(*args, **kwargs):
        raise RuntimeError("scheduling exploded")

    monkeypatch.setattr(kirvano, "_handle_pix_generated", boom)

    real_commit = session.commit
    calls = {"n": 0}

    def flaky_commit():
        # The first commit after the rollback is the error-row insert: pretend a
        # concurrent delivery committed the same (event, sale_id, created_at) key
        # while this transaction was rolling back.
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("dup", None, Exception("duplicate key"))
        return real_commit()

    monkeypatch.setattr(session, "commit", flaky_commit)
    result = handle_event(session, kirvano_payload(), settings=settings)
    assert result.outcome == "error" and result.reason == "RuntimeError"


def test_two_first_time_deliveries_for_one_sale_do_not_lose_the_delivery(
    session, settings, frozen_clock, monkeypatch
):
    """`_lock_order` cannot lock a row that does not exist yet — both sides INSERT."""
    real_lock = kirvano._lock_order
    calls = {"n": 0}

    def racing_lock(sess, sale_id):
        order = real_lock(sess, sale_id)
        if calls["n"] == 0:
            calls["n"] += 1
            # The competing delivery commits the order between our lookup and our
            # INSERT: we saw None, but by the time we flush the row exists.
            sess.add(
                Order(
                    sale_id=sale_id,
                    page_token=kirvano.new_page_token(),
                    currency="BRL",
                    status="pending",
                    created_at=frozen_clock.now,
                    updated_at=frozen_clock.now,
                )
            )
            sess.flush()
            return None
        return order

    monkeypatch.setattr(kirvano, "_lock_order", racing_lock)
    result = handle_event(session, kirvano_payload(sale_id="RACE1"), settings=settings)

    # The loser of the race updates the winner's row instead of failing the delivery.
    assert result.outcome == "processed"
    order = session.execute(select(Order).where(Order.sale_id == "RACE1")).scalar_one()
    assert order.phone_e164 == "5511987654321"  # our payload was applied
    assert result.order_id == order.id


@pytest.mark.parametrize("event", ["SALE_APPROVED", "PIX_EXPIRED"])
def test_status_event_for_an_unknown_sale_also_survives_the_race(
    session, settings, frozen_clock, monkeypatch, event
):
    real_lock = kirvano._lock_order
    fired = {"once": False}

    def racing_lock(sess, sale_id):
        order = real_lock(sess, sale_id)
        if not fired["once"]:
            fired["once"] = True
            sess.add(
                Order(
                    sale_id=sale_id,
                    page_token=kirvano.new_page_token(),
                    currency="BRL",
                    status="pending",
                    created_at=frozen_clock.now,
                    updated_at=frozen_clock.now,
                )
            )
            sess.flush()
            return None
        return order

    monkeypatch.setattr(kirvano, "_lock_order", racing_lock)
    result = handle_event(session, kirvano_payload(event=event, sale_id="RACE2"), settings=settings)
    assert result.outcome == "processed"
    order = session.execute(select(Order).where(Order.sale_id == "RACE2")).scalar_one()
    assert order.status in ("paid", "expired")
