"""The client panel: ``/painel`` and its sub-routes, behind HTTP Basic auth.

Audience: one non-technical operator, often on a phone. Everything the client
changes day to day lives here — the reminder delay, the on/off switch, quiet
hours, opt-outs, the inbox — plus read-only windows into what the system did
(orders, webhook events, template status).

Security model (SPEC "Panel", ARCHITECTURE §7 rule 7):

* **HTTP Basic** against ``PANEL_USER`` / ``PANEL_PASSWORD``, compared with
  :func:`hmac.compare_digest` on *both* fields so neither the user name nor the
  password leaks through timing. No password configured → 503, never an open
  panel.
* **Mutations are POST-only** and pass two independent checks: the request's
  ``Origin``/``Referer`` host must equal ``Host``
  (:func:`app.deps.is_same_origin`), and the form must carry the hidden nonce.
  The nonce is an HMAC of the authenticated user under a secret generated per
  process — there are no cookies or server-side sessions to hang a CSRF token
  on. A cross-origin page cannot read the panel HTML, so it cannot produce the
  value, and the secret dies with the process.
* Every response is ``Cache-Control: no-store``: the panel shows customer phone
  numbers and message bodies.

Transactions: each route owns its own. Reads never commit; mutations mutate and
then ``session.commit()``. The one Graph API call the panel can make (a
free-text reply) happens with no writes pending, so no HTTP request is ever
issued inside an open write transaction (ARCHITECTURE §7 rule 4).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import alerts, clock
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import FRAME_DENY_HEADERS, get_graph_client, is_same_origin
from app.inbound import (
    WindowClosedError,
    find_order_for_contact,
    send_free_text,
    window_closes_at,
    window_open,
)
from app.models import Contact, JobState, OptOutSource, OrderStatus
from app.optout import add_opt_out, cancel_jobs_for_phones, list_opt_outs, remove_opt_out
from app.panel_meta import refresh_template_status
from app.panel_password import (
    PasswordError,
    check_panel_password,
    get_password_hash,
    set_password,
    validate_new_password,
)
from app.panel_queries import all_template_status, conversation_rows
from app.phone import normalize_br
from app.queries import (
    cart_metrics,
    cart_status_label,
    conversation_messages,
    dashboard_counts,
    job_state_label,
    order_status_label,
    post_sale_metrics,
    post_sale_status_label,
    recent_carts,
    recent_events,
    recent_orders,
    recent_post_sales,
    worker_heartbeat_age,
)
from app.settings_store import (
    CART_MAX_STEPS,
    POST_MAX_DELAY_MINUTES,
    POST_MAX_STEPS,
    SettingsStore,
    SettingValueError,
    get_settings_store,
    parse_link_utm,
    parse_product_links,
)
from app.templating import templates
from app.whatsapp import CART_PARAM_KEYS, POST_PARAM_KEYS, TEMPLATE_PARAM_KEYS, GraphClient

log = logging.getLogger("app.panel")

router = APIRouter(prefix="/painel", tags=["painel"])

REALM = 'Basic realm="Painel"'

# Fallback nonce secret for an app that never called `setup()` (a router mounted
# by hand, e.g. in a test). Regenerated on every import, like the per-app one.
_FALLBACK_NONCE_SECRET = secrets.token_bytes(32)


# --- authentication -----------------------------------------------------------------


def _basic_credentials(header: str | None) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic`` header into ``(user, password)``."""
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return user, password


def require_panel_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> str:
    """FastAPI dependency: the HTTP Basic gate. Returns the authenticated user name.

    The password comes from the ``settings`` table when the operator has set one
    in the panel (see :mod:`app.panel_password`); otherwise from the environment.
    A stored password makes the panel serve even when ``PANEL_PASSWORD`` is unset,
    which is why the 503 below also checks the database.
    """
    stored_hash = get_password_hash(session)
    if not settings.panel_password and stored_hash is None:
        # Refusing to serve is the safe failure: an unauthenticated panel would
        # expose customer phone numbers and let anyone switch the reminders off.
        raise HTTPException(
            status_code=503,
            detail=(
                "Painel indisponível: a senha de acesso (PANEL_PASSWORD) não está "
                "configurada no servidor."
            ),
        )
    creds = _basic_credentials(request.headers.get("authorization"))
    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="Autenticação necessária.",
            headers={"WWW-Authenticate": REALM},
        )
    user, password = creds
    # Both comparisons always run (no short-circuit) and both are constant-time,
    # so a wrong user name costs exactly as much as a wrong password.
    user_ok = hmac.compare_digest(user.encode("utf-8"), settings.panel_user.encode("utf-8"))
    pass_ok = check_panel_password(session, password, settings.panel_password)
    if not (user_ok and pass_ok):
        log.warning("panel: failed login attempt for user %r", user[:32])
        raise HTTPException(
            status_code=401,
            detail="Usuário ou senha inválidos.",
            headers={"WWW-Authenticate": REALM},
        )
    return user


# --- CSRF: same-origin + per-process nonce ------------------------------------------


def _nonce_secret(request: Request) -> bytes:
    return getattr(request.app.state, "panel_nonce_secret", _FALLBACK_NONCE_SECRET)


def panel_nonce(request: Request, user: str) -> str:
    """Hidden form token bound to the logged-in user and this process' secret."""
    return hmac.new(_nonce_secret(request), user.encode("utf-8"), hashlib.sha256).hexdigest()


def check_mutation(request: Request, user: str, nonce: str) -> str | None:
    """pt-BR error message when a POST fails a CSRF check, ``None`` when it passes."""
    if not is_same_origin(request):
        return (
            "Pedido bloqueado: a origem da requisição não confere com o endereço do "
            "painel. Abra o painel diretamente e tente de novo."
        )
    if not hmac.compare_digest(nonce or "", panel_nonce(request, user)):
        return (
            "Formulário expirado (o servidor foi reiniciado ou a página ficou aberta "
            "por muito tempo). Recarregue a página e envie de novo."
        )
    return None


# --- labels and badges ---------------------------------------------------------------

# pt-BR wording for every ``recovery_jobs.reason`` the core can write
# (ARCHITECTURE §3) plus the prefixed families handled in `reason_label`.
REASON_LABELS: dict[str, str] = {
    "clamped_to_expiry": "antecipado (PIX expira logo)",
    "quiet_hours": "adiado (horário silencioso)",
    "expires_too_soon": "PIX expiraria antes do envio",
    "disabled": "pausado (lembretes desligados)",
    "paid": "cancelado: pago",
    "expired": "cancelado: PIX expirado",
    "refused": "cancelado: pagamento recusado",
    "refunded": "cancelado: reembolsado",
    "chargeback": "cancelado: chargeback",
    "opted_out": "cliente pediu para não receber",
    "daily_limit": "limite diário de contatos atingido",
    "template_unavailable": "modelo indisponível",
    "template_not_configured": "modelo não configurado",
    "template_paused": "modelo pausado pela Meta",
    "template_disabled": "modelo desativado pela Meta",
    "template_not_found": "modelo não encontrado na Meta",
    "no_phone": "sem telefone válido",
    "not_on_whatsapp": "número não tem WhatsApp",
    "max_attempts": "falhou após 3 tentativas",
    "token_invalid": "token do WhatsApp inválido",
    "stale_sending": "envio interrompido (resultado desconhecido)",
    # Written when the worker hands an untouched claim back (kill switch or shutdown).
    "worker_stopping": "reagendado (o serviço estava reiniciando)",
    "record_failed": "reagendado (falha ao gravar o resultado)",
    "marketing_limit_24h": "limite de marketing por usuário",
    "window_closed": "fora da janela de 24 horas",
    "param_invalid": "parâmetro inválido na mensagem",
    "param_count_mismatch": "quantidade de parâmetros do modelo não confere",
    "param_format_mismatch": "formato de parâmetro do modelo não confere",
    "param_issue": "problema em um parâmetro do modelo",
    "rate_limited": "limite de envios da Meta",
    "pair_rate_limited": "limite de envios para este contato",
    "network_error": "falha de rede ao falar com a Meta",
    "unknown_error": "erro desconhecido",
    "job_cancelled": "lembrete cancelado",
    # abandoned-cart sequence (app.cart)
    "pix_generated": "cliente gerou o PIX",
    "purchased": "cliente comprou",
    "purchased_after": "cliente comprou depois do abandono",
    "pix_flow_active": "PIX em andamento (lembrete do PIX cuida)",
    "pix_reminder_sent": "já recebeu o lembrete do PIX",
    "already_purchased": "já comprou este produto",
    "cart_too_old": "carrinho antigo demais",
    "cart_open": "carrinho em aberto",
    "cart_pix_generated": "cliente gerou o PIX",
    "cart_purchased": "cliente comprou",
    "step_disabled": "mensagem desligada no painel",
    "previous_not_sent": "a mensagem anterior não foi enviada",
    "waiting_previous": "aguardando a mensagem anterior",
    "sequence_exists": "sequência já iniciada",
    # post-sale follow-up (app.postsale); "refunded"/"chargeback" are shared above
    "sale_refunded": "venda reembolsada",
    "sale_chargeback": "chargeback na venda",
    "sale_too_old": "aviso da venda chegou tarde demais",
    "too_late": "o horário da mensagem já tinha passado",
}


def reason_label(reason: str | None) -> str:
    """pt-BR text for a job reason, including the ``order_*`` / ``retry_*`` families."""
    if not reason:
        return ""
    if reason in REASON_LABELS:
        return REASON_LABELS[reason]
    if reason.startswith("order_"):
        # Fire-time re-check found the order no longer pending.
        return f"pedido já {order_status_label(reason.removeprefix('order_'))}"
    if reason.startswith("retry_"):
        return f"nova tentativa (erro {reason.removeprefix('retry_')})"
    if reason.startswith("graph_"):
        return f"erro {reason.removeprefix('graph_')} da Meta"
    return reason.replace("_", " ")


def job_badge(state: str | None) -> str:
    """CSS modifier for a job-state badge (base.html provides ok / warn / bad)."""
    return {
        JobState.SENT.value: "ok",
        JobState.SCHEDULED.value: "warn",
        JobState.SENDING.value: "warn",
        JobState.FAILED.value: "bad",
        JobState.CANCELLED.value: "",
        JobState.SKIPPED.value: "",
    }.get(state or "", "")


def order_badge(status: str | None) -> str:
    return {
        OrderStatus.PAID.value: "ok",
        OrderStatus.PENDING.value: "warn",
        OrderStatus.EXPIRED.value: "bad",
        OrderStatus.REFUSED.value: "bad",
        OrderStatus.REFUNDED.value: "bad",
        OrderStatus.CHARGEBACK.value: "bad",
    }.get(status or "", "")


TEMPLATE_STATUS_BADGE: dict[str, str] = {
    "APPROVED": "ok",
    "PENDING": "warn",
    "IN_APPEAL": "warn",
    "PENDING_DELETION": "warn",
    "PAUSED": "bad",
    "DISABLED": "bad",
    "REJECTED": "bad",
}

# Success messages are looked up by code, so a redirect never reflects arbitrary
# text back into the page.
OK_MESSAGES: dict[str, str] = {
    "config": "Configurações salvas.",
    "resposta": "Mensagem enviada.",
    "descadastro": "Número adicionado aos descadastros.",
    "descadastro_removido": "Número removido dos descadastros.",
    "modelo": "Status do modelo atualizado com a Meta.",
    "senha": "Senha alterada. Use a nova senha no próximo acesso.",
    "carrinho": "Configurações do carrinho salvas.",
    "carrinho_modelos": "Status dos modelos de carrinho atualizado com a Meta.",
    "pos_venda": "Configurações do pós-venda salvas.",
    "pos_venda_modelos": "Status dos modelos de pós-venda atualizado com a Meta.",
}


def pretty_json(value: Any) -> str:
    """Indented UTF-8 JSON for the Eventos page (Jinja autoescapes the result)."""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


# Filters are registered from here rather than by editing app/templating.py,
# which several builders share, so the panel templates can stay declarative.
templates.env.filters.setdefault("pretty_json", pretty_json)
templates.env.filters.setdefault("reason_label", reason_label)
templates.env.filters.setdefault("job_state_label", job_state_label)
templates.env.filters.setdefault("order_status_label", order_status_label)
templates.env.filters.setdefault("job_badge", job_badge)
templates.env.filters.setdefault("order_badge", order_badge)
templates.env.filters.setdefault("cart_status_label", cart_status_label)
templates.env.filters.setdefault("post_sale_status_label", post_sale_status_label)


# --- rendering helpers ---------------------------------------------------------------


def _render(
    request: Request,
    template: str,
    user: str,
    *,
    active: str,
    title: str,
    status_code: int = 200,
    flash: str | None = None,
    flash_kind: str = "ok",
    **context: Any,
) -> Response:
    ok_code = request.query_params.get("ok")
    if flash is None and ok_code in OK_MESSAGES:
        flash = OK_MESSAGES[ok_code]
    response = templates.TemplateResponse(
        request,
        template,
        {
            "active": active,
            "page_title": title,
            "nonce": panel_nonce(request, user),
            "flash": flash,
            "flash_kind": flash_kind,
            **context,
        },
        status_code=status_code,
    )
    # Operator data (phones, message bodies) must not sit in any shared cache.
    response.headers["Cache-Control"] = "no-store"
    # Never framable: see app.deps.FRAME_DENY_HEADERS — framing defeats both CSRF checks.
    response.headers.update(FRAME_DENY_HEADERS)
    return response


def _error_page(request: Request, user: str, message: str, *, status_code: int) -> Response:
    return _render(
        request,
        "panel/erro.html",
        user,
        active="",
        title="Não foi possível continuar",
        status_code=status_code,
        flash=message,
        flash_kind="bad",
    )


def _redirect(path: str, code: str) -> RedirectResponse:
    """Post/Redirect/Get, so a browser refresh never repeats a mutation."""
    response = RedirectResponse(url=f"{path}?ok={code}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers.update(FRAME_DENY_HEADERS)
    return response


# --- Início --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def inicio(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
) -> Response:
    now = clock.utcnow()
    heartbeat = worker_heartbeat_age(session, now=now)
    return _render(
        request,
        "panel/inicio.html",
        user,
        active="inicio",
        title="Início",
        counts=dashboard_counts(session, now=now),
        orders=recent_orders(session, limit=50),
        open_alerts=alerts.open_alerts(session, limit=20),
        enabled=store.enabled,
        delay_minutes=store.delay_minutes,
        heartbeat_age=heartbeat,
        worker_ok=heartbeat is not None and heartbeat < 60,
        now=now,
    )


# --- Configurações -------------------------------------------------------------------

# Panel-level validation, stricter than the store's (SPEC "Panel" bullet): a
# delay of 0 would fire before the customer can even read the checkout page, and
# a daily limit of 0 would silently disable every send.
PANEL_INT_RANGES: dict[str, tuple[int, int]] = {
    "delay_minutes": (1, 1440),
    "daily_recipient_limit": (1, 100_000),
    "url_button_index": (0, 9),
}
TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]+$")  # Meta's own template-name charset
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # HH:MM, 24 h


def validate_setting(key: str, value: str) -> str | None:
    """pt-BR error message for a submitted settings field, ``None`` when valid."""
    if key in PANEL_INT_RANGES:
        low, high = PANEL_INT_RANGES[key]
        try:
            number = int(value.strip())
        except ValueError:
            return "Informe um número inteiro."
        # -1 is the documented "this template has no URL button" sentinel
        # (ARCHITECTURE §6, TEMPLATE_URL_BUTTON_INDEX). Keeping it accepted is
        # what lets the client switch to a fallback template from the panel
        # instead of a deploy, as the SPEC's Template section requires.
        if key == "url_button_index" and number == -1:
            return None
        if number < low or number > high:
            return f"Use um número entre {low} e {high}."
        return None
    if key in ("quiet_start", "quiet_end"):
        return None if TIME_RE.match(value.strip()) else "Use o formato HH:MM, por exemplo 22:00."
    if key == "template_name":
        return (
            None
            if TEMPLATE_NAME_RE.match(value.strip())
            else "Use apenas letras minúsculas, números e _ (igual ao nome na Meta)."
        )
    if key == "template_params":
        # An unknown key used to render as an EMPTY parameter, and Meta then rejected
        # every reminder with #132000/#132012 — a single typo here (firstname for
        # first_name) silently killed the whole flow. Reject it at the form instead.
        keys = [p.strip() for p in value.split(",") if p.strip()]
        if not keys:
            return "Informe pelo menos um parâmetro."
        unknown = [k for k in keys if k not in TEMPLATE_PARAM_KEYS]
        if unknown:
            return (
                f"Parâmetro desconhecido: {', '.join(unknown)}. "
                f"Use apenas: {', '.join(sorted(TEMPLATE_PARAM_KEYS))}."
            )
        return None
    return None


def _config_context(store: SettingsStore, values: dict[str, str]) -> dict[str, Any]:
    return {
        # The cart and post-sale keys live on their own pages.
        "definitions": SettingsStore.group_definitions("pix"),
        "values": values,
        "ranges": PANEL_INT_RANGES,
        "store": store,
    }


@router.get("/configuracoes", response_class=HTMLResponse)
def configuracoes(
    request: Request,
    user: str = Depends(require_panel_auth),
    store: SettingsStore = Depends(get_settings_store),
) -> Response:
    return _render(
        request,
        "panel/configuracoes.html",
        user,
        active="configuracoes",
        title="Configurações",
        errors={},
        **_config_context(store, store.as_dict()),
    )


@router.post("/configuracoes", response_class=HTMLResponse)
def salvar_configuracoes(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    nonce: str = Form(""),
    enabled: str | None = Form(None),
    delay_minutes: str = Form(""),
    quiet_start: str = Form(""),
    quiet_end: str = Form(""),
    daily_recipient_limit: str = Form(""),
    template_name: str = Form(""),
    template_language: str = Form(""),
    url_button_index: str = Form(""),
    template_params: str = Form(""),
    checkout_url: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)

    submitted: dict[str, str] = {
        # An unchecked checkbox is simply absent from the form body.
        "enabled": "true" if enabled is not None else "false",
        "delay_minutes": delay_minutes.strip(),
        "quiet_start": quiet_start.strip(),
        "quiet_end": quiet_end.strip(),
        "daily_recipient_limit": daily_recipient_limit.strip(),
        "template_name": template_name.strip(),
        "template_language": template_language.strip(),
        "url_button_index": url_button_index.strip(),
        "template_params": template_params.strip(),
        "checkout_url": checkout_url.strip(),
    }
    errors = {k: msg for k, v in submitted.items() if (msg := validate_setting(k, v))}
    if not errors:
        try:
            # set_many validates every value before writing any of them, so one
            # bad field can never leave the settings half-updated.
            store.set_many(submitted)
        except SettingValueError as exc:
            errors["__all__"] = str(exc)
    if errors:
        session.rollback()
        return _render(
            request,
            "panel/configuracoes.html",
            user,
            active="configuracoes",
            title="Configurações",
            status_code=400,
            flash="Corrija os campos destacados: nada foi salvo.",
            flash_kind="bad",
            errors=errors,
            **_config_context(store, submitted),
        )
    session.commit()
    log.info("panel: settings updated by %s", user)
    return _redirect("/painel/configuracoes", "config")


# --- Senha ---------------------------------------------------------------------------


@router.get("/senha", response_class=HTMLResponse)
def senha(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
) -> Response:
    return _render(
        request,
        "panel/senha.html",
        user,
        active="senha",
        title="Senha",
        error=None,
        using_stored=get_password_hash(session) is not None,
    )


@router.post("/senha", response_class=HTMLResponse)
def salvar_senha(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    nonce: str = Form(""),
    senha_atual: str = Form(""),
    nova_senha: str = Form(""),
    confirmar_senha: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)

    def fail(message: str) -> Response:
        session.rollback()
        return _render(
            request,
            "panel/senha.html",
            user,
            active="senha",
            title="Senha",
            status_code=400,
            error=message,
            using_stored=get_password_hash(session) is not None,
        )

    # Re-check the current password even though Basic auth already passed: the
    # browser replays those credentials on every request, so without this a
    # borrowed unlocked screen would be enough to take the panel over.
    if not check_panel_password(session, senha_atual, settings.panel_password):
        log.warning("panel: wrong current password on change attempt by %s", user)
        return fail("A senha atual está incorreta.")
    # Checked before the strength rules on purpose: telling someone who retyped
    # their current password that it needs 10 characters is confusing when the
    # real problem is that nothing would change.
    if nova_senha and check_panel_password(session, nova_senha, settings.panel_password):
        return fail("A nova senha é igual à atual. Escolha outra.")
    try:
        validate_new_password(nova_senha, confirmar_senha)
    except PasswordError as exc:
        return fail(str(exc))

    set_password(session, nova_senha)
    session.commit()
    log.info("panel: password changed by %s", user)
    return _redirect("/painel/senha", "senha")


# --- Conversas -----------------------------------------------------------------------


@router.get("/conversas", response_class=HTMLResponse)
def conversas(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
) -> Response:
    now = clock.utcnow()
    return _render(
        request,
        "panel/conversas.html",
        user,
        active="conversas",
        title="Conversas",
        rows=conversation_rows(session, limit=100, now=now),
        now=now,
    )


def _conversation_page(
    request: Request,
    user: str,
    session: Session,
    contact: Contact,
    client: GraphClient | None,
    *,
    status_code: int = 200,
    flash: str | None = None,
    flash_kind: str = "ok",
    draft: str = "",
) -> Response:
    now = clock.utcnow()
    return _render(
        request,
        "panel/conversa.html",
        user,
        active="conversas",
        title=contact.profile_name or contact.phone or contact.wa_id,
        status_code=status_code,
        flash=flash,
        flash_kind=flash_kind,
        contact=contact,
        messages=conversation_messages(session, contact.wa_id, limit=200),
        janela_aberta=window_open(contact, now),
        janela_fecha_em=window_closes_at(contact),
        order=find_order_for_contact(session, contact.wa_id),
        meta_configured=client is not None and client.configured,
        draft=draft,
        now=now,
    )


@router.get("/conversas/{wa_id}", response_class=HTMLResponse)
def conversa(
    wa_id: str,
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    client: GraphClient | None = Depends(get_graph_client),
) -> Response:
    contact = session.get(Contact, wa_id)
    if contact is None:
        return _error_page(request, user, "Conversa não encontrada.", status_code=404)
    return _conversation_page(request, user, session, contact, client)


@router.post("/conversas/{wa_id}", response_class=HTMLResponse)
def responder(
    wa_id: str,
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    client: GraphClient | None = Depends(get_graph_client),
    nonce: str = Form(""),
    body: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)
    contact = session.get(Contact, wa_id)
    if contact is None:
        return _error_page(request, user, "Conversa não encontrada.", status_code=404)

    text = body.strip()
    if not text:
        return _conversation_page(
            request,
            user,
            session,
            contact,
            client,
            status_code=400,
            flash="Escreva uma mensagem antes de enviar.",
            flash_kind="bad",
        )
    if client is None or not client.configured:
        return _conversation_page(
            request,
            user,
            session,
            contact,
            client,
            status_code=400,
            flash=(
                "O token do WhatsApp (META_ACCESS_TOKEN) não está configurado no "
                "servidor, então não é possível responder por aqui."
            ),
            flash_kind="bad",
            draft=text,
        )

    try:
        # send_free_text refuses (WindowClosedError) outside the 24 h service
        # window — Meta would answer 131047 anyway, and failing locally keeps the
        # error rate, and therefore the number's quality rating, clean.
        result = send_free_text(session, client, contact.wa_id, text)
    except WindowClosedError as exc:
        session.rollback()
        return _conversation_page(
            request,
            user,
            session,
            contact,
            client,
            status_code=400,
            flash=str(exc),
            flash_kind="bad",
            draft=text,
        )
    if not result.ok:
        session.rollback()
        detail = result.error.summary() if result.error else "erro desconhecido"
        return _conversation_page(
            request,
            user,
            session,
            contact,
            client,
            status_code=502,
            flash=f"A Meta recusou o envio: {detail}",
            flash_kind="bad",
            draft=text,
        )
    # send_free_text wrote the outbound Message row; the route owns the commit.
    session.commit()
    # quote(): the wa_id comes from Meta (digits in practice) but it ends up in a
    # Location header, so it is escaped rather than trusted.
    return _redirect(f"/painel/conversas/{quote(contact.wa_id)}", "resposta")


# --- Descadastros --------------------------------------------------------------------


def _descadastros_page(
    request: Request,
    user: str,
    session: Session,
    *,
    flash: str | None = None,
    flash_kind: str = "ok",
    status_code: int = 200,
) -> Response:
    return _render(
        request,
        "panel/descadastros.html",
        user,
        active="descadastros",
        title="Descadastros",
        status_code=status_code,
        flash=flash,
        flash_kind=flash_kind,
        rows=list_opt_outs(session, limit=500),
    )


@router.get("/descadastros", response_class=HTMLResponse)
def descadastros(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
) -> Response:
    return _descadastros_page(request, user, session)


@router.post("/descadastros", response_class=HTMLResponse)
def adicionar_descadastro(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    nonce: str = Form(""),
    phone: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)
    forms = normalize_br(phone)
    if forms is None:
        return _descadastros_page(
            request,
            user,
            session,
            flash="Número inválido. Informe com DDD, por exemplo 51994697674.",
            flash_kind="bad",
            status_code=400,
        )
    now = clock.utcnow()
    # add_opt_out writes one row per variant (13- and 12-digit BR forms), so a
    # later order arriving in either form is matched.
    created = add_opt_out(
        session,
        phone=forms.primary,
        wa_id=None,
        source=OptOutSource.MANUAL.value,
        note="Adicionado no painel",
        now=now,
    )
    # A manual opt-out must also stop whatever is already scheduled for it.
    cancelled = cancel_jobs_for_phones(session, forms.variants, None, now=now)
    session.commit()
    log.info(
        "panel: manual opt-out by %s (%d rows, %d jobs cancelled)",
        user,
        len(created),
        len(cancelled),
    )
    return _redirect("/painel/descadastros", "descadastro")


@router.post("/descadastros/remover", response_class=HTMLResponse)
def remover_descadastro(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    nonce: str = Form(""),
    phone: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)
    removed = remove_opt_out(session, phone.strip())
    session.commit()
    if not removed:
        return _descadastros_page(
            request,
            user,
            session,
            flash="Nenhum descadastro encontrado para esse número.",
            flash_kind="bad",
            status_code=404,
        )
    log.info("panel: opt-out removed by %s (%d rows)", user, removed)
    return _redirect("/painel/descadastros", "descadastro_removido")


# --- Eventos -------------------------------------------------------------------------


@router.get("/eventos", response_class=HTMLResponse)
def eventos(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
) -> Response:
    source = request.query_params.get("origem")
    if source not in ("kirvano", "meta"):
        source = None
    return _render(
        request,
        "panel/eventos.html",
        user,
        active="eventos",
        title="Eventos",
        events=recent_events(session, limit=100, source=source),
        source=source,
    )


# --- Modelo --------------------------------------------------------------------------


def _modelo_page(
    request: Request,
    user: str,
    session: Session,
    store: SettingsStore,
    client: GraphClient | None,
    *,
    flash: str | None = None,
    flash_kind: str = "ok",
    status_code: int = 200,
) -> Response:
    return _render(
        request,
        "panel/modelo.html",
        user,
        active="modelo",
        title="Modelo",
        status_code=status_code,
        flash=flash,
        flash_kind=flash_kind,
        rows=all_template_status(session),
        store=store,
        badges=TEMPLATE_STATUS_BADGE,
        meta_configured=client is not None and client.configured,
    )


@router.get("/modelo", response_class=HTMLResponse)
def modelo(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
) -> Response:
    return _modelo_page(request, user, session, store, client)


@router.post("/modelo", response_class=HTMLResponse)
def atualizar_modelo(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
    nonce: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)
    result = refresh_template_status(session, client, store.template_name, store.template_language)
    if not result.ok:
        session.rollback()
        return _modelo_page(
            request,
            user,
            session,
            store,
            client,
            flash=result.message,
            flash_kind="bad",
            status_code=502,
        )
    session.commit()
    return _redirect("/painel/modelo", "modelo")


# --- Carrinho (abandoned-cart recovery) ----------------------------------------------

CART_PERIODS: dict[str, tuple[str, int | None]] = {
    "7": ("últimos 7 dias", 7),
    "30": ("últimos 30 dias", 30),
    "tudo": ("desde o início", None),
}
COUPON_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
# Never two messages of one sequence closer than this (app.cart.MIN_STEP_GAP).
MIN_STEP_GAP_MINUTES = 60


async def submitted_form(request: Request) -> dict[str, str]:
    """Async dependency: the urlencoded form as a plain dict (the Carrinho form has
    one field per step, so naming each in the signature would be noise)."""
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def _cart_keys() -> list[str]:
    return [d.key for d in SettingsStore.group_definitions("cart")]


def validate_cart_settings(values: dict[str, str]) -> dict[str, str]:
    """pt-BR errors keyed by field (``__all__`` for cross-field problems); empty = valid."""
    errors: dict[str, str] = {}

    def as_int(key: str, low: int, high: int) -> int | None:
        try:
            number = int(values.get(key, "").strip())
        except ValueError:
            errors[key] = "Informe um número inteiro."
            return None
        if not low <= number <= high:
            errors[key] = f"Use um número entre {low} e {high}."
            return None
        return number

    steps = as_int("cart_steps", 1, CART_MAX_STEPS) or 1
    if not values.get("cart_template_language", "").strip():
        errors["cart_template_language"] = "Informe o idioma, por exemplo pt_BR."
    fallback = values.get("cart_checkout_url", "").strip()
    if fallback and not URL_RE.match(fallback):
        errors["cart_checkout_url"] = "Use um link completo, começando com https://."
    coupon = values.get("cart_coupon", "").strip()
    if coupon and not COUPON_RE.match(coupon):
        errors["cart_coupon"] = "Use só letras, números, - e _ (até 40), igual ao cupom na Kirvano."
    try:
        parse_link_utm(values.get("cart_link_utm", ""))
    except SettingValueError as exc:
        errors["cart_link_utm"] = str(exc)
    product_links: list[tuple[str, str]] = []
    try:
        product_links = parse_product_links(values.get("cart_product_links", ""))
    except SettingValueError as exc:
        errors["cart_product_links"] = str(exc)

    uses_coupon = False
    needs_link = False
    previous_delay: int | None = None
    for i in range(1, CART_MAX_STEPS + 1):
        enabled = i <= steps
        delay = as_int(f"cart_step{i}_delay_minutes", 5, 3 * 24 * 60)
        name = values.get(f"cart_step{i}_template", "").strip()
        if enabled and not name:
            errors[f"cart_step{i}_template"] = "Informe o nome do modelo desta mensagem."
        elif name and not TEMPLATE_NAME_RE.match(name):
            errors[f"cart_step{i}_template"] = (
                "Use apenas letras minúsculas, números e _ (igual ao nome na Meta)."
            )
        button = as_int(f"cart_step{i}_url_button_index", -1, 9)
        keys = [k.strip() for k in values.get(f"cart_step{i}_params", "").split(",") if k.strip()]
        unknown = [k for k in keys if k not in CART_PARAM_KEYS]
        if unknown:
            errors[f"cart_step{i}_params"] = (
                f"Parâmetro desconhecido: {', '.join(unknown)}. "
                f"Use apenas: {', '.join(sorted(CART_PARAM_KEYS))}."
            )
        if not enabled:
            continue
        uses_coupon = uses_coupon or "coupon" in keys
        needs_link = needs_link or "link" in keys or (button is not None and button >= 0)
        if delay is not None and previous_delay is not None:
            if delay < previous_delay + MIN_STEP_GAP_MINUTES:
                errors[f"cart_step{i}_delay_minutes"] = (
                    f"Precisa ser pelo menos {MIN_STEP_GAP_MINUTES} minutos depois da "
                    f"mensagem {i - 1}."
                )
        previous_delay = delay if delay is not None else previous_delay

    if uses_coupon and not coupon:
        errors["cart_coupon"] = "Um dos modelos usa o parâmetro coupon: informe o cupom."
    enabled_flag = values.get("cart_enabled", "false") == "true"
    if enabled_flag and needs_link and not fallback and not product_links:
        # The cart's own Kirvano link is used when the event carries one, but nothing
        # guarantees it does — without a fallback the button could lead nowhere.
        errors["cart_checkout_url"] = (
            "Informe o link do checkout: é para onde o botão leva quando a Kirvano não "
            "envia o link do carrinho."
        )
    return errors


def _carrinho_page(
    request: Request,
    user: str,
    session: Session,
    store: SettingsStore,
    client: GraphClient | None,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    flash: str | None = None,
    flash_kind: str = "ok",
    status_code: int = 200,
) -> Response:
    now = clock.utcnow()
    period_key = request.query_params.get("periodo", "30")
    if period_key not in CART_PERIODS:
        period_key = "30"
    period_label, days = CART_PERIODS[period_key]
    since = now - timedelta(days=days) if days else None
    steps = store.cart_step_configs()
    template_rows = {(row.name, row.language): row for row in all_template_status(session)}
    heartbeat = worker_heartbeat_age(session, now=now)
    return _render(
        request,
        "panel/carrinho.html",
        user,
        active="carrinho",
        title="Carrinho abandonado",
        status_code=status_code,
        flash=flash,
        flash_kind=flash_kind,
        metrics=cart_metrics(session, since=since, now=now),
        period_key=period_key,
        period_label=period_label,
        periods=CART_PERIODS,
        steps=steps,
        template_rows=template_rows,
        badges=TEMPLATE_STATUS_BADGE,
        carts=recent_carts(session, limit=50),
        definitions=SettingsStore.group_definitions("cart"),
        values=values if values is not None else {k: store.get(k) for k in _cart_keys()},
        errors=errors or {},
        store=store,
        meta_configured=client is not None and client.configured,
        worker_ok=heartbeat is not None and heartbeat < 60,
        max_steps=CART_MAX_STEPS,
        # The public domain (PUBLIC_BASE_URL / API_DOMAIN), whatever host the panel is
        # being browsed from: this is what goes into the template in WhatsApp Manager.
        button_url=f"{store.settings.base_url}/c/{{{{1}}}}",
        now=now,
    )


@router.get("/carrinho", response_class=HTMLResponse)
def carrinho(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
) -> Response:
    return _carrinho_page(request, user, session, store, client)


@router.post("/carrinho", response_class=HTMLResponse)
def salvar_carrinho(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
    form: dict[str, str] = Depends(submitted_form),
) -> Response:
    error = check_mutation(request, user, form.get("nonce", ""))
    if error:
        return _error_page(request, user, error, status_code=403)
    submitted = {k: form.get(k, "").strip() for k in _cart_keys()}
    # An unchecked checkbox is simply absent from the form body.
    submitted["cart_enabled"] = "true" if "cart_enabled" in form else "false"
    errors = validate_cart_settings(submitted)
    if not errors:
        try:
            store.set_many(submitted)
        except SettingValueError as exc:
            errors["__all__"] = str(exc)
    if errors:
        session.rollback()
        return _carrinho_page(
            request,
            user,
            session,
            store,
            client,
            values=submitted,
            errors=errors,
            flash="Corrija os campos destacados: nada foi salvo.",
            flash_kind="bad",
            status_code=400,
        )
    session.commit()
    log.info("panel: cart settings updated by %s", user)
    return _redirect("/painel/carrinho", "carrinho")


@router.post("/carrinho/modelos", response_class=HTMLResponse)
def atualizar_modelos_carrinho(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
    nonce: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)
    names = sorted({s.template_name for s in store.cart_step_configs() if s.template_name})
    problems = []
    for name in names:
        result = refresh_template_status(session, client, name, store.cart_template_language)
        if not result.ok:
            problems.append(result.message)
    # Keep what DID refresh even when another template failed.
    session.commit()
    if problems:
        return _carrinho_page(
            request,
            user,
            session,
            store,
            client,
            flash=" ".join(problems),
            flash_kind="bad",
            status_code=502,
        )
    return _redirect("/painel/carrinho", "carrinho_modelos")


# --- Pós-venda (post-sale follow-up) -------------------------------------------------


def _post_keys() -> list[str]:
    return [d.key for d in SettingsStore.group_definitions("post")]


def validate_post_settings(values: dict[str, str]) -> dict[str, str]:
    """pt-BR errors keyed by field; empty = valid. Same rules as the Carrinho form."""
    errors: dict[str, str] = {}

    def as_int(key: str, low: int, high: int) -> int | None:
        try:
            number = int(values.get(key, "").strip())
        except ValueError:
            errors[key] = "Informe um número inteiro."
            return None
        if not low <= number <= high:
            errors[key] = f"Use um número entre {low} e {high}."
            return None
        return number

    steps = as_int("post_steps", 1, POST_MAX_STEPS) or 1
    if not values.get("post_template_language", "").strip():
        errors["post_template_language"] = "Informe o idioma, por exemplo pt_BR."
    previous_delay: int | None = None
    for i in range(1, POST_MAX_STEPS + 1):
        enabled = i <= steps
        delay = as_int(f"post_step{i}_delay_minutes", 0, POST_MAX_DELAY_MINUTES)
        name = values.get(f"post_step{i}_template", "").strip()
        if enabled and not name:
            errors[f"post_step{i}_template"] = "Informe o nome do modelo desta mensagem."
        elif name and not TEMPLATE_NAME_RE.match(name):
            errors[f"post_step{i}_template"] = (
                "Use apenas letras minúsculas, números e _ (igual ao nome na Meta)."
            )
        keys = [k.strip() for k in values.get(f"post_step{i}_params", "").split(",") if k.strip()]
        unknown = [k for k in keys if k not in POST_PARAM_KEYS]
        if unknown:
            errors[f"post_step{i}_params"] = (
                f"Parâmetro desconhecido: {', '.join(unknown)}. "
                f"Use apenas: {', '.join(sorted(POST_PARAM_KEYS))}."
            )
        if not enabled:
            continue
        if delay is not None and previous_delay is not None:
            if delay < previous_delay + MIN_STEP_GAP_MINUTES:
                errors[f"post_step{i}_delay_minutes"] = (
                    f"Precisa ser pelo menos {MIN_STEP_GAP_MINUTES} minutos depois da "
                    f"mensagem {i - 1}."
                )
        previous_delay = delay if delay is not None else previous_delay
    return errors


def _pos_venda_page(
    request: Request,
    user: str,
    session: Session,
    store: SettingsStore,
    client: GraphClient | None,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    flash: str | None = None,
    flash_kind: str = "ok",
    status_code: int = 200,
) -> Response:
    now = clock.utcnow()
    period_key = request.query_params.get("periodo", "30")
    if period_key not in CART_PERIODS:
        period_key = "30"
    period_label, days = CART_PERIODS[period_key]
    since = now - timedelta(days=days) if days else None
    template_rows = {(row.name, row.language): row for row in all_template_status(session)}
    heartbeat = worker_heartbeat_age(session, now=now)
    return _render(
        request,
        "panel/pos_venda.html",
        user,
        active="pos_venda",
        title="Pós-venda",
        status_code=status_code,
        flash=flash,
        flash_kind=flash_kind,
        metrics=post_sale_metrics(session, since=since, now=now),
        period_key=period_key,
        period_label=period_label,
        periods=CART_PERIODS,
        steps=store.post_step_configs(),
        template_rows=template_rows,
        badges=TEMPLATE_STATUS_BADGE,
        sales=recent_post_sales(session, limit=50),
        definitions=SettingsStore.group_definitions("post"),
        values=values if values is not None else {k: store.get(k) for k in _post_keys()},
        errors=errors or {},
        store=store,
        meta_configured=client is not None and client.configured,
        worker_ok=heartbeat is not None and heartbeat < 60,
        max_steps=POST_MAX_STEPS,
        now=now,
    )


@router.get("/pos-venda", response_class=HTMLResponse)
def pos_venda(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
) -> Response:
    return _pos_venda_page(request, user, session, store, client)


@router.post("/pos-venda", response_class=HTMLResponse)
def salvar_pos_venda(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
    form: dict[str, str] = Depends(submitted_form),
) -> Response:
    error = check_mutation(request, user, form.get("nonce", ""))
    if error:
        return _error_page(request, user, error, status_code=403)
    submitted = {k: form.get(k, "").strip() for k in _post_keys()}
    # An unchecked checkbox is simply absent from the form body.
    submitted["post_enabled"] = "true" if "post_enabled" in form else "false"
    errors = validate_post_settings(submitted)
    if not errors:
        try:
            store.set_many(submitted)
        except SettingValueError as exc:
            errors["__all__"] = str(exc)
    if errors:
        session.rollback()
        return _pos_venda_page(
            request,
            user,
            session,
            store,
            client,
            values=submitted,
            errors=errors,
            flash="Corrija os campos destacados: nada foi salvo.",
            flash_kind="bad",
            status_code=400,
        )
    session.commit()
    log.info("panel: post-sale settings updated by %s", user)
    return _redirect("/painel/pos-venda", "pos_venda")


@router.post("/pos-venda/modelos", response_class=HTMLResponse)
def atualizar_modelos_pos_venda(
    request: Request,
    user: str = Depends(require_panel_auth),
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
    client: GraphClient | None = Depends(get_graph_client),
    nonce: str = Form(""),
) -> Response:
    error = check_mutation(request, user, nonce)
    if error:
        return _error_page(request, user, error, status_code=403)
    names = sorted({s.template_name for s in store.post_step_configs() if s.template_name})
    problems = []
    for name in names:
        result = refresh_template_status(session, client, name, store.post_template_language)
        if not result.ok:
            problems.append(result.message)
    # Keep what DID refresh even when another template failed.
    session.commit()
    if problems:
        return _pos_venda_page(
            request,
            user,
            session,
            store,
            client,
            flash=" ".join(problems),
            flash_kind="bad",
            status_code=502,
        )
    return _redirect("/painel/pos-venda", "pos_venda_modelos")


# --- mount hook ----------------------------------------------------------------------


def setup(app: FastAPI) -> None:
    """Called by ``app.main.include_extra_routers`` right after the router is mounted.

    Gives this app instance its own CSRF nonce secret. It is deliberately not
    persisted: restarting the API invalidates every open form, which is the
    right trade-off for a panel that sees a handful of POSTs a day.
    """
    app.state.panel_nonce_secret = secrets.token_bytes(32)
