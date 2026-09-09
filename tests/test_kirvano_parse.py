from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.kirvano import brl_to_cents, parse_amount_brl, parse_naive_local, parse_payload

SPEC_EXAMPLE = {
    "event": "PIX_GENERATED",
    "event_description": "PIX gerado",
    "checkout_id": "Q8J1N6K3",
    "sale_id": "D2RP8RQ7",
    "payment_method": "PIX",
    "total_price": "R$ 169,80",
    "type": "ONE_TIME",
    "status": "PENDING",
    "created_at": "2023-12-18 16:38:17",
    "customer": {
        "name": "Fulano de Tal",
        "document": "12345678900",
        "email": "fulano@example.com",
        "phone_number": "5511987654321",
    },
    "payment": {
        "method": "PIX",
        "qrcode": "00020126...",
        "qrcode_image": "https://x/qr.png",
        "expires_at": "2023-12-18 17:38:17",
    },
    "products": [
        {
            "id": "b1",
            "offer_id": "o-bump",
            "name": "Bump",
            "price": "R$ 9,90",
            "is_order_bump": True,
        },
        {
            "id": "p1",
            "offer_id": "o-main",
            "name": "Produto",
            "price": "R$ 169,80",
            "is_order_bump": False,
        },
    ],
    "utm": {"src": None, "utm_source": None},
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R$ 169,80", Decimal("169.80")),
        ("R$ 1.169,80", Decimal("1169.80")),
        ("R$1.234.567,05", Decimal("1234567.05")),
        ("169.80", Decimal("169.80")),
        ("1.169", Decimal("1169.00")),
        ("97", Decimal("97.00")),
        ("R$ 0,50", Decimal("0.50")),
        (169.8, Decimal("169.80")),
        (97, Decimal("97.00")),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_parse_amount_brl(raw, expected):
    assert parse_amount_brl(raw) == expected


def test_brl_to_cents():
    assert brl_to_cents(Decimal("169.80")) == 16980
    assert brl_to_cents(Decimal("1169.80")) == 116980
    assert brl_to_cents(None) is None


def test_parse_naive_local_uses_kirvano_tz():
    dt = parse_naive_local("2023-12-18 16:38:17", "America/Sao_Paulo")
    assert dt == datetime(2023, 12, 18, 19, 38, 17, tzinfo=UTC)
    assert dt.tzinfo is not None
    # A different KIRVANO_TZ changes the interpretation.
    assert parse_naive_local("2023-12-18 16:38:17", "UTC") == datetime(
        2023, 12, 18, 16, 38, 17, tzinfo=UTC
    )


def test_parse_naive_local_tolerates_iso_and_garbage():
    assert parse_naive_local("2023-12-18T16:38:17Z") == datetime(
        2023, 12, 18, 16, 38, 17, tzinfo=UTC
    )
    assert parse_naive_local("2023-12-18T13:38:17-03:00") == datetime(
        2023, 12, 18, 16, 38, 17, tzinfo=UTC
    )
    assert parse_naive_local("") is None
    assert parse_naive_local(None) is None
    assert parse_naive_local("not a date") is None


def test_parse_payload_spec_example():
    p = parse_payload(SPEC_EXAMPLE, "America/Sao_Paulo")
    assert p.event == "PIX_GENERATED"
    assert p.sale_id == "D2RP8RQ7"
    assert p.checkout_id == "Q8J1N6K3"
    assert p.amount_cents == 16980
    assert p.created_at == datetime(2023, 12, 18, 19, 38, 17, tzinfo=UTC)
    assert p.payment.expires_at == datetime(2023, 12, 18, 20, 38, 17, tzinfo=UTC)
    assert p.customer.phone_number == "5511987654321"
    assert p.customer.name == "Fulano de Tal"
    assert p.is_pix
    # Order bumps are ignored when naming the product.
    assert p.product_name == "Produto"
    assert p.offer_id == "o-main"
    assert p.idempotency_key() == "PIX_GENERATED|D2RP8RQ7|2023-12-18 16:38:17"


def test_parse_payload_tolerates_missing_sections():
    p = parse_payload({"event": "pix_generated", "sale_id": "X"})
    assert p.event == "PIX_GENERATED"
    assert p.customer.phone_number is None
    assert p.payment.expires_at is None
    assert p.products == []
    assert p.product_name is None
    assert p.amount_cents is None
    assert p.idempotency_key() == "PIX_GENERATED|X|"


def test_idempotency_key_without_ids_uses_hash():
    a = parse_payload({"event": "WEIRD", "foo": 1})
    b = parse_payload({"event": "WEIRD", "foo": 2})
    assert a.idempotency_key() != b.idempotency_key()
    assert a.idempotency_key().startswith("WEIRD|sha:")
