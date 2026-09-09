"""The client panel: auth, CSRF, and every page/mutation it exposes.

All Graph API traffic is mocked with respx; no test ever touches the network.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select

from app.config import get_settings, reset_settings_cache
from app.inbound import record_outbound_message, upsert_contact
from app.kirvano import handle_event
from app.models import (
    Contact,
    JobState,
    Message,
    OptOut,
    Order,
    RecoveryJob,
    Setting,
    TemplateStatus,
)
from app.panel import reason_label, validate_setting
from tests.conftest import (
    DEFAULT_NOW,
    GRAPH_MESSAGES_URL,
    graph_error,
    graph_success,
    kirvano_payload,
)

AUTH = ("admin", "panel-pw")
ORIGIN = {"Origin": "http://testserver"}
TEMPLATES_URL = "https://graph.facebook.com/v23.0/958025707339789/message_templates"

NONCE_RE = re.compile(r'name="nonce" value="([0-9a-f]{64})"')


def nonce_from(html: str) -> str:
    match = NONCE_RE.search(html)
    assert match, "hidden nonce missing from the form"
    return match.group(1)


def get_nonce(client: TestClient, path: str) -> str:
    page = client.get(path, auth=AUTH)
    assert page.status_code == 200, page.text[:400]
    return nonce_from(page.text)


def make_order(session, settings, *, sale_id: str = "5LZEB2GJ", phone: str = "5551994697674"):
    """Create a pending order + scheduled job through the real Kirvano path."""
    handle_event(session, kirvano_payload(sale_id=sale_id, phone=phone), settings=settings)
    session.commit()
    return session.execute(select(Order).where(Order.sale_id == sale_id)).scalar_one()


# --- authentication ---------------------------------------------------------------


def test_panel_requires_basic_auth(client):
    r = client.get("/painel")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="Painel"'


@pytest.mark.parametrize("creds", [("admin", "wrong"), ("outro", "panel-pw"), ("", "")])
def test_panel_rejects_bad_credentials(client, creds):
    r = client.get("/painel", auth=creds)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="Painel"'


def test_panel_rejects_malformed_authorization_header(client):
    r = client.get("/painel", headers={"Authorization": "Basic not-base64!!"})
    assert r.status_code == 401


def test_panel_accepts_valid_credentials_and_is_not_cached(client):
    r = client.get("/painel", auth=AUTH)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "Início" in r.text


def test_panel_returns_503_without_a_password(monkeypatch, engine):
    """An unconfigured panel must refuse to serve, never serve unauthenticated."""
    from app.main import create_app

    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    reset_settings_cache()
    try:
        with TestClient(create_app(get_settings())) as c:
            r = c.get("/painel", auth=AUTH)
            assert r.status_code == 503
            assert "PANEL_PASSWORD" in r.text
    finally:
        reset_settings_cache()


# --- Início -----------------------------------------------------------------------


def test_inicio_shows_counters_and_recent_orders(client, session, settings, frozen_clock):
    make_order(session, settings, sale_id="AAA11111")
    make_order(session, settings, sale_id="BBB22222", phone="5511987654321")
    handle_event(
        session,
        kirvano_payload(
            "SALE_APPROVED", sale_id="BBB22222", created_at=DEFAULT_NOW + timedelta(minutes=1)
        ),
        settings=settings,
    )
    session.commit()

    r = client.get("/painel", auth=AUTH)
    assert r.status_code == 200
    assert "AAA11111" in r.text and "BBB22222" in r.text
    assert "pendentes agora" in r.text
    assert "agendado" in r.text  # job state label for AAA11111
    assert "cancelado: pago" in r.text  # reason label for BBB22222
    assert 'class="badge ok"' in r.text and 'class="badge warn"' in r.text


def test_reason_label_covers_prefixed_families():
    assert reason_label("quiet_hours") == "adiado (horário silencioso)"
    assert reason_label("order_paid") == "pedido já pago"
    assert reason_label("retry_130429") == "nova tentativa (erro 130429)"
    assert reason_label("graph_9999") == "erro 9999 da Meta"
    assert reason_label(None) == ""


# --- Configurações ----------------------------------------------------------------


def test_configuracoes_form_shows_current_values(client, session, settings):
    r = client.get("/painel/configuracoes", auth=AUTH)
    assert r.status_code == 200
    assert 'name="delay_minutes"' in r.text and 'value="10"' in r.text
    assert 'name="template_name"' in r.text and "pix_pendente_v2" in r.text
    assert nonce_from(r.text)


def test_configuracoes_saves_valid_values(client, session):
    nonce = get_nonce(client, "/painel/configuracoes")
    r = client.post(
        "/painel/configuracoes",
        auth=AUTH,
        data={
            "nonce": nonce,
            "enabled": "true",
            "delay_minutes": "20",
            "quiet_start": "21:30",
            "quiet_end": "07:00",
            "daily_recipient_limit": "1000",
            "template_name": "pix_pendente_v3",
            "template_language": "pt_BR",
            "url_button_index": "0",
            "template_params": "first_name,sale_id,amount,expiry",
            "checkout_url": "https://pay.kirvano.com/x",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/painel/configuracoes?ok=config"
    stored = {s.key: s.value for s in session.execute(select(Setting)).scalars()}
    assert stored["delay_minutes"] == "20"
    assert stored["quiet_start"] == "21:30"
    assert stored["template_name"] == "pix_pendente_v3"
    assert stored["url_button_index"] == "0"
    # And the redirect target shows the confirmation.
    assert "Configurações salvas." in client.get("/painel/configuracoes?ok=config", auth=AUTH).text


def test_configuracoes_unchecked_switch_turns_reminders_off(client, session):
    nonce = get_nonce(client, "/painel/configuracoes")
    r = client.post(
        "/painel/configuracoes",
        auth=AUTH,
        data={
            "nonce": nonce,
            # "enabled" absent = checkbox unchecked
            "delay_minutes": "10",
            "quiet_start": "22:00",
            "quiet_end": "08:00",
            "daily_recipient_limit": "250",
            "template_name": "pix_pendente_v2",
            "template_language": "pt_BR",
            "url_button_index": "1",
            "template_params": "first_name,sale_id,amount,expiry",
            "checkout_url": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert session.get(Setting, "enabled").value == "false"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delay_minutes", "0"),
        ("delay_minutes", "1441"),
        ("delay_minutes", "dez"),
        ("quiet_start", "9:5"),
        ("quiet_end", "25:00"),
        ("daily_recipient_limit", "0"),
        ("daily_recipient_limit", "100001"),
        ("template_name", "PIX_Pendente"),
        ("template_name", "pix pendente"),
        ("url_button_index", "10"),
        ("url_button_index", "-2"),
        # A one-character typo used to become an EMPTY template parameter and Meta
        # rejected EVERY reminder with #132000/#132012 (SPEC line 66: "code bug").
        ("template_params", "firstname,sale_id"),
        ("template_params", "first_name,sale_id,amont"),
        ("template_params", ""),
        ("template_params", "   ,  "),
    ],
)
def test_configuracoes_rejects_invalid_values_without_saving(client, session, field, value):
    nonce = get_nonce(client, "/painel/configuracoes")
    payload = {
        "nonce": nonce,
        "enabled": "true",
        "delay_minutes": "10",
        "quiet_start": "22:00",
        "quiet_end": "08:00",
        "daily_recipient_limit": "250",
        "template_name": "pix_pendente_v2",
        "template_language": "pt_BR",
        "url_button_index": "1",
        "template_params": "first_name,sale_id,amount,expiry",
        "checkout_url": "",
    }
    payload[field] = value
    r = client.post("/painel/configuracoes", auth=AUTH, data=payload, follow_redirects=False)
    assert r.status_code == 400
    assert "nada foi salvo" in r.text
    # Nothing at all was written: not even the valid fields of the same form.
    assert session.execute(select(Setting)).scalars().first() is None


def test_url_button_index_accepts_the_no_button_sentinel(client, session):
    """-1 is the documented "template has no URL button" value (ARCHITECTURE §6)."""
    assert validate_setting("url_button_index", "-1") is None
    nonce = get_nonce(client, "/painel/configuracoes")
    r = client.post(
        "/painel/configuracoes",
        auth=AUTH,
        data={
            "nonce": nonce,
            "enabled": "true",
            "delay_minutes": "10",
            "quiet_start": "22:00",
            "quiet_end": "08:00",
            "daily_recipient_limit": "250",
            "template_name": "pix_pendente_v2",
            "template_language": "pt_BR",
            "url_button_index": "-1",
            "template_params": "first_name,sale_id,amount,expiry",
            "checkout_url": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert session.get(Setting, "url_button_index").value == "-1"


# --- CSRF -------------------------------------------------------------------------


def test_post_without_nonce_is_refused(client, session):
    r = client.post("/painel/descadastros", auth=AUTH, data={"phone": "5551994697674"})
    assert r.status_code == 403
    assert "Formulário expirado" in r.text
    assert session.execute(select(OptOut)).scalars().first() is None


def test_post_from_another_origin_is_refused(client, session):
    nonce = get_nonce(client, "/painel/descadastros")
    r = client.post(
        "/painel/descadastros",
        auth=AUTH,
        data={"nonce": nonce, "phone": "5551994697674"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "origem da requisição" in r.text
    assert session.execute(select(OptOut)).scalars().first() is None


def test_post_with_same_origin_header_is_accepted(client, session):
    nonce = get_nonce(client, "/painel/descadastros")
    r = client.post(
        "/painel/descadastros",
        auth=AUTH,
        data={"nonce": nonce, "phone": "5551994697674"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_panel_has_no_get_mutations(client):
    """Mutations are POST-only: the same paths must not answer GET with a change."""
    assert client.get("/painel/descadastros/remover", auth=AUTH).status_code == 405


# --- Conversas --------------------------------------------------------------------


def test_conversas_list_shows_window_badges(client, session, frozen_clock):
    upsert_contact(session, "5551994697674", profile_name="Maria", inbound_at=frozen_clock.now)
    upsert_contact(
        session,
        "5511987654321",
        profile_name="João",
        inbound_at=frozen_clock.now - timedelta(hours=30),
    )
    record_outbound_message(
        session,
        wa_id="5551994697674",
        phone="5551994697674",
        message_id="wamid.OUT1",
        kind="text",
        body="Oi, tudo bem?",
        now=frozen_clock.now,
    )
    session.commit()

    r = client.get("/painel/conversas", auth=AUTH)
    assert r.status_code == 200
    assert "janela aberta" in r.text and "janela fechada" in r.text
    assert "Maria" in r.text and "João" in r.text
    assert "Oi, tudo bem?" in r.text


def test_conversa_unknown_contact_is_404(client):
    r = client.get("/painel/conversas/5500000000000", auth=AUTH)
    assert r.status_code == 404
    assert "Conversa não encontrada." in r.text


@respx.mock
def test_reply_sends_free_text_when_window_open(client, session, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(200, json=graph_success("5551994697674", "wamid.REPLY"))
    )
    upsert_contact(session, "5551994697674", profile_name="Maria", inbound_at=frozen_clock.now)
    session.commit()

    nonce = get_nonce(client, "/painel/conversas/5551994697674")
    r = client.post(
        "/painel/conversas/5551994697674",
        auth=AUTH,
        data={"nonce": nonce, "body": "Seu PIX continua valendo!"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/painel/conversas/5551994697674?ok=resposta"
    assert route.called
    sent = route.calls[0].request
    assert b"Seu PIX continua valendo!" in sent.content

    session.expire_all()
    msg = session.execute(
        select(Message).where(Message.wa_message_id == "wamid.REPLY")
    ).scalar_one()
    assert msg.direction == "out" and msg.kind == "text"
    assert msg.body == "Seu PIX continua valendo!"
    assert session.get(Contact, "5551994697674").last_outbound_at == frozen_clock.now


@respx.mock
def test_reply_is_blocked_when_window_closed(client, session, frozen_clock):
    route = respx.post(GRAPH_MESSAGES_URL).mock(return_value=Response(200, json=graph_success()))
    upsert_contact(
        session,
        "5551994697674",
        profile_name="Maria",
        inbound_at=frozen_clock.now - timedelta(hours=25),
    )
    session.commit()

    page = client.get("/painel/conversas/5551994697674", auth=AUTH)
    assert "janela fechada" in page.text
    r = client.post(
        "/painel/conversas/5551994697674",
        auth=AUTH,
        data={"nonce": nonce_from(page.text), "body": "oi"},
    )
    assert r.status_code == 400
    assert "janela de 24 horas está fechada" in r.text
    assert not route.called
    session.expire_all()
    assert session.execute(select(Message)).scalars().first() is None


@respx.mock
def test_reply_reports_a_graph_error_without_recording_a_message(client, session, frozen_clock):
    respx.post(GRAPH_MESSAGES_URL).mock(
        return_value=Response(400, json=graph_error(131047, "Re-engagement message"))
    )
    upsert_contact(session, "5551994697674", inbound_at=frozen_clock.now)
    session.commit()

    nonce = get_nonce(client, "/painel/conversas/5551994697674")
    r = client.post(
        "/painel/conversas/5551994697674",
        auth=AUTH,
        data={"nonce": nonce, "body": "oi"},
    )
    assert r.status_code == 502
    assert "A Meta recusou o envio" in r.text
    session.expire_all()
    assert session.execute(select(Message)).scalars().first() is None


def test_reply_requires_a_body(client, session, frozen_clock):
    upsert_contact(session, "5551994697674", inbound_at=frozen_clock.now)
    session.commit()
    nonce = get_nonce(client, "/painel/conversas/5551994697674")
    r = client.post(
        "/painel/conversas/5551994697674", auth=AUTH, data={"nonce": nonce, "body": "   "}
    )
    assert r.status_code == 400
    assert "Escreva uma mensagem" in r.text


# --- Descadastros -----------------------------------------------------------------


def test_add_opt_out_stores_both_variants_and_cancels_the_job(
    client, session, settings, frozen_clock
):
    order = make_order(session, settings, sale_id="CCC33333", phone="5551994697674")
    job = session.execute(select(RecoveryJob).where(RecoveryJob.order_id == order.id)).scalar_one()
    assert job.state == JobState.SCHEDULED.value

    nonce = get_nonce(client, "/painel/descadastros")
    r = client.post(
        "/painel/descadastros",
        auth=AUTH,
        data={"nonce": nonce, "phone": "(51) 99469-7674"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    session.expire_all()
    phones = {row.phone for row in session.execute(select(OptOut)).scalars()}
    assert phones == {"5551994697674", "555194697674"}
    job = session.get(RecoveryJob, job.id)
    assert job.state == JobState.CANCELLED.value and job.reason == "opted_out"

    listing = client.get("/painel/descadastros", auth=AUTH)
    assert "5551994697674" in listing.text and "Adicionado no painel" in listing.text


def test_add_opt_out_rejects_an_invalid_number(client, session):
    nonce = get_nonce(client, "/painel/descadastros")
    r = client.post("/painel/descadastros", auth=AUTH, data={"nonce": nonce, "phone": "abc"})
    assert r.status_code == 400
    assert "Número inválido" in r.text
    assert session.execute(select(OptOut)).scalars().first() is None


def test_remove_opt_out(client, session):
    nonce = get_nonce(client, "/painel/descadastros")
    client.post(
        "/painel/descadastros",
        auth=AUTH,
        data={"nonce": nonce, "phone": "5551994697674"},
        follow_redirects=False,
    )
    r = client.post(
        "/painel/descadastros/remover",
        auth=AUTH,
        data={"nonce": nonce, "phone": "5551994697674"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    session.expire_all()
    assert session.execute(select(OptOut)).scalars().first() is None

    missing = client.post(
        "/painel/descadastros/remover",
        auth=AUTH,
        data={"nonce": nonce, "phone": "5551994697674"},
    )
    assert missing.status_code == 404
    assert "Nenhum descadastro encontrado" in missing.text


# --- Eventos ----------------------------------------------------------------------


def test_eventos_lists_raw_payloads_collapsed(client, session, settings, frozen_clock):
    make_order(session, settings, sale_id="DDD44444")
    r = client.get("/painel/eventos", auth=AUTH)
    assert r.status_code == 200
    assert "<details>" in r.text and "ver JSON" in r.text
    assert "PIX_GENERATED" in r.text and "DDD44444" in r.text
    # The parser strips the CPF before storing the raw payload; the panel must
    # therefore never be able to show it.
    assert "12345678900" not in r.text


def test_eventos_filter_by_source(client, session, settings, frozen_clock):
    make_order(session, settings, sale_id="EEE55555")
    assert "EEE55555" in client.get("/painel/eventos?origem=kirvano", auth=AUTH).text
    assert "EEE55555" not in client.get("/painel/eventos?origem=meta", auth=AUTH).text
    # An unknown filter falls back to "all" instead of erroring.
    assert "EEE55555" in client.get("/painel/eventos?origem=lixo", auth=AUTH).text


# --- Modelo -----------------------------------------------------------------------


def test_modelo_lists_known_template_rows(client, session, frozen_clock):
    session.add(
        TemplateStatus(
            name="pix_pendente_v2",
            language="pt_BR",
            status="APPROVED",
            category="UTILITY",
            updated_at=frozen_clock.now,
        )
    )
    session.commit()
    r = client.get("/painel/modelo", auth=AUTH)
    assert r.status_code == 200
    assert "pix_pendente_v2" in r.text and "APPROVED" in r.text and "UTILITY" in r.text
    assert "em uso" in r.text


@respx.mock
def test_modelo_refresh_button_updates_from_the_graph_api(client, session, frozen_clock):
    route = respx.get(TEMPLATES_URL).mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "name": "pix_pendente_v2",
                        "status": "PAUSED",
                        "category": "MARKETING",
                        "language": "pt_BR",
                        "id": "123456",
                    }
                ]
            },
        )
    )
    nonce = get_nonce(client, "/painel/modelo")
    r = client.post("/painel/modelo", auth=AUTH, data={"nonce": nonce}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/painel/modelo?ok=modelo"
    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer meta-test-token"

    session.expire_all()
    row = session.get(TemplateStatus, ("pix_pendente_v2", "pt_BR"))
    assert row.status == "PAUSED" and row.category == "MARKETING" and row.template_id == "123456"


@respx.mock
def test_modelo_refresh_reports_a_missing_template(client, session, frozen_clock):
    respx.get(TEMPLATES_URL).mock(return_value=Response(200, json={"data": []}))
    nonce = get_nonce(client, "/painel/modelo")
    r = client.post("/painel/modelo", auth=AUTH, data={"nonce": nonce})
    assert r.status_code == 502
    assert "não retornou nenhum modelo" in r.text
    session.expire_all()
    assert session.execute(select(TemplateStatus)).scalars().first() is None


def test_panel_meta_helper_without_a_token(session):
    from app.panel_meta import refresh_template_status

    result = refresh_template_status(session, None, "pix_pendente_v2", "pt_BR")
    assert result.ok is False and "META_ACCESS_TOKEN" in result.message


def test_inicio_shows_the_opt_in_evidence(client, session, settings, frozen_clock):
    """README section 11 promises the checkout IP is visible in the panel.

    It was written to the column and rendered nowhere, so the evidence Meta or a
    disputing customer would ask for could only be reached with psql.
    """
    make_order(session, settings, sale_id="CCC33333")
    order = session.execute(select(Order).where(Order.sale_id == "CCC33333")).scalar_one()
    order.consent_ip = "200.152.1.115"
    order.consent_at = DEFAULT_NOW
    session.commit()

    r = client.get("/painel", auth=AUTH)
    assert r.status_code == 200
    assert "IP 200.152.1.115" in r.text
    # Never on a public page.
    public = client.get(f"/p/{order.page_token}")
    assert "200.152.1.115" not in public.text


# --- password rotation route ---------------------------------------------------------


def test_senha_page_requires_auth(client):
    assert client.get("/painel/senha").status_code == 401


def test_senha_page_renders(client):
    r = client.get("/painel/senha", auth=AUTH)
    assert r.status_code == 200
    assert "Senha" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_change_password_rejects_wrong_current(client):
    page = client.get("/painel/senha", auth=AUTH)
    r = client.post(
        "/painel/senha",
        auth=AUTH,
        headers=ORIGIN,
        data={
            "nonce": nonce_from(page.text),
            "senha_atual": "nao-e-a-senha",
            "nova_senha": "uma senha nova 1",
            "confirmar_senha": "uma senha nova 1",
        },
    )
    assert r.status_code == 400
    assert "incorreta" in r.text
    # and the old password still works
    assert client.get("/painel", auth=AUTH).status_code == 200


def test_change_password_rejects_reusing_the_current_one(client):
    page = client.get("/painel/senha", auth=AUTH)
    r = client.post(
        "/painel/senha",
        auth=AUTH,
        headers=ORIGIN,
        data={
            "nonce": nonce_from(page.text),
            "senha_atual": AUTH[1],
            "nova_senha": AUTH[1],
            "confirmar_senha": AUTH[1],
        },
    )
    assert r.status_code == 400
    assert "igual" in r.text


def test_change_password_requires_csrf_nonce(client):
    r = client.post(
        "/painel/senha",
        auth=AUTH,
        headers=ORIGIN,
        data={
            "nonce": "invalido",
            "senha_atual": AUTH[1],
            "nova_senha": "uma senha nova 1",
            "confirmar_senha": "uma senha nova 1",
        },
    )
    assert r.status_code == 403
    assert client.get("/painel", auth=AUTH).status_code == 200


def test_change_password_switches_credentials(client):
    page = client.get("/painel/senha", auth=AUTH)
    r = client.post(
        "/painel/senha",
        auth=AUTH,
        headers=ORIGIN,
        data={
            "nonce": nonce_from(page.text),
            "senha_atual": AUTH[1],
            "nova_senha": "uma senha nova 1",
            "confirmar_senha": "uma senha nova 1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/painel/senha?ok=senha"
    assert client.get("/painel", auth=AUTH).status_code == 401
    assert client.get("/painel", auth=(AUTH[0], "uma senha nova 1")).status_code == 200
