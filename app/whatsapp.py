"""WhatsApp Cloud API (Graph) client, payload builder, sanitiser, error mapping
and webhook payload parsing.

Nothing in this module touches the database; it is pure HTTP + data shaping so
it can be unit-tested with ``respx`` and reused by scripts.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings, get_settings
from app.format import first_name, fmt_brl, fmt_dt_sp
from app.models import Cart, Order, PostSale
from app.settings_store import CartStepConfig, SettingsStore, StepConfig

log = logging.getLogger(__name__)

# --- parameter sanitising --------------------------------------------------------------

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NAME_MAX_LEN = 60
PARAM_MAX_LEN = 120

# Every key `build_template_params` knows how to render. The panel and the settings
# store validate `template_params` against this set, because an unknown key used to
# render as an EMPTY parameter and Meta answers #132000/#132012 — "code bug, fail,
# alert" per SPEC line 66 — for every single reminder until someone noticed.
TEMPLATE_PARAM_KEYS = frozenset(
    {
        "first_name",
        "sale_id",
        "amount",
        "amount_full",
        "expiry",
        "product",
        "customer_name",
        "page_url",
    }
)

# SPEC line 62 / docs/TEMPLATE.md §6: the RENDERED body (static text + values) must
# stay under 1024 characters. We cannot see the static text (it lives in WhatsApp
# Manager), so we reserve a generous budget for it and trim the longest values.
RENDERED_BODY_MAX = 1024
BODY_TEXT_BUDGET = 220  # static text of pix_pendente_v2, rounded up
PARAM_TOTAL_BUDGET = RENDERED_BODY_MAX - BODY_TEXT_BUDGET


def sanitize_param(value: object, max_len: int = NAME_MAX_LEN, *, ascii_only: bool = False) -> str:
    """Make a value safe for a template parameter.

    Meta rejects newline, tab and 4+ consecutive spaces (#131009); we collapse all
    whitespace to one space, drop control characters, trim and cap the length.
    ``ascii_only`` is the "sanitize harder" mode used on a 131009/132012/132018 retry.
    """
    s = "" if value is None else str(value)
    s = _WS.sub(" ", s)
    s = _CTRL.sub("", s)
    if ascii_only:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
        s = re.sub(r"[^A-Za-z0-9 .,:/@_()-]", "", s)
    s = s.strip()
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


# --- template payload ------------------------------------------------------------------


def expiry_param(order: Order, settings: Settings) -> str:
    """``dd/mm às HH:MM`` in São Paulo; falls back to the configured checkout expiry or "hoje"."""
    if order.pix_expires_at is not None:
        return fmt_dt_sp(order.pix_expires_at)
    if settings.kirvano_pix_expiry_minutes and order.created_at is not None:
        return fmt_dt_sp(order.created_at + timedelta(minutes=settings.kirvano_pix_expiry_minutes))
    return "hoje"


def build_template_params(
    order: Order,
    store: SettingsStore,
    *,
    hard: bool = False,
    settings: Settings | None = None,
) -> list[str]:
    """Body parameters in the order configured by ``template_params``.

    Known keys: :data:`TEMPLATE_PARAM_KEYS`. An unknown key still renders as an empty
    string rather than raising (a send must never crash), but the panel and the
    settings store reject unknown keys up front so that cannot reach production.

    The values are then trimmed to fit :data:`PARAM_TOTAL_BUDGET`, because a long
    ``product`` or ``page_url`` can otherwise push the rendered body past Meta's
    1024-character limit and every reminder fails with a #132000-class error.
    """
    settings = settings or get_settings()
    name_len = 30 if hard else NAME_MAX_LEN
    values: dict[str, str] = {
        "first_name": sanitize_param(first_name(order.customer_name), name_len, ascii_only=hard)
        or "cliente",
        "sale_id": sanitize_param(order.sale_id, 64, ascii_only=hard),
        "amount": fmt_brl(order.amount_cents),
        "amount_full": fmt_brl(order.amount_cents, symbol=True),
        "expiry": expiry_param(order, settings),
        "product": sanitize_param(order.product_name or "", PARAM_MAX_LEN, ascii_only=hard)
        or "produto",
        "customer_name": sanitize_param(order.customer_name or "", name_len, ascii_only=hard)
        or "cliente",
        "page_url": settings.page_url(order.page_token),
    }
    return fit_body_budget([values.get(key, "") for key in store.template_params])


def fit_body_budget(params: list[str], budget: int = PARAM_TOTAL_BUDGET) -> list[str]:
    """Shorten the longest parameters until they fit ``budget`` characters in total.

    Trimming the longest value first keeps the short, meaning-carrying parameters
    (name, order code, amount) intact; only a runaway ``product`` or ``page_url``
    loses characters. Values of 8 characters or fewer are never touched — an ellipsis
    on a 6-character order code would be worse than the risk we are avoiding.
    """
    out = list(params)
    while sum(len(p) for p in out) > budget:
        longest = max(range(len(out)), key=lambda i: len(out[i]))
        if len(out[longest]) <= 8:
            break  # nothing left worth cutting
        out[longest] = out[longest][: len(out[longest]) - 8].rstrip()
        log.warning("template params trimmed to fit the %d-char body cap", RENDERED_BODY_MAX)
    return out


def build_template_payload(
    order: Order,
    store: SettingsStore,
    *,
    to: str,
    hard: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """The exact JSON body for ``POST /{PHONE_NUMBER_ID}/messages`` (spec example)."""
    settings = settings or get_settings()
    params = build_template_params(order, store, hard=hard, settings=settings)
    components: list[dict[str, Any]] = []
    if params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
        )
    # A template with no variables takes NO body component at all: sending
    # `"parameters": []` is what Meta answers #132000 to.
    idx = store.url_button_index
    if idx >= 0:
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": str(idx),
                "parameters": [{"type": "text", "text": order.page_token}],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": store.template_name,
            "language": {"code": store.template_language},
            "components": components,
        },
    }


# --- abandoned-cart templates ------------------------------------------------------------

# Keys a cart template may use. No sale_id/expiry: an abandoned cart has neither.
# `coupon` is the panel's Cupom field, so the client can change the code without
# re-approving the template; `link` is the full /c/ URL for a template with no button.
CART_PARAM_KEYS = frozenset(
    {"first_name", "customer_name", "product", "amount", "amount_full", "coupon", "link"}
)
COUPON_MAX_LEN = 40


def cart_link_url(settings: Settings, link_token: str) -> str:
    """Public redirect that sends the customer back to the checkout (pages.cart_redirect)."""
    return f"{settings.base_url}/c/{link_token}"


def build_cart_params(
    cart: Cart,
    step: CartStepConfig,
    coupon: str,
    *,
    hard: bool = False,
    settings: Settings | None = None,
) -> list[str]:
    """Body parameters for one cart step, in the order configured for that step."""
    settings = settings or get_settings()
    name_len = 30 if hard else NAME_MAX_LEN
    values: dict[str, str] = {
        "first_name": sanitize_param(first_name(cart.customer_name), name_len, ascii_only=hard)
        or "cliente",
        "customer_name": sanitize_param(cart.customer_name or "", name_len, ascii_only=hard)
        or "cliente",
        "product": sanitize_param(cart.product_name or "", PARAM_MAX_LEN, ascii_only=hard)
        or "produto",
        "amount": fmt_brl(cart.amount_cents),
        "amount_full": fmt_brl(cart.amount_cents, symbol=True),
        "coupon": sanitize_param(coupon, COUPON_MAX_LEN, ascii_only=hard),
        "link": cart_link_url(settings, cart.link_token),
    }
    return fit_body_budget([values.get(key, "") for key in step.params])


def step_template_payload(
    step: StepConfig, params: list[str], *, to: str, button_text: str | None = None
) -> dict[str, Any]:
    """``POST /messages`` body for one step of a sequence (cart or post-sale).

    A template with no variables takes NO body component (``"parameters": []`` is what
    Meta answers #132000 to); the URL button is added only when the step has one.
    """
    components: list[dict[str, Any]] = []
    if params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
        )
    if step.url_button_index >= 0 and button_text:
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": str(step.url_button_index),
                "parameters": [{"type": "text", "text": button_text}],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": step.template_name,
            "language": {"code": step.language},
            "components": components,
        },
    }


def build_cart_payload(
    cart: Cart,
    step: CartStepConfig,
    coupon: str,
    *,
    to: str,
    hard: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """``POST /messages`` body for one cart step; the URL button carries the link token."""
    settings = settings or get_settings()
    params = build_cart_params(cart, step, coupon, hard=hard, settings=settings)
    return step_template_payload(step, params, to=to, button_text=cart.link_token)


# --- post-sale templates -----------------------------------------------------------------

# Keys a post-sale template may use. No coupon or checkout link: an offer in a
# post-sale message makes Meta file the template as Marketing, at Marketing prices.
POST_PARAM_KEYS = frozenset(
    {"first_name", "customer_name", "product", "amount", "amount_full", "sale_id", "email", "link"}
)
EMAIL_MAX_LEN = 120


def access_link_url(settings: Settings, sale_id: str) -> str:
    """Public redirect to the members area of the sale's product (pages.access_redirect)."""
    return f"{settings.base_url}/a/{quote(sale_id, safe='')}"


def build_post_sale_params(
    sale: PostSale, step: StepConfig, *, hard: bool = False, settings: Settings | None = None
) -> list[str]:
    """Body parameters for one post-sale step, in the order configured for that step."""
    settings = settings or get_settings()
    name_len = 30 if hard else NAME_MAX_LEN
    values: dict[str, str] = {
        "first_name": sanitize_param(first_name(sale.customer_name), name_len, ascii_only=hard)
        or "cliente",
        "customer_name": sanitize_param(sale.customer_name or "", name_len, ascii_only=hard)
        or "cliente",
        "product": sanitize_param(sale.product_name or "", PARAM_MAX_LEN, ascii_only=hard)
        or "produto",
        "amount": fmt_brl(sale.amount_cents),
        "amount_full": fmt_brl(sale.amount_cents, symbol=True),
        "sale_id": sanitize_param(sale.sale_id, 64, ascii_only=hard),
        "email": sanitize_param(sale.customer_email or "", EMAIL_MAX_LEN, ascii_only=hard)
        or "seu e-mail",
        "link": access_link_url(settings, sale.sale_id),
    }
    return fit_body_budget([values.get(key, "") for key in step.params])


def build_post_sale_payload(
    sale: PostSale,
    step: StepConfig,
    *,
    to: str,
    hard: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """``POST /messages`` body for one post-sale step; the dynamic button carries the
    sale code (a fixed-link button takes no component at all)."""
    params = build_post_sale_params(sale, step, hard=hard, settings=settings)
    return step_template_payload(step, params, to=to, button_text=quote(sale.sale_id, safe=""))


def step_template_preview(step: StepConfig, params: list[str]) -> str:
    """Human-readable body stored in ``messages.body`` for one step of a sequence."""
    return f"[modelo {step.template_name}] " + " | ".join(params)


def build_text_payload(to: str, body: str) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}


def template_preview(store: SettingsStore, params: list[str]) -> str:
    """Human-readable body stored in ``messages.body`` for the panel inbox."""
    return f"[modelo {store.template_name}] " + " | ".join(params)


# --- errors ----------------------------------------------------------------------------


class ErrorAction(StrEnum):
    RETRY_ALT_NUMBER = "retry_alt_number"  # 131026: try the 12-digit form once
    SANITIZE_RETRY = "sanitize_retry"  # 131009 / 132012 / 132018: harder sanitising, once
    BACKOFF = "backoff"  # 130429 / 131056 / 80007 / 5xx / network: exponential, max 3
    FAIL = "fail"  # 132000 / 132001 / anything unknown
    OPT_OUT = "opt_out"  # 131050: record opt-out
    NO_RETRY_24H = "no_retry_24h"  # 131049: marketing per-user limit
    TOKEN_INVALID = "token_invalid"  # 190 / 401: alert, pause sends
    TEMPLATE_UNAVAILABLE = "template_unavailable"  # 132015 / 132016: mark template, alert
    WINDOW_CLOSED = "window_closed"  # 131047: free text outside the 24h window


ERROR_ACTIONS: dict[int, ErrorAction] = {
    131009: ErrorAction.SANITIZE_RETRY,
    131026: ErrorAction.RETRY_ALT_NUMBER,
    132000: ErrorAction.FAIL,
    132001: ErrorAction.FAIL,
    132012: ErrorAction.SANITIZE_RETRY,
    132018: ErrorAction.SANITIZE_RETRY,
    132015: ErrorAction.TEMPLATE_UNAVAILABLE,
    132016: ErrorAction.TEMPLATE_UNAVAILABLE,
    131047: ErrorAction.WINDOW_CLOSED,
    131049: ErrorAction.NO_RETRY_24H,
    131050: ErrorAction.OPT_OUT,
    130429: ErrorAction.BACKOFF,
    131056: ErrorAction.BACKOFF,
    80007: ErrorAction.BACKOFF,
    190: ErrorAction.TOKEN_INVALID,
    401: ErrorAction.TOKEN_INVALID,
}

# Codes whose failure must raise an operator alert.
ALERT_CODES = frozenset({132000, 132001, 132015, 132016, 190, 401})

FAIL_REASONS: dict[int, str] = {
    131009: "param_invalid",
    131026: "not_on_whatsapp",
    132000: "param_count_mismatch",
    132001: "template_not_found",
    132012: "param_format_mismatch",
    132018: "param_issue",
    132015: "template_paused",
    132016: "template_disabled",
    131047: "window_closed",
    131049: "marketing_limit_24h",
    131050: "opted_out",
    130429: "rate_limited",
    131056: "pair_rate_limited",
    80007: "rate_limited",
    190: "token_invalid",
    401: "token_invalid",
}


@dataclass(frozen=True)
class GraphError:
    code: int | None
    message: str
    subcode: int | None = None
    type: str | None = None
    details: str | None = None
    http_status: int = 0
    fbtrace_id: str | None = None

    @property
    def code_str(self) -> str:
        return str(self.code) if self.code is not None else "network"

    def summary(self, limit: int = 500) -> str:
        parts = [f"#{self.code_str}", self.message]
        if self.details:
            parts.append(self.details)
        return " — ".join(p for p in parts if p)[:limit]


def parse_graph_error(http_status: int, data: Any) -> GraphError:
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return GraphError(code=None, message=f"HTTP {http_status}", http_status=http_status)
    code = err.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    sub = err.get("error_subcode")
    try:
        sub = int(sub) if sub is not None else None
    except (TypeError, ValueError):
        sub = None
    details = None
    ed = err.get("error_data")
    if isinstance(ed, dict):
        details = ed.get("details")
    return GraphError(
        code=code,
        message=str(err.get("message") or ""),
        subcode=sub,
        type=err.get("type"),
        details=details,
        http_status=http_status,
        fbtrace_id=err.get("fbtrace_id"),
    )


def classify_error(error: GraphError | None) -> ErrorAction:
    """Map a Graph error to the action the worker must take (spec table)."""
    if error is None:
        return ErrorAction.FAIL
    if error.code is None:
        return ErrorAction.BACKOFF  # network / non-JSON: transient
    action = ERROR_ACTIONS.get(error.code)
    if action is not None:
        return action
    if error.http_status == 401:
        return ErrorAction.TOKEN_INVALID
    if error.http_status >= 500 or error.code in (1, 2, 4, 17, 32, 613):
        return ErrorAction.BACKOFF  # generic Graph transient/throttling codes
    return ErrorAction.FAIL


def fail_reason_for(error: GraphError | None) -> str:
    if error is None:
        return "unknown_error"
    if error.code is None:
        return "network_error"
    return FAIL_REASONS.get(error.code, f"graph_{error.code}")


# --- signature -------------------------------------------------------------------------


def verify_signature(app_secret: str | None, raw_body: bytes, header_value: str | None) -> bool:
    """Validate ``X-Hub-Signature-256: sha256=<hex hmac-sha256(app_secret, raw_body)>``."""
    if not app_secret or not header_value:
        return False
    if not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[7:].strip())


# --- client ----------------------------------------------------------------------------


@dataclass
class SendResult:
    ok: bool
    to: str
    wa_id: str | None = None
    message_id: str | None = None
    error: GraphError | None = None
    http_status: int = 0
    request_body: dict[str, Any] = field(default_factory=dict)
    params: list[str] = field(default_factory=list)


class GraphClient:
    """Thin synchronous client over the Graph API messages endpoint.

    The access token is only ever placed in the Authorization header; it is
    never logged. ``http`` may be injected for tests.
    """

    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None) -> None:
        self.settings = settings or get_settings()
        self._own_http = http is None
        self.http = http or httpx.Client(timeout=self.settings.http_timeout_seconds)

    @property
    def configured(self) -> bool:
        return self.settings.meta_configured

    @property
    def messages_url(self) -> str:
        return f"{self.settings.graph_base}/{self.settings.meta_phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.meta_access_token or ''}",
            "Content-Type": "application/json",
        }

    def _post_messages(self, body: dict[str, Any], to: str) -> SendResult:
        try:
            resp = self.http.post(self.messages_url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            log.warning("graph network error to=%s: %s", to, exc)
            return SendResult(
                ok=False,
                to=to,
                error=GraphError(code=None, message=f"{type(exc).__name__}: {exc}"),
                request_body=body,
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or "error" in data:
            err = parse_graph_error(resp.status_code, data)
            log.warning("graph send failed to=%s status=%s %s", to, resp.status_code, err.summary())
            return SendResult(
                ok=False, to=to, error=err, http_status=resp.status_code, request_body=body
            )
        contacts = data.get("contacts") or [{}]
        messages = data.get("messages") or [{}]
        return SendResult(
            ok=True,
            to=to,
            wa_id=(contacts[0] or {}).get("wa_id") or to,
            message_id=(messages[0] or {}).get("id"),
            http_status=resp.status_code,
            request_body=body,
        )

    def send_template(
        self,
        order: Order,
        store: SettingsStore,
        *,
        to: str | None = None,
        hard: bool = False,
    ) -> SendResult:
        """Send the configured template for ``order`` to ``to`` (default: primary number)."""
        target = to or order.phone_e164 or order.phone_alt
        if not target:
            return SendResult(ok=False, to="", error=GraphError(code=None, message="no phone"))
        body = build_template_payload(order, store, to=target, hard=hard, settings=self.settings)
        result = self._post_messages(body, target)
        # Look the body component up by type: it is absent for a template with no
        # variables, and then components[0] would be the URL button.
        body_component = next(
            (c for c in body["template"]["components"] if c.get("type") == "body"), None
        )
        result.params = [p["text"] for p in body_component["parameters"]] if body_component else []
        return result

    def send_cart_template(
        self,
        cart: Cart,
        step: CartStepConfig,
        coupon: str,
        *,
        to: str | None = None,
        hard: bool = False,
    ) -> SendResult:
        """Send step ``step`` of a cart sequence to ``to`` (default: primary number)."""
        target = to or cart.phone_e164 or cart.phone_alt
        if not target:
            return SendResult(ok=False, to="", error=GraphError(code=None, message="no phone"))
        body = build_cart_payload(cart, step, coupon, to=target, hard=hard, settings=self.settings)
        return self._post_step(body, target)

    def send_post_sale_template(
        self, sale: PostSale, step: StepConfig, *, to: str | None = None, hard: bool = False
    ) -> SendResult:
        """Send step ``step`` of a post-sale follow-up to ``to`` (default: primary number)."""
        target = to or sale.phone_e164 or sale.phone_alt
        if not target:
            return SendResult(ok=False, to="", error=GraphError(code=None, message="no phone"))
        body = build_post_sale_payload(sale, step, to=target, hard=hard, settings=self.settings)
        return self._post_step(body, target)

    def _post_step(self, body: dict[str, Any], target: str) -> SendResult:
        result = self._post_messages(body, target)
        body_component = next(
            (c for c in body["template"]["components"] if c.get("type") == "body"), None
        )
        result.params = [p["text"] for p in body_component["parameters"]] if body_component else []
        return result

    def send_text(self, to: str, body: str) -> SendResult:
        """Free-form text — only valid inside the 24h customer-service window."""
        return self._post_messages(build_text_payload(to, body), to)

    def fetch_template_status(self, name: str, language: str | None = None) -> dict | None:
        """``GET /{WABA_ID}/message_templates?name=...`` → first matching template dict or None."""
        url = f"{self.settings.graph_base}/{self.settings.meta_waba_id}/message_templates"
        params = {"name": name, "fields": "name,status,category,language,id"}
        try:
            resp = self.http.get(url, params=params, headers=self._headers())
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("template status fetch failed: %s", exc)
            return None
        if resp.status_code >= 400:
            log.warning(
                "template status fetch HTTP %s: %s",
                resp.status_code,
                parse_graph_error(resp.status_code, data).summary(),
            )
            return None
        for item in data.get("data") or []:
            if item.get("name") == name and (language is None or item.get("language") == language):
                return item
        return None

    def close(self) -> None:
        if self._own_http:
            self.http.close()


# --- webhook payload parsing --------------------------------------------------------------


def _ts(value: object) -> datetime | None:
    """Meta timestamps are unix seconds as strings."""
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC)
    except (TypeError, ValueError):
        return None


@dataclass
class StatusUpdate:
    message_id: str
    status: str  # sent | delivered | read | failed
    timestamp: datetime | None
    recipient_id: str | None
    error_code: int | None = None
    error_title: str | None = None
    error_message: str | None = None
    error_details: str | None = None
    conversation_id: str | None = None
    pricing_category: str | None = None


@dataclass
class InboundMessage:
    wa_id: str  # "from"
    message_id: str
    timestamp: datetime | None
    type: str
    text: str | None = None
    button_payload: str | None = None
    button_text: str | None = None
    interactive_id: str | None = None
    interactive_title: str | None = None
    profile_name: str | None = None

    @property
    def display_text(self) -> str:
        """Best-effort text for storage/opt-out detection."""
        return (
            self.text
            or self.button_text
            or self.interactive_title
            or self.button_payload
            or self.interactive_id
            or f"[{self.type}]"
        )

    @property
    def is_button_reply(self) -> bool:
        return self.type in ("button", "interactive")


@dataclass
class TemplateStatusUpdate:
    event: str
    name: str
    language: str | None
    template_id: str | None = None
    reason: str | None = None


@dataclass
class TemplateCategoryUpdate:
    name: str
    language: str | None
    previous_category: str | None
    new_category: str | None


@dataclass
class AccountUpdate:
    field: str
    value: dict


@dataclass
class WebhookEvents:
    statuses: list[StatusUpdate] = field(default_factory=list)
    messages: list[InboundMessage] = field(default_factory=list)
    template_status_updates: list[TemplateStatusUpdate] = field(default_factory=list)
    template_category_updates: list[TemplateCategoryUpdate] = field(default_factory=list)
    other: list[AccountUpdate] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.statuses
            or self.messages
            or self.template_status_updates
            or self.template_category_updates
            or self.other
        )


def _parse_messages_value(value: dict, out: WebhookEvents) -> None:
    profiles: dict[str, str | None] = {}
    for c in value.get("contacts") or []:
        if isinstance(c, dict) and c.get("wa_id"):
            profiles[str(c["wa_id"])] = (c.get("profile") or {}).get("name")

    for st in value.get("statuses") or []:
        if not isinstance(st, dict) or not st.get("id"):
            continue
        err = (st.get("errors") or [{}])[0] or {}
        code = err.get("code")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        out.statuses.append(
            StatusUpdate(
                message_id=str(st["id"]),
                status=str(st.get("status") or "").lower(),
                timestamp=_ts(st.get("timestamp")),
                recipient_id=st.get("recipient_id"),
                error_code=code,
                error_title=err.get("title"),
                error_message=err.get("message"),
                error_details=(err.get("error_data") or {}).get("details"),
                conversation_id=(st.get("conversation") or {}).get("id"),
                pricing_category=(st.get("pricing") or {}).get("category"),
            )
        )

    for m in value.get("messages") or []:
        if not isinstance(m, dict) or not m.get("id") or not m.get("from"):
            continue
        mtype = str(m.get("type") or "unknown").lower()
        text = (m.get("text") or {}).get("body") if mtype == "text" else None
        button = m.get("button") or {}
        interactive = m.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        wa_id = str(m["from"])
        out.messages.append(
            InboundMessage(
                wa_id=wa_id,
                message_id=str(m["id"]),
                timestamp=_ts(m.get("timestamp")),
                type=mtype,
                text=text,
                button_payload=button.get("payload"),
                button_text=button.get("text"),
                interactive_id=reply.get("id"),
                interactive_title=reply.get("title"),
                profile_name=profiles.get(wa_id),
            )
        )


def parse_webhook_payload(payload: dict) -> WebhookEvents:
    """Flatten ``entry[].changes[]`` of a Meta webhook body into typed events."""
    out = WebhookEvents()
    if not isinstance(payload, dict):
        return out
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            fld = str(change.get("field") or "")
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            out.fields.append(fld)
            if fld == "messages":
                _parse_messages_value(value, out)
            elif fld == "message_template_status_update":
                if value.get("message_template_name"):
                    out.template_status_updates.append(
                        TemplateStatusUpdate(
                            event=str(value.get("event") or "").upper(),
                            name=str(value["message_template_name"]),
                            language=value.get("message_template_language"),
                            template_id=(
                                str(value["message_template_id"])
                                if value.get("message_template_id") is not None
                                else None
                            ),
                            reason=value.get("reason"),
                        )
                    )
            elif fld == "template_category_update":
                if value.get("message_template_name"):
                    out.template_category_updates.append(
                        TemplateCategoryUpdate(
                            name=str(value["message_template_name"]),
                            language=value.get("message_template_language"),
                            previous_category=value.get("previous_category"),
                            new_category=value.get("new_category"),
                        )
                    )
            else:
                out.other.append(AccountUpdate(field=fld, value=value))
    return out
