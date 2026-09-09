"""Template payload builder, parameter sanitiser, formatting helpers."""

from datetime import UTC, datetime, timedelta

import pytest

from app.format import SP_TZ, first_name, fmt_brl, fmt_dt_sp
from app.models import Order
from app.whatsapp import (
    build_template_payload,
    build_text_payload,
    sanitize_param,
    template_preview,
)
from tests.conftest import DEFAULT_NOW


def make_order(**kw) -> Order:
    base = dict(
        id=1,
        sale_id="D2RP8RQ7",
        customer_name="maria da silva",
        phone_e164="5549988760799",
        phone_alt="554988760799",
        amount_cents=9700,
        pix_expires_at=datetime(2026, 9, 15, 18, 0, tzinfo=SP_TZ).astimezone(UTC),
        page_token="abc123XYZ_-0987654321",
        product_name="Jornada com Meu Anjo",
        status="pending",
        created_at=DEFAULT_NOW,
        updated_at=DEFAULT_NOW,
    )
    base.update(kw)
    return Order(**base)


def test_build_template_payload_matches_spec_shape(store, settings):
    order = make_order()
    body = build_template_payload(order, store, to="5549988760799", settings=settings)
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "5549988760799"
    assert body["type"] == "template"
    assert body["template"]["name"] == "pix_pendente_v2"
    assert body["template"]["language"] == {"code": "pt_BR"}
    comps = body["template"]["components"]
    assert comps[0] == {
        "type": "body",
        "parameters": [
            {"type": "text", "text": "Maria"},
            {"type": "text", "text": "D2RP8RQ7"},
            {"type": "text", "text": "97,00"},
            {"type": "text", "text": "15/09 às 18:00"},
        ],
    }
    assert comps[1] == {
        "type": "button",
        "sub_type": "url",
        "index": "1",
        "parameters": [{"type": "text", "text": "abc123XYZ_-0987654321"}],
    }


def test_button_index_and_param_order_are_configurable(session, store, settings):
    store.set("url_button_index", 0)
    store.set("template_params", "sale_id,first_name,product")
    store.set("template_name", "pix_fallback")
    store.set("template_language", "pt_PT")
    body = build_template_payload(make_order(), store, to="5549988760799", settings=settings)
    assert body["template"]["name"] == "pix_fallback"
    assert body["template"]["language"]["code"] == "pt_PT"
    params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
    assert params == ["D2RP8RQ7", "Maria", "Jornada com Meu Anjo"]
    assert body["template"]["components"][1]["index"] == "0"

    store.set("url_button_index", -1)
    body = build_template_payload(make_order(), store, to="5549988760799", settings=settings)
    assert len(body["template"]["components"]) == 1  # no URL button component


def test_template_fallbacks(store, settings):
    order = make_order(
        customer_name=None, amount_cents=None, pix_expires_at=None, product_name=None
    )
    body = build_template_payload(order, store, to="5549988760799", settings=settings)
    params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
    assert params == ["cliente", "D2RP8RQ7", "0,00", "hoje"]


def test_expiry_fallback_uses_configured_minutes(store, settings, monkeypatch):
    monkeypatch.setattr(settings, "kirvano_pix_expiry_minutes", 60)
    order = make_order(pix_expires_at=None, created_at=DEFAULT_NOW)  # 12:00 SP
    body = build_template_payload(order, store, to="55", settings=settings)
    assert body["template"]["components"][0]["parameters"][3]["text"] == "08/09 às 13:00"


def test_text_payload_and_preview(store):
    assert build_text_payload("55", "oi") == {
        "messaging_product": "whatsapp",
        "to": "55",
        "type": "text",
        "text": {"body": "oi"},
    }
    assert template_preview(store, ["Maria", "X"]) == "[modelo pix_pendente_v2] Maria | X"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Maria\nSilva", "Maria Silva"),
        ("Maria\tSilva", "Maria Silva"),
        ("Maria     Silva", "Maria Silva"),
        ("  Maria  ", "Maria"),
        ("Ma\x00ria\x1f", "Maria"),
        ("Maria\r\n\r\nSilva", "Maria Silva"),
        (None, ""),
        (123, "123"),
    ],
)
def test_sanitize_param(raw, expected):
    assert sanitize_param(raw) == expected


def test_sanitize_param_length_and_ascii():
    long = "A" * 100
    assert len(sanitize_param(long)) == 60
    assert sanitize_param("João Antônio Ç", ascii_only=True) == "Joao Antonio C"
    assert sanitize_param("x" * 10, 4) == "xxxx"
    assert "\n" not in sanitize_param("a\n" * 50)
    assert "    " not in sanitize_param("a    b      c")


def test_format_helpers():
    assert fmt_brl(9700) == "97,00"
    assert fmt_brl(116980) == "1.169,80"
    assert fmt_brl(116980, symbol=True) == "R$ 1.169,80"
    assert fmt_brl(5) == "0,05"
    assert fmt_brl(None) == "0,00"
    assert fmt_brl(-1050) == "-10,50"
    assert fmt_dt_sp(datetime(2026, 9, 15, 21, 0, tzinfo=UTC)) == "15/09 às 18:00"
    assert fmt_dt_sp(None) == ""
    with pytest.raises(ValueError):
        fmt_dt_sp(datetime(2026, 1, 1))
    assert first_name("maria da silva") == "Maria"
    assert first_name("JOÃO PEDRO") == "João"
    assert first_name("D'Ávila Souza") == "D'Ávila"
    assert first_name("") == "cliente"
    assert first_name(None) == "cliente"
    assert first_name("   ") == "cliente"
    assert (DEFAULT_NOW + timedelta(hours=1)).tzinfo is not None


# --- template_params validation and the 1024-char body cap --------------------------


def test_unknown_template_param_key_is_rejected(store, session):
    """A typo used to become an EMPTY parameter and Meta failed every send (#132000)."""
    from app.panel import validate_setting
    from app.settings_store import SettingValueError

    assert validate_setting("template_params", "first_name,sale_id,amount,expiry") is None
    msg = validate_setting("template_params", "firstname,sale_id")
    assert msg and "firstname" in msg
    assert validate_setting("template_params", "  ") == "Informe pelo menos um parâmetro."

    # The API path (settings_store) is guarded too, not just the form.
    with pytest.raises(SettingValueError):
        store.set("template_params", "firstname,sale_id")
    with pytest.raises(SettingValueError):
        store.set("template_params", "")
    # A valid list is canonicalised (whitespace dropped).
    assert store.set("template_params", " sale_id , first_name ") == "sale_id,first_name"


def test_body_component_is_omitted_when_there_are_no_params(store, settings, monkeypatch):
    """`"parameters": []` is exactly what Meta answers #132000 to."""
    monkeypatch.setattr(type(store), "template_params", property(lambda self: []))
    body = build_template_payload(make_order(), store, to="55", settings=settings)
    components = body["template"]["components"]
    assert all(c["type"] != "body" for c in components)
    assert components == [
        {
            "type": "button",
            "sub_type": "url",
            "index": "1",
            "parameters": [{"type": "text", "text": "abc123XYZ_-0987654321"}],
        }
    ]


def test_long_params_are_trimmed_to_fit_the_1024_char_body(store, settings, session):
    from app.whatsapp import BODY_TEXT_BUDGET, PARAM_TOTAL_BUDGET, RENDERED_BODY_MAX

    store.set("template_params", "first_name,sale_id,product,page_url")
    order = make_order(product_name="Jornada " * 200)  # 1600 chars before sanitising
    body = build_template_payload(order, store, to="55", settings=settings)
    params = [p["text"] for p in body["template"]["components"][0]["parameters"]]
    assert sum(len(p) for p in params) <= PARAM_TOTAL_BUDGET
    assert PARAM_TOTAL_BUDGET + BODY_TEXT_BUDGET == RENDERED_BODY_MAX
    # The short, meaning-carrying values survive untouched; only the long one is cut.
    assert params[0] == "Maria" and params[1] == "D2RP8RQ7"


def test_short_params_are_never_trimmed():
    from app.whatsapp import fit_body_budget

    # Nothing to gain from mangling an order code, even if the budget is impossible.
    assert fit_body_budget(["Maria", "D2RP8RQ7"], budget=1) == ["Maria", "D2RP8RQ7"]
    # 40 → 32 → 24 → 16 → 8 (8 chars per pass), then 8 + 5 fits and the loop stops.
    assert fit_body_budget(["a" * 40, "b" * 5], budget=20) == ["a" * 8, "b" * 5]
