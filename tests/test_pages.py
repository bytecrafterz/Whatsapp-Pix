"""Public pages: /p/{token} (all states + QR PNG) and /privacidade."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus
from app.pages import PRIVACY_UPDATED_AT, effective_state, new_pix_url, safe_http_url
from tests.conftest import DEFAULT_NOW, dumps

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kirvano_pix_generated.json"

# A real-shaped EMV copia-e-cola string (same one as the captured fixture).
PIX_CODE = (
    "00020101021226990014br.gov.bcb.pix2577pix.bancogenial.com/qr/v3/at/"
    "1f8c9a2e-4d7b-4a10-9c3e-2b6f8d5a7e41520400005303986540597.005802BR"
    "5925CONNECT LT NEGOCIOS DIGIT6009SAO PAULO62070503***6304A1B2"
)
FULL_NAME = "Maria Souza de Oliveira"
EMAIL = "maria.souza.exemplo@gmail.com"
PHONE = "5551994697674"
CPF = "00000000191"

TOKEN = "tok_abc123XYZ_-0987654321"


def make_order(session: Session, **kw) -> Order:
    """Insert one order; committed so the request's own session sees it."""
    base = dict(
        sale_id="5LZEB2GJ",
        checkout_id="XE11BWM0",
        product_name="A Jornada com meu Anjo",
        customer_name=FULL_NAME,
        customer_email=EMAIL,
        phone_raw=PHONE,
        phone_e164=PHONE,
        phone_alt="555194697674",
        amount_cents=9700,
        currency="BRL",
        pix_code=PIX_CODE,
        pix_expires_at=DEFAULT_NOW + timedelta(hours=24),
        status=OrderStatus.PENDING.value,
        page_token=TOKEN,
        consent_ip="203.0.113.42",
        consent_at=DEFAULT_NOW,
        created_at=DEFAULT_NOW,
        updated_at=DEFAULT_NOW,
    )
    base.update(kw)
    order = Order(**base)
    session.add(order)
    session.commit()
    return order


# --- pure helpers -------------------------------------------------------------------------


def test_safe_http_url_only_accepts_http_schemes():
    assert safe_http_url("https://pay.kirvano.com/x") == "https://pay.kirvano.com/x"
    assert safe_http_url("  http://example.com/y  ") == "http://example.com/y"
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url("data:text/html,<script>") is None
    assert safe_http_url("") is None
    assert safe_http_url(None) is None


def test_effective_state_expires_by_the_clock_not_by_the_event(session):
    """Kirvano's PIX_EXPIRED can lag behind the printed expiry; the page must not."""
    order = make_order(session)
    assert effective_state(order, DEFAULT_NOW) == "pending"
    assert effective_state(order, DEFAULT_NOW + timedelta(hours=25)) == "expired"
    order.status = OrderStatus.PAID.value
    # A paid order stays paid even after the code's validity window has passed.
    assert effective_state(order, DEFAULT_NOW + timedelta(hours=25)) == "paid"


def test_new_pix_url_prefers_the_order_recovery_link(session, store):
    order = make_order(session, checkout_recovery_url="https://pay.kirvano.com/recovery/uuid")
    assert new_pix_url(order, store) == "https://pay.kirvano.com/recovery/uuid"
    order.checkout_recovery_url = None
    # Falls back to the panel setting (KIRVANO_CHECKOUT_URL in the test env).
    assert new_pix_url(order, store) == "https://pay.kirvano.com/checkout-uuid"
    order.checkout_recovery_url = "javascript:alert(1)"
    assert new_pix_url(order, store) == "https://pay.kirvano.com/checkout-uuid"


# --- pending page --------------------------------------------------------------------------


def test_pending_page_shows_code_qr_amount_and_expiry(client, session, frozen_clock):
    make_order(session)
    r = client.get(f"/p/{TOKEN}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "Aguardando pagamento" in html
    assert "5LZEB2GJ" in html
    assert "A Jornada com meu Anjo" in html
    assert "R$ 97,00" in html
    assert PIX_CODE in html  # copia e cola, inside a readonly textarea
    assert 'id="pix-code"' in html and "readonly" in html
    assert "Copiar código" in html
    assert f'src="/p/{TOKEN}/qr.png"' in html
    # Expiry: 24 h after 2026-09-08 15:00 UTC → 09/09 às 12:00 in São Paulo.
    assert "09/09 às 12:00" in html
    assert 'id="contagem"' in html and 'data-expira="2026-09-09T15:00:00+00:00"' in html


def test_pending_page_leaks_no_pii_beyond_the_first_name(client, session, frozen_clock):
    make_order(session)
    html = client.get(f"/p/{TOKEN}").text
    assert "Maria" in html  # first name only
    assert "Souza" not in html and "Oliveira" not in html
    assert EMAIL not in html
    assert PHONE not in html and "555194697674" not in html
    assert CPF not in html
    assert "203.0.113.42" not in html  # consent IP is operator-only


def test_pending_page_has_copy_button_with_clipboard_and_fallback(client, session, frozen_clock):
    make_order(session)
    html = client.get(f"/p/{TOKEN}").text
    assert "navigator.clipboard" in html
    assert "execCommand" in html
    assert "Copiado!" in html
    # No external assets, no tracking on a customer-facing page.
    assert "http://" not in html
    assert 'src="https://' not in html and 'href="https://' not in html


def test_page_has_no_external_stylesheets_or_scripts(client, session, frozen_clock):
    make_order(session)
    html = client.get(f"/p/{TOKEN}").text
    assert "<link" not in html
    assert "<script src" not in html


# --- QR endpoint ---------------------------------------------------------------------------


def test_qr_png_renders_server_side(client, session, frozen_clock):
    make_order(session)
    r = client.get(f"/p/{TOKEN}/qr.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(r.content) > 200


def test_qr_png_404_without_code_or_token(client, session, frozen_clock):
    make_order(session, pix_code=None)
    assert client.get(f"/p/{TOKEN}/qr.png").status_code == 404
    assert client.get("/p/nao-existe/qr.png").status_code == 404


def test_page_falls_back_to_kirvano_image_url_when_there_is_no_code(client, session, frozen_clock):
    """Only when the stored value really is a URL — for this merchant it is NULL."""
    make_order(session, pix_code=None, pix_qr_image_url="https://cdn.example.com/qr.png")
    html = client.get(f"/p/{TOKEN}").text
    assert 'src="https://cdn.example.com/qr.png"' in html
    assert '/qr.png"' in html
    assert 'id="pix-code"' not in html


def test_page_never_renders_a_non_url_qrcode_image(client, session, frozen_clock):
    """payment.qrcode_image is the EMV string in the real payload — never an <img src>."""
    make_order(session, pix_code=None, pix_qr_image_url=PIX_CODE)
    html = client.get(f"/p/{TOKEN}").text
    assert f'src="{PIX_CODE}"' not in html
    assert "não está disponível nesta página" in html


# --- other states --------------------------------------------------------------------------


def test_paid_page(client, session, frozen_clock):
    make_order(session, status=OrderStatus.PAID.value, paid_at=DEFAULT_NOW)
    html = client.get(f"/p/{TOKEN}").text
    assert "Pagamento confirmado" in html
    assert "08/09/2026 12:00" in html
    assert PIX_CODE not in html  # nothing to pay any more
    assert "Copiar código" not in html


def test_expired_by_status_offers_the_recovery_link(client, session, frozen_clock):
    make_order(
        session,
        status=OrderStatus.EXPIRED.value,
        checkout_recovery_url="https://pay.kirvano.com/recovery/uuid-1",
    )
    html = client.get(f"/p/{TOKEN}").text
    assert "Este PIX expirou" in html
    assert "Gerar novo PIX" in html
    assert 'href="https://pay.kirvano.com/recovery/uuid-1"' in html
    assert PIX_CODE not in html


def test_expired_by_the_clock_falls_back_to_the_settings_checkout_url(
    client, session, frozen_clock
):
    make_order(session)  # still 'pending' in the DB
    frozen_clock.set(DEFAULT_NOW + timedelta(hours=25))
    html = client.get(f"/p/{TOKEN}").text
    assert "Este PIX expirou" in html
    assert 'href="https://pay.kirvano.com/checkout-uuid"' in html


def test_expired_without_any_link_explains_what_to_do(client, session, store, frozen_clock):
    store.set("checkout_url", "")
    session.commit()
    make_order(session, status=OrderStatus.EXPIRED.value)
    html = client.get(f"/p/{TOKEN}").text
    assert "Este PIX expirou" in html
    assert "Gerar novo PIX" not in html
    assert "jornadacommeuanjo@outlook.com" in html


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (OrderStatus.REFUSED.value, "O pagamento não foi aprovado"),
        (OrderStatus.REFUNDED.value, "Este pedido foi reembolsado"),
        (OrderStatus.CHARGEBACK.value, "Este pedido está em contestação"),
        (OrderStatus.UNKNOWN.value, "Não conseguimos confirmar este pedido"),
    ],
)
def test_friendly_pages_for_the_remaining_states(client, session, frozen_clock, status, expected):
    make_order(session, status=status)
    r = client.get(f"/p/{TOKEN}")
    assert r.status_code == 200
    assert expected in r.text
    assert PIX_CODE not in r.text


# --- unknown token -------------------------------------------------------------------------


def test_unknown_token_renders_a_ptbr_404_page(client, session, frozen_clock):
    r = client.get("/p/token-que-nao-existe")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"
    assert "Não encontramos esta página" in r.text
    assert "<html" in r.text  # a page, not a JSON error


# --- end-to-end with the REAL captured payload ---------------------------------------------


def test_real_webhook_payload_produces_a_working_pix_page(client, session, frozen_clock):
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # Just after the real event fired (2026-07-10 17:05:30 São Paulo = 20:05:30 UTC).
    frozen_clock.set(datetime(2026, 7, 10, 20, 10, 0, tzinfo=UTC))
    r = client.post(
        "/webhooks/kirvano",
        content=dumps(payload),
        headers={"content-type": "application/json", "x-kirvano-token": "kirvano-test-token"},
    )
    assert r.status_code == 200

    order = session.query(Order).filter_by(sale_id="5LZEB2GJ").one()
    page = client.get(f"/p/{order.page_token}")
    assert page.status_code == 200
    html = page.text
    assert "R$ 97,00" in html
    assert "A Jornada com meu Anjo" in html
    assert payload["payment"]["qrcode"] in html
    assert "11/07 às 17:05" in html  # 24 h validity, São Paulo time
    assert CPF not in html and payload["cookies"]["fbp"] not in html
    assert payload["ip"] not in html
    # The QR is drawn from the code, never from the (non-URL) qrcode_image field.
    qr = client.get(f"/p/{order.page_token}/qr.png")
    assert qr.status_code == 200 and qr.content[:8] == b"\x89PNG\r\n\x1a\n"


# --- privacy policy ------------------------------------------------------------------------


def test_privacy_page_covers_everything_the_spec_requires(client):
    r = client.get("/privacidade")
    assert r.status_code == 200
    html = r.text
    for needle in (
        "Política de Privacidade",
        "CONNECT LT NEGOCIOS DIGITAIS LTDA",
        "52.134.502/0001-09",
        "Florianópolis",
        "LT.CONNECT@OUTLOOK.COM",
        "Art. 7º, V",
        "Art. 7º, IX",
        "art. 18",
        "12 meses",
        "SAIR",
        "Não quero receber",
        "WhatsApp",
        "Kirvano",
        "Hostinger",
        "ANPD",
        "transferência internacional",
        PRIVACY_UPDATED_AT,
    ):
        assert needle in html, needle
    # Honesty guards: no certification or absolute-compliance claims.
    lowered = html.lower()
    for forbidden in ("100% conform", "certificad", "totalmente seguro", "garantimos a segurança"):
        assert forbidden not in lowered, forbidden


def test_privacy_page_states_the_cpf_is_not_stored(client):
    html = client.get("/privacidade").text
    assert "não armazenamos o seu CPF" in html


def test_home_page_renders(client):
    """The bare domain must not return a raw 404 to Meta reviewers or customers."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Jornada com Meu Anjo" in r.text
    assert "SAIR" in r.text
    assert "/privacidade" in r.text
    assert r.headers["cache-control"] == "no-store"
