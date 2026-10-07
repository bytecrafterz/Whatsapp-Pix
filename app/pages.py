"""Public, unauthenticated pages: the customer PIX page and the privacy policy.

Both are customer-facing, so everything the customer reads is pt-BR while the
code and comments stay English. Hard constraints from ``docs/SPEC.md``:

* ``GET /p/{page_token}`` answers ``Cache-Control: no-store`` — it shows a live
  payment state and a payable code, neither of which may sit in a shared cache
  (a CDN or a corporate proxy handing order A's PIX to customer B would be the
  worst possible bug here);
* no external assets, no tracking, no JS framework — the whole page is the
  inline CSS of ``base.html`` plus ~30 lines of vanilla JS;
* no PII beyond the customer's **first name** and the order code. Never the
  phone, the e-mail, the CPF (which we do not even store) or the checkout IP;
* the QR is rendered **server side** from ``order.pix_code`` with ``qrcode``,
  because ``payment.qrcode_image`` in the real Kirvano payload is a duplicate of
  the EMV copia-e-cola string, not a URL (docs/ARCHITECTURE.md §9.1). The parser
  therefore leaves ``pix_qr_image_url`` NULL for this merchant; we only fall back
  to it when it really is an ``http(s)`` URL *and* we have no code to draw.

The QR lives at its own route (``/p/{token}/qr.png``) instead of a ``data:`` URI
so the HTML stays small on a mobile connection and the browser can paint the
text (code + copy button, the part that actually pays) before the image arrives.
"""

from __future__ import annotations

import io
import logging
import unicodedata
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import qrcode
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import clock
from app.cart import register_click
from app.db import get_session
from app.deps import FRAME_DENY_HEADERS
from app.models import Cart, Order, OrderStatus
from app.queries import cart_by_link_token, order_by_page_token
from app.settings_store import SettingsStore, get_settings_store
from app.templating import templates

log = logging.getLogger("app.pages")

router = APIRouter()

# Date shown on /privacidade ("última atualização").
PRIVACY_UPDATED_AT = "07/10/2026"  # added the abandoned-cart messages

# Public contacts: the controller's e-mail from the CNPJ record, and the merchant's
# support address (it arrives in every Kirvano payload as `contactEmail`).
CONTROLLER_EMAIL = "LT.CONNECT@OUTLOOK.COM"
SUPPORT_EMAIL = "jornadacommeuanjo@outlook.com"

# Headers shared by every /p/... response.
#   no-store    — SPEC: the page must never be cached anywhere.
#   no-referrer — the page_token is the URL's only secret; without this, following
#   the "Gerar novo PIX" link would hand that token to pay.kirvano.com in Referer.
#   frame headers — a framed PIX page is a clickjacking surface for the "Copiar
#   codigo" button and would let a third party present our page as its own.
NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    **FRAME_DENY_HEADERS,
}

# QR geometry: box_size 8 gives a ~330 px PNG (crisp on a phone, still a couple of
# KB) and ERROR_CORRECT_M is the usual level for PIX codes in Brazilian bank apps.
_QR_BOX_SIZE = 8
_QR_BORDER = 2


def render_qr_png(data: str) -> bytes:
    """Render an EMV PIX payload as a PNG (bytes). Raises on an over-long payload."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=_QR_BOX_SIZE,
        border=_QR_BORDER,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def effective_state(order: Order, now: datetime) -> str:
    """What the customer must see, which is not always ``order.status``.

    Kirvano sends ``PIX_EXPIRED`` when *its* job runs, which can lag minutes behind
    the printed ``expires_at``. Between those two moments the order is still
    ``pending`` in our DB but the code is already dead — showing it would invite a
    payment the bank will refuse. So expiry is decided by the clock, not the event.
    """
    status = order.status or OrderStatus.UNKNOWN.value
    if (
        status == OrderStatus.PENDING.value
        and order.pix_expires_at is not None
        and order.pix_expires_at <= now
    ):
        return OrderStatus.EXPIRED.value
    return status


def safe_http_url(value: str | None) -> str | None:
    """Return ``value`` only when it is an http(s) URL, else ``None``.

    ``checkout_url`` is operator-editable in the panel and ``checkout_recovery_url``
    comes straight from a webhook body: neither may become a ``javascript:`` href.
    """
    if not value:
        return None
    candidate = value.strip()
    if candidate.lower().startswith(("http://", "https://")):
        return candidate
    return None


def new_pix_url(order: Order, store: SettingsStore) -> str | None:
    """Where a customer with an expired/refused order generates a fresh PIX.

    Kirvano's own recovery link (``checkout_url`` on the PIX_EXPIRED event) is
    order-specific and therefore preferred; the panel's generic checkout link is
    the fallback. ``None`` when neither is usable — the template then explains
    what to do instead of rendering a dead button.
    """
    return safe_http_url(order.checkout_recovery_url) or safe_http_url(store.checkout_url)


def _not_found(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "pages/nao_encontrado.html",
        {},
        status_code=404,
        headers=NO_STORE_HEADERS,
    )


@router.get("/p/{page_token}", response_class=HTMLResponse)
def pix_page(
    page_token: str,
    request: Request,
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
) -> Response:
    """The page linked from the WhatsApp template's URL button."""
    order = order_by_page_token(session, page_token)
    if order is None:
        return _not_found(request)

    now = clock.utcnow()
    has_code = bool(order.pix_code)
    context = {
        "order": order,
        "state": effective_state(order, now),
        "qr_png_url": f"/p/{quote(order.page_token, safe='')}/qr.png" if has_code else None,
        # Only used when Kirvano really gave us an image URL and we have no code to draw.
        "qr_fallback_url": None if has_code else safe_http_url(order.pix_qr_image_url),
        "new_pix_url": new_pix_url(order, store),
        # ISO-8601 (UTC) drives the countdown script; the human date comes from `dt_sp`.
        "expires_iso": order.pix_expires_at.isoformat() if order.pix_expires_at else None,
        "support_email": SUPPORT_EMAIL,
    }
    return templates.TemplateResponse(request, "pages/pix.html", context, headers=NO_STORE_HEADERS)


@router.get("/p/{page_token}/qr.png")
def pix_qr_png(page_token: str, session: Session = Depends(get_session)) -> Response:
    """The PIX QR as a PNG, drawn on the fly from the stored EMV string."""
    order = order_by_page_token(session, page_token)
    if order is None or not order.pix_code:
        return PlainTextResponse("não encontrado", status_code=404, headers=NO_STORE_HEADERS)
    try:
        png = render_qr_png(order.pix_code)
    except Exception:
        # A payload too long for any QR version, or a broken Pillow install. The page
        # still works without the image (the copia-e-cola textarea is the real payment
        # path), so degrade to a broken <img> instead of a 500 on the whole request.
        log.exception("QR rendering failed for order %s", order.sale_id)
        return PlainTextResponse("erro ao gerar o QR", status_code=404, headers=NO_STORE_HEADERS)
    headers = dict(NO_STORE_HEADERS)
    headers["Content-Length"] = str(len(png))
    return Response(png, media_type="image/png", headers=headers)


def with_tracking(url: str, coupon: str, utm: list[tuple[str, str]]) -> str:
    """``url`` with the coupon (Kirvano applies ``?coupon=CODE`` by itself) and our UTMs.

    The link's other parameters are kept. Its own ``utm_*`` (the ad the customer first
    came from) are dropped when we add ours, so the sale is credited to the WhatsApp
    recovery instead of a mix of both.
    """
    ours = list(utm)
    if coupon:
        ours.append(("coupon", coupon))
    if not ours:
        return url
    replaced = {k.lower() for k, _ in ours}
    drop_utm = any(k.lower().startswith("utm_") for k, _ in utm)
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in replaced and not (drop_utm and k.lower().startswith("utm_"))
    ]
    return urlunsplit(parts._replace(query=urlencode(kept + ours)))


def _product_key(name: str | None) -> str:
    """Product name for matching: no accents, no case, single spaces
    ("Oração de Santo  Antônio" == "oracao de santo antonio")."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


def product_checkout_link(cart: Cart, links: list[tuple[str, str]]) -> str | None:
    """The panel's checkout link for the cart's product: same product name (ignoring
    case and accents), else a link that contains the cart's Kirvano offer id."""
    product = _product_key(cart.product_name)
    for name, url in links:
        if name and product and _product_key(name) == product:
            return safe_http_url(url)
    if cart.offer_id:
        for _, url in links:
            if cart.offer_id in url:
                return safe_http_url(url)
    return None


def cart_destination(cart: Cart, store: SettingsStore) -> str | None:
    """Where the cart message's button leads, with the coupon already applied and the
    WhatsApp-recovery UTMs: the cart's own checkout link, else the panel's link for
    the cart's product, else the panel's fallback for carts, else the PIX flow's
    generic checkout link."""
    base = (
        safe_http_url(cart.checkout_url)
        or product_checkout_link(cart, store.cart_product_links)
        or safe_http_url(store.cart_checkout_url)
        or safe_http_url(store.checkout_url)
    )
    if base is None:
        return None
    return with_tracking(base, store.cart_coupon.strip(), store.cart_link_utm)


@router.get("/c/{link_token}")
def cart_redirect(
    link_token: str,
    request: Request,
    session: Session = Depends(get_session),
    store: SettingsStore = Depends(get_settings_store),
) -> Response:
    """The abandoned-cart message's button: count the tap, send them to the checkout.

    Going through our domain instead of linking Kirvano directly keeps the approved
    template independent of Kirvano's URL format (the button's base URL is fixed at
    approval time) and is the only way to know a message was actually tapped.
    """
    cart = cart_by_link_token(session, link_token)
    if cart is None:
        return _not_found(request)
    destination = cart_destination(cart, store)
    if destination is None:
        log.warning("cart %s: no checkout link to redirect to", cart.id)
        return _not_found(request)
    try:
        register_click(session, cart, clock.utcnow())
        session.commit()
    except Exception:  # noqa: BLE001 - a lost click count must never block the customer
        session.rollback()
        log.exception("could not record click for cart %s", cart.id)
    # 302 + no-store: the redirect must be re-evaluated (and counted) on every tap.
    # no-referrer keeps the link token out of Kirvano's logs.
    return RedirectResponse(destination, status_code=302, headers=NO_STORE_HEADERS)


@router.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> Response:
    """Public landing page.

    The API itself has no browsable root, but Meta reviewers and curious
    customers do open the bare domain, and a raw 404 reads as a broken site.
    Keep it factual: what the domain is for, and how to stop the messages.
    """
    return templates.TemplateResponse(
        request,
        "pages/inicio.html",
        {"support_email": SUPPORT_EMAIL},
        headers=NO_STORE_HEADERS,
    )


@router.get("/privacidade", response_class=HTMLResponse)
def privacy_page(request: Request) -> Response:
    """Privacy policy — Meta requires a public one before the app can go Live."""
    return templates.TemplateResponse(
        request,
        "pages/privacidade.html",
        {
            "updated_at": PRIVACY_UPDATED_AT,
            "controller_email": CONTROLLER_EMAIL,
            "support_email": SUPPORT_EMAIL,
        },
        headers=dict(FRAME_DENY_HEADERS),
    )
