"""Tests for the operator scripts in ``scripts/``.

Only pure helpers (payload builders, formatters, response readers) and the CLI
wiring are exercised; every HTTP call is mocked with ``respx``. The most valuable
one is :func:`test_simulated_payload_matches_real_fixture_shape` plus the
round-trip through ``/webhooks/kirvano``: if the simulator ever drifts from the
real capture, the round-trip test stops proving anything and this catches it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import JobState, OrderStatus
from app.queries import job_for_order, order_by_sale_id
from scripts import check_template, send_test, simulate_kirvano, subscribe_app
from tests.conftest import DEFAULT_NOW, GRAPH_MESSAGES_URL, graph_error, graph_success

FIXTURE = Path(__file__).parent / "fixtures" / "kirvano_pix_generated.json"
TEMPLATES_URL = "https://graph.facebook.com/v23.0/958025707339789/message_templates"
SUBSCRIBED_APPS_URL = "https://graph.facebook.com/v23.0/958025707339789/subscribed_apps"


# =====================================================================================
# simulate_kirvano.py
# =====================================================================================


def test_simulated_payload_matches_real_fixture_shape() -> None:
    """The simulator must produce the same top-level keys as the real capture."""
    real = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fake = simulate_kirvano.build_payload("PIX_GENERATED")
    assert set(fake) == set(real) - {"_comment"}
    # ... and the same nested keys where the parser actually reads.
    assert set(fake["payment"]) == set(real["payment"])
    assert set(fake["customer"]) == set(real["customer"])
    assert set(fake["products"][0]) == set(real["products"][0])
    assert set(fake["fiscal"]) == set(real["fiscal"])


def test_simulated_payload_reproduces_the_payload_gotchas() -> None:
    created = datetime(2026, 7, 10, 20, 5, 30, tzinfo=UTC)  # 17:05:30 in Sao Paulo
    p = simulate_kirvano.build_payload(
        "PIX_GENERATED", sale_id="5LZEB2GJ", created_at=created, amount=97.0
    )
    assert p["created_at"] == "2026-07-10 17:05:30"
    # 24 h validity (this merchant's checkout), not the vendor doc's 1 h.
    assert p["payment"]["expires_at"] == "2026-07-11 17:05:30"
    # qrcode_image repeats the EMV string; it is NOT a URL.
    assert p["payment"]["qrcode_image"] == p["payment"]["qrcode"]
    assert not p["payment"]["qrcode_image"].startswith("http")
    # numeric fiscal.total_value alongside the formatted string
    assert p["fiscal"]["total_value"] == 97.0
    assert p["total_price"] == "R$ 97,00"
    # PII that our parser must drop
    assert p["customer"]["document"]
    assert p["cookies"]["fbp"]
    assert p["ip"]
    # camelCase and snake_case side by side at the top level
    assert "contactEmail" in p and "payment_method" in p


@pytest.mark.parametrize(
    ("value", "expected"),
    [(97.0, "R$ 97,00"), (169.8, "R$ 169,80"), (1169.8, "R$ 1.169,80"), (1.0, "R$ 1,00")],
)
def test_fmt_price(value: float, expected: str) -> None:
    assert simulate_kirvano.fmt_price(value) == expected


def test_simulated_status_events() -> None:
    approved = simulate_kirvano.build_payload("SALE_APPROVED", sale_id="ABC12345")
    assert approved["status"] == "APPROVED"
    assert approved["payment"]["finished_at"]

    expired = simulate_kirvano.build_payload("PIX_EXPIRED", checkout_id="XE11BWM0")
    assert expired["status"] == "CANCELED"
    # PIX_EXPIRED carries the link to generate a NEW PIX.
    assert expired["checkout_url"].startswith("https://pay.kirvano.com/recovery/")


def test_simulated_card_sale_has_no_pix_fields() -> None:
    card = simulate_kirvano.build_payload("SALE_APPROVED", method="credit_card")
    assert card["payment_method"] == "CREDIT_CARD" and card["payment"]["method"] == "CREDIT_CARD"
    assert "qrcode" not in card["payment"] and card["payment"]["finished_at"]
    args = simulate_kirvano.build_parser().parse_args(["--method", "CREDIT_CARD"])
    assert args.method == "CREDIT_CARD"


def test_random_code_shape() -> None:
    code = simulate_kirvano.random_code()
    assert len(code) == 8
    assert code.isupper() or code.isdigit()
    assert code.isalnum()


@pytest.mark.parametrize(
    ("token_in", "in_header", "in_body"),
    [("header", True, False), ("body", False, True), ("both", True, True), ("none", False, False)],
)
def test_token_placement(token_in: str, in_header: bool, in_body: bool) -> None:
    headers = simulate_kirvano.build_headers("tok", token_in=token_in)
    body = simulate_kirvano.with_body_token({"event": "X"}, "tok", token_in=token_in)
    assert ("x-kirvano-token" in headers) is in_header
    assert ("token" in body) is in_body
    # The original payload is never mutated.
    assert "token" not in {"event": "X"}


@respx.mock
def test_post_payload_sends_token_and_json() -> None:
    route = respx.post("http://testserver/webhooks/kirvano").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    payload = simulate_kirvano.build_payload("PIX_GENERATED")
    with httpx.Client() as client:
        resp = simulate_kirvano.post_payload(
            client, "http://testserver/webhooks/kirvano", payload, token="secret", token_in="both"
        )
    assert resp.status_code == 200
    request = route.calls.last.request
    assert request.headers["x-kirvano-token"] == "secret"
    assert json.loads(request.content)["token"] == "secret"


def test_simulated_payload_drives_the_real_webhook(
    client: TestClient, session: Session, settings: Settings, frozen_clock
) -> None:
    """End-to-end: the simulated body creates an order + a scheduled job, and the
    CPF/cookies are dropped — exactly like the real capture."""
    payload = simulate_kirvano.build_payload(
        "PIX_GENERATED",
        sale_id="SIM12345",
        created_at=DEFAULT_NOW,
        phone="5551994697674",
        amount=97.0,
    )
    resp = client.post(
        "/webhooks/kirvano",
        json=payload,
        headers=simulate_kirvano.build_headers("kirvano-test-token"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "processed"

    order = order_by_sale_id(session, "SIM12345")
    assert order is not None
    assert order.amount_cents == 9700
    assert order.status == OrderStatus.PENDING.value
    assert order.phone_e164 == "5551994697674"
    assert order.customer_document is None  # CPF never persisted
    assert order.pix_qr_image_url is None  # qrcode_image is not a URL
    assert order.pix_code.startswith("00020101")
    assert order.consent_ip == payload["ip"]

    job = job_for_order(session, order)
    assert job is not None
    assert job.state == JobState.SCHEDULED.value
    assert job.run_at == DEFAULT_NOW + timedelta(minutes=settings.reminder_delay_minutes)

    # ... and the follow-up event cancels the reminder.
    paid = simulate_kirvano.build_payload(
        "SALE_APPROVED", sale_id="SIM12345", created_at=DEFAULT_NOW, amount=97.0
    )
    resp = client.post(
        "/webhooks/kirvano", json=paid, headers=simulate_kirvano.build_headers("kirvano-test-token")
    )
    assert resp.status_code == 200
    session.expire_all()
    order = order_by_sale_id(session, "SIM12345")
    assert order is not None
    assert order.status == OrderStatus.PAID.value
    job = job_for_order(session, order)
    assert job is not None
    assert job.state == JobState.CANCELLED.value


def test_simulate_main_print_only(capsys: pytest.CaptureFixture[str], settings: Settings) -> None:
    rc = simulate_kirvano.main(["--print", "--event", "PIX_GENERATED"], settings=settings)
    assert rc == 0
    body = json.loads(capsys.readouterr().out)
    assert body["event"] == "PIX_GENERATED"
    # --token-in defaults to header, so the body carries no token.
    assert "token" not in body


# =====================================================================================
# send_test.py
# =====================================================================================


def test_send_test_payload_follows_the_configured_param_order(settings: Settings) -> None:
    values = send_test.param_values(
        settings,
        name="Maria Souza",
        sale_id="5LZEB2GJ",
        amount="97,00",
        expiry="11/07 às 17:05",
        page_token="abc123",
    )
    payload = send_test.build_template_payload(
        settings,
        to="5551994697674",
        values=values,
        template_name="pix_pendente_v2",
        language="pt_BR",
        params=["first_name", "sale_id", "amount", "expiry"],
        button_index=1,
        page_token="abc123",
    )
    assert payload["messaging_product"] == "whatsapp"
    assert payload["template"]["language"] == {"code": "pt_BR"}
    body = payload["template"]["components"][0]
    assert [p["text"] for p in body["parameters"]] == [
        "Maria",
        "5LZEB2GJ",
        "97,00",
        "11/07 às 17:05",
    ]
    button = payload["template"]["components"][1]
    assert button["sub_type"] == "url"
    assert button["index"] == "1"  # Meta wants the index as a STRING
    assert button["parameters"][0]["text"] == "abc123"


def test_send_test_payload_without_url_button(settings: Settings) -> None:
    payload = send_test.build_template_payload(
        settings,
        to="55",
        values={"sale_id": "X"},
        template_name="t",
        language="pt_BR",
        params=["sale_id"],
        button_index=-1,  # template has no URL button
        page_token="abc",
    )
    assert len(payload["template"]["components"]) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Maria\nSouza", "Maria Souza"),
        ("Maria\tSouza", "Maria Souza"),
        ("Maria     Souza", "Maria Souza"),
        ("  Maria  ", "Maria"),
        ("Maria\x00Souza", "MariaSouza"),
    ],
)
def test_sanitize_removes_what_meta_rejects(raw: str, expected: str) -> None:
    # Newline, tab and 4+ consecutive spaces trigger #131009.
    assert send_test.sanitize(raw) == expected


def test_sanitize_caps_length() -> None:
    assert len(send_test.sanitize("a" * 200)) == 60
    assert send_test.first_name("Maria Souza de Oliveira") == "Maria"
    assert send_test.first_name(None) == "cliente"


def test_fmt_expiry_is_sao_paulo_local() -> None:
    # 2026-07-11 20:05 UTC == 17:05 in Sao Paulo (UTC-3).
    dt = datetime(2026, 7, 11, 20, 5, tzinfo=UTC)
    assert send_test.fmt_expiry(dt) == "11/07 às 17:05"


@respx.mock
def test_send_test_send_uses_bearer_token(settings: Settings) -> None:
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=httpx.Response(200, json=graph_success())
    )
    with httpx.Client() as client:
        resp = send_test.send(client, settings, send_test.build_text_payload("55119", "oi"))
    assert resp.status_code == 200
    assert route.calls.last.request.headers["authorization"] == "Bearer meta-test-token"
    assert "enviado" in send_test.describe_response(resp)
    assert "wamid.OK" in send_test.describe_response(resp)


def test_describe_response_explains_the_error_code() -> None:
    resp = httpx.Response(400, json=graph_error(131026))
    text = send_test.describe_response(resp)
    assert "#131026" in text
    assert "9o digito" in text  # the pt-BR hint from the error table


@respx.mock
def test_send_test_main_reports_failure(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(GRAPH_MESSAGES_URL).mock(return_value=httpx.Response(400, json=graph_error(132001)))
    rc = send_test.main(["--to", "55 51 99469-7674", "--order", "5LZEB2GJ"], settings=settings)
    out = capsys.readouterr().out
    assert rc == 1
    assert "#132001" in out
    assert "5551994697674" in out  # non-digits stripped from --to


def test_send_test_main_print_only(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    rc = send_test.main(["--to", "5551994697674", "--print"], settings=settings)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["template"]["name"] == "pix_pendente_v2"
    assert len(payload["template"]["components"][0]["parameters"]) == 4


# =====================================================================================
# check_template.py
# =====================================================================================


def test_check_template_urls(settings: Settings) -> None:
    assert check_template.templates_url(settings) == TEMPLATES_URL
    assert check_template.auth_headers(settings)["Authorization"] == "Bearer meta-test-token"


def test_select_templates_filters_by_name_and_language() -> None:
    data = {
        "data": [
            {"name": "pix_pendente_v2", "language": "pt_BR", "status": "APPROVED"},
            {"name": "pix_pendente_v2", "language": "en_US", "status": "REJECTED"},
            {"name": "outro", "language": "pt_BR", "status": "APPROVED"},
            "lixo",
        ]
    }
    got = check_template.select_templates(data, name="pix_pendente_v2", language="pt_BR")
    assert len(got) == 1
    assert got[0]["status"] == "APPROVED"
    assert check_template.select_templates({}, name="x") == []
    assert check_template.select_templates(None) == []


def test_format_template_and_category_warning() -> None:
    approved = {
        "name": "pix_pendente_v2",
        "language": "pt_BR",
        "status": "PAUSED",
        "category": "UTILITY",
    }
    line = check_template.format_template(approved)
    assert "pix_pendente_v2" in line and "PAUSED" in line and "#132015" in line
    assert check_template.category_warning([approved]) is None
    warning = check_template.category_warning([{**approved, "category": "MARKETING"}])
    assert warning is not None and "MARKETING" in warning


@respx.mock
def test_check_template_main_prints_status(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.get(TEMPLATES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "pix_pendente_v2",
                        "language": "pt_BR",
                        "status": "APPROVED",
                        "category": "UTILITY",
                        "id": "1",
                    }
                ]
            },
        )
    )
    rc = check_template.main([], settings=settings)
    out = capsys.readouterr().out
    assert rc == 0
    assert "APPROVED" in out and "UTILITY" in out
    request = route.calls.last.request
    assert request.url.params["name"] == "pix_pendente_v2"
    assert "category" in request.url.params["fields"]


@respx.mock
def test_check_template_main_when_missing(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(TEMPLATES_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    assert check_template.main([], settings=settings) == 1
    assert "Nenhum modelo encontrado" in capsys.readouterr().out


# =====================================================================================
# subscribe_app.py
# =====================================================================================


def test_subscribed_apps_url_and_mask(settings: Settings) -> None:
    assert subscribe_app.subscribed_apps_url(settings) == SUBSCRIBED_APPS_URL
    masked = subscribe_app.mask_token("EAAG" + "x" * 40 + "END1")
    assert "x" * 40 not in masked
    assert masked.startswith("EAAGxx") and masked.endswith("caracteres)")
    assert subscribe_app.mask_token(None) == "(ausente)"


def test_response_summary_reads_graph_errors() -> None:
    assert "erro #190" in subscribe_app.response_summary(httpx.Response(401, json=graph_error(190)))
    assert "success" in subscribe_app.response_summary(httpx.Response(200, json={"success": True}))
    assert "HTTP 502" in subscribe_app.response_summary(httpx.Response(502, text="bad gateway"))


@respx.mock
def test_subscribe_app_main_posts(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.post(SUBSCRIBED_APPS_URL).mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    rc = subscribe_app.main([], settings=settings)
    assert rc == 0
    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer meta-test-token"
    out = capsys.readouterr().out
    assert "meta-test-token" not in out  # the token is only ever printed masked


def test_subscribe_app_main_without_token(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    no_token = settings.model_copy(update={"meta_access_token": None})
    assert subscribe_app.main([], settings=no_token) == 2
    assert "META_ACCESS_TOKEN" in capsys.readouterr().out


def test_simulate_print_never_reveals_the_token(
    capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """`--print --token-in body` used to dump KIRVANO_WEBHOOK_TOKEN into the terminal."""
    rc = simulate_kirvano.main(
        ["--print", "--token-in", "body", "--token", "SUPER-SECRET-TOKEN"], settings=settings
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "SUPER-SECRET-TOKEN" not in out
    # The placeholder still shows WHERE the token would travel, which is the point.
    assert json.loads(out)["token"] == simulate_kirvano.TOKEN_PLACEHOLDER

    # The default token comes from the environment and must be masked too.
    rc = simulate_kirvano.main(["--print", "--token-in", "both"], settings=settings)
    assert rc == 0
    out = capsys.readouterr().out
    assert settings.kirvano_webhook_token not in out


def test_simulate_post_still_sends_the_real_token() -> None:
    """Masking is a --print concern only: the POST path must keep the real value."""
    body = simulate_kirvano.with_body_token({"event": "X"}, "real-token", token_in="body")
    assert body["token"] == "real-token"


@respx.mock
@pytest.mark.parametrize("event", ["ABANDONED_CART", "SALE_APPROVED", "PIX_EXPIRED"])
def test_simulate_main_posts_every_event(
    event: str, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """The summary printed before the POST used to crash on ABANDONED_CART (no sale_id,
    no payment block), so the cart test never reached the server."""
    route = respx.post("https://api.test.local/webhooks/kirvano").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    rc = simulate_kirvano.main(
        [
            "--event",
            event,
            "--phone",
            "+554398150536",
            "--url",
            "https://api.test.local/webhooks/kirvano",
        ],
        settings=settings,
    )
    assert rc == 0 and route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["event"] == event and sent["customer"]["phone_number"] == "+554398150536"
    assert "HTTP 200" in capsys.readouterr().out
