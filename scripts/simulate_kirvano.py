"""Post a realistic Kirvano webhook body to a local or remote URL.

Kirvano has no sandbox: every real test costs an R$1,00 PIX. This script fakes
the three events we care about so the whole pipeline (parse -> order -> job ->
cancel) can be exercised against a running instance without touching Kirvano.

The body is modelled on the REAL capture in
``tests/fixtures/kirvano_pix_generated.json``: same keys, same mixed
snake_case/camelCase at the top level, ``payment.qrcode_image`` repeating the
EMV string instead of being a URL, numeric ``fiscal.total_value``, ``ip``,
``cookies`` and the CPF in ``customer.document`` — the last two exist here on
purpose, so a run also proves that the server drops them (LGPD minimisation).

Usage::

    uv run python scripts/simulate_kirvano.py                       # PIX_GENERATED, localhost
    uv run python scripts/simulate_kirvano.py --event SALE_APPROVED --sale-id 5LZEB2GJ
    uv run python scripts/simulate_kirvano.py --event SALE_APPROVED --method CREDIT_CARD
    uv run python scripts/simulate_kirvano.py --url https://api.jornadaanjo.cloud/webhooks/kirvano
    uv run python scripts/simulate_kirvano.py --print               # only show the JSON

The token defaults to ``KIRVANO_WEBHOOK_TOKEN``; ``--token-in`` decides whether
it travels as a header (Kirvano's transport is undocumented), in the body, or
both — handy for probing a server running in ``KIRVANO_TOKEN_MODE=enforce``.
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

# `python scripts/foo.py` puts scripts/ on sys.path, not the repo root, so the
# `app` package would not be importable without this line.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8000/webhooks/kirvano"
DEFAULT_TOKEN_HEADER = "x-kirvano-token"
# Stand-in used by --print so the real secret never reaches the terminal.
TOKEN_PLACEHOLDER = "<KIRVANO_WEBHOOK_TOKEN>"

# Statuses observed in the real payloads (spec, Kirvano webhook doc).
STATUS_BY_EVENT: dict[str, str] = {
    "PIX_GENERATED": "PENDING",
    "PIX_EXPIRED": "CANCELED",
    "SALE_APPROVED": "APPROVED",
    "SALE_REFUSED": "REFUSED",
    "SALE_REFUNDED": "REFUNDED",
    "SALE_CHARGEBACK": "CHARGEBACK",
    # Not captured from a real delivery yet: shape assumed from the other events
    # (no sale_id, no payment). Replace with the real body once one arrives.
    "ABANDONED_CART": "ABANDONED",
}
DESCRIPTION_BY_EVENT: dict[str, str] = {
    "PIX_GENERATED": "PIX gerado",
    "PIX_EXPIRED": "PIX expirado",
    "SALE_APPROVED": "Compra aprovada",
    "SALE_REFUSED": "Compra recusada",
    "SALE_REFUNDED": "Reembolso",
    "SALE_CHARGEBACK": "Chargeback",
    "ABANDONED_CART": "Carrinho abandonado",
}
EVENTS = tuple(STATUS_BY_EVENT)

# Real ids from the client's account: the merchant filters the webhook by product,
# so a simulated event must carry his product/offer to be realistic.
PRODUCT_ID = "1c5f17f3-8682-4015-9837-05d2d7807757"
OFFER_ID = "56619168-3b4c-4052-b28b-238288ad2190"
CATEGORY_ID = "91321cff-087a-4353-96c3-01af1c7f171"
PRODUCT_NAME = "A Jornada com meu Anjo"
OFFER_NAME = "Padrao 97 Sem Order"
CONTACT_EMAIL = "jornadacommeuanjo@outlook.com"

# The EMV "copia e cola" payload. Kirvano repeats it verbatim in `qrcode_image`.
EMV = (
    "00020101021226990014br.gov.bcb.pix2577pix.bancogenial.com/qr/v3/at/"
    "1f8c9a2e-4d7b-4a10-9c3e-2b6f8d5a7e41520400005303986540597.005802BR"
    "5925CONNECT LT NEGOCIOS DIGIT6009SAO PAULO62070503***6304A1B2"
)


# --- pure helpers (unit-tested) ---------------------------------------------------


def random_code(length: int = 8) -> str:
    """An 8-char uppercase code, the shape of ``sale_id`` / ``checkout_id``."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def fmt_local(dt: datetime, tz_name: str = "America/Sao_Paulo") -> str:
    """Aware datetime -> Kirvano's NAIVE local ``YYYY-MM-DD HH:MM:SS`` string."""
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def fmt_price(value: float) -> str:
    """``97`` -> ``"R$ 97,00"``; ``1169.8`` -> ``"R$ 1.169,80"`` (pt-BR grouping)."""
    return "R$ " + f"{value:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def build_payload(
    event: str = "PIX_GENERATED",
    *,
    sale_id: str | None = None,
    checkout_id: str | None = None,
    customer_name: str = "Maria Souza de Oliveira",
    customer_email: str = "maria.souza.exemplo@gmail.com",
    phone: str = "5551994697674",
    document: str = "00000000191",
    amount: float = 97.0,
    created_at: datetime | None = None,
    expiry_hours: float = 24.0,
    expires_at: datetime | None = None,
    finished_at: datetime | None = None,
    checkout_url: str | None = None,
    ip: str = "200.152.1.115",
    tz_name: str = "America/Sao_Paulo",
    method: str = "PIX",
) -> dict[str, Any]:
    """Build one Kirvano webhook body, byte-shaped like the real capture.

    ``created_at`` defaults to "now"; ``expires_at`` to created_at + 24 h, which
    is this merchant's real checkout setting (the vendor doc's 1 h example is not
    his). PIX_EXPIRED carries the ``checkout_url`` recovery link. ``method`` other
    than PIX makes a card/boleto sale: no PIX fields, and the server creates no order.
    """
    event = event.upper()
    now = created_at or datetime.now(tz=ZoneInfo(tz_name))
    sale_id = sale_id or random_code()
    checkout_id = checkout_id or random_code()
    expires = expires_at or (now + timedelta(hours=expiry_hours))
    price = fmt_price(amount)

    method = method.upper()
    payment: dict[str, Any] = {"method": method}
    if event in ("PIX_GENERATED", "SALE_APPROVED") and method == "PIX":
        payment["qrcode"] = EMV
        payment["expires_at"] = fmt_local(expires, tz_name)
        # NOT a URL: Kirvano repeats the EMV string here. The PIX page must
        # render the QR itself; anything reading this as an <img src> is a bug.
        payment["qrcode_image"] = EMV
    elif event == "PIX_EXPIRED":
        payment["expires_at"] = fmt_local(expires, tz_name)
    if event == "SALE_APPROVED":
        payment["finished_at"] = fmt_local(finished_at or now, tz_name)

    commission = round(amount * 0.9304, 2)  # ~ the real fee split (R$ 6,75 on R$ 97)
    fee = round(amount - commission, 2)

    body: dict[str, Any] = {
        # Top level mixes snake_case and camelCase exactly like the real payload.
        "ip": ip,
        "fee": fee,
        "utm": {
            "src": "comquizlead9expert2v0fechamentonovo",
            "utm_term": "Facebook_Stories",
            "utm_medium": "Aberto|120248144239720388",
            "utm_source": "FB",
            "utm_content": "Video 233 Anjo|120248144265040388",
            "utm_campaign": "CBO Angel - 1-1-5 - 20/06/2026|120248144239730388",
        },
        "type": "ONE_TIME",
        "event": event,
        "fiscal": {
            "fee": fee,
            "net_value": commission,
            "commission": commission,
            "service_tax": 0,
            # A NUMBER, unlike total_price: the parser prefers it.
            "total_value": amount,
            "original_value": amount,
            "coupon_discount": 0,
            "total_discounts": 0,
            "total_commissions": commission,
            "automatic_discount": 0,
            "affiliate_commission": 0,
            "coproduction_commission": 0,
        },
        "status": STATUS_BY_EVENT.get(event, "UNKNOWN"),
        # Ad identifiers: sent by Kirvano, MUST be dropped by our parser.
        "cookies": {
            "fbp": "fb.1.1760742058426.392676984852077911",
            "sck": "v3_96e04d41-05ed-44a3-ba95-755ebef2ba39_6a4bad4e6f95c",
            "ttp": "01KNZG67BNKFT4DJ34SH4BBY0N_tt.1",
            "gclid": "11.2034691902.1776911519.430392091.1776936636.1776936635",
            "fbclid": "IwZXh0bgNhZW0BMABhZGlkAas2KY4St0RzcnRjBmFwcF9pZA",
        },
        "payment": payment,
        "sale_id": sale_id,
        "customer": {
            "name": customer_name,
            "email": customer_email,
            "address": {
                "city": None,
                "state": None,
                "number": None,
                "street": None,
                "zipcode": None,
                "complement": None,
                "neighborhood": None,
            },
            # The CPF. Present in the wire format, never persisted by us.
            "document": document,
            "phone_number": phone,
        },
        "products": [
            {
                "id": PRODUCT_ID,
                "name": PRODUCT_NAME,
                "photo": f"products/{PRODUCT_ID}/cover-1.jpg",
                "price": price,
                "format": "community",
                "category": CATEGORY_ID,
                "offer_id": OFFER_ID,
                "offer_name": OFFER_NAME,
                "description": "A Jornada com meu Anjo e um guia espiritual para guiar",
                "is_order_bump": False,
            }
        ],
        "commission": commission,
        "created_at": fmt_local(now, tz_name),
        "checkout_id": checkout_id,
        "total_price": price,
        "contactEmail": CONTACT_EMAIL,
        "couponDiscount": 0,
        "payment_method": method,
        "automaticDiscount": 0,
        "event_description": DESCRIPTION_BY_EVENT.get(event, event),
        "affiliateCommission": 0,
        "coproductionCommission": 0,
    }
    if event == "PIX_EXPIRED":
        # The link Kirvano gives the customer to generate a NEW PIX.
        body["checkout_url"] = checkout_url or f"https://pay.kirvano.com/recovery/{checkout_id}"
    elif checkout_url:
        body["checkout_url"] = checkout_url
    if event == "ABANDONED_CART":
        # Nothing was paid or even generated: no sale, no payment block.
        for key in ("sale_id", "payment", "payment_method"):
            body.pop(key, None)
    return body


def build_headers(
    token: str | None, *, token_in: str = "header", header_name: str = DEFAULT_TOKEN_HEADER
) -> dict[str, str]:
    """Request headers; the token is added only for ``header``/``both``."""
    headers = {"Content-Type": "application/json", "User-Agent": "kirvano-simulator/1.0"}
    if token and token_in in ("header", "both"):
        headers[header_name] = token
    return headers


def with_body_token(
    payload: dict[str, Any], token: str | None, *, token_in: str = "header"
) -> dict[str, Any]:
    """Copy of ``payload`` with ``token`` in the body for ``body``/``both``."""
    if not token or token_in not in ("body", "both"):
        return payload
    out = dict(payload)
    out["token"] = token
    return out


def post_payload(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    token_in: str = "header",
    header_name: str = DEFAULT_TOKEN_HEADER,
) -> httpx.Response:
    """POST the body; returns the raw response so the caller can print it."""
    return client.post(
        url,
        json=with_body_token(payload, token, token_in=token_in),
        headers=build_headers(token, token_in=token_in, header_name=header_name),
    )


# --- CLI --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="simulate_kirvano.py",
        description="Envia um webhook Kirvano realista para testar o fluxo ponta a ponta.",
    )
    p.add_argument("--event", default="PIX_GENERATED", choices=EVENTS, help="evento a simular")
    p.add_argument("--url", default=DEFAULT_URL, help=f"destino (padrao {DEFAULT_URL})")
    p.add_argument("--sale-id", default=None, help="reaproveita o mesmo pedido entre eventos")
    p.add_argument("--checkout-id", default=None)
    p.add_argument("--name", default="Maria Souza de Oliveira")
    p.add_argument("--phone", default="5551994697674", help="55 + DDD + numero, so digitos")
    p.add_argument("--amount", type=float, default=97.0)
    p.add_argument(
        "--method",
        default="PIX",
        choices=("PIX", "CREDIT_CARD", "BANK_SLIP"),
        help="forma de pagamento (CREDIT_CARD: venda de cartao, nao cria pedido PIX)",
    )
    p.add_argument("--expiry-hours", type=float, default=24.0, help="validade do PIX (real: 24 h)")
    p.add_argument("--token", default=None, help="padrao: KIRVANO_WEBHOOK_TOKEN do ambiente")
    p.add_argument("--token-in", default="header", choices=("header", "body", "both", "none"))
    p.add_argument("--token-header", default=DEFAULT_TOKEN_HEADER)
    p.add_argument("--print", dest="print_only", action="store_true", help="so imprime o JSON")
    p.add_argument("--timeout", type=float, default=15.0)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    token = args.token if args.token is not None else settings.kirvano_webhook_token

    payload = build_payload(
        args.event,
        sale_id=args.sale_id,
        checkout_id=args.checkout_id,
        customer_name=args.name,
        phone=args.phone,
        amount=args.amount,
        expiry_hours=args.expiry_hours,
        tz_name=settings.kirvano_tz,
        method=args.method,
    )

    if args.print_only:
        # NEVER print the real token: `--print --token-in body` would put
        # KIRVANO_WEBHOOK_TOKEN into the terminal, the shell history and any pasted
        # output. The placeholder still shows WHERE the token would sit, which is the
        # only thing --print is for; post_payload keeps using the real value.
        shown = with_body_token(
            payload, TOKEN_PLACEHOLDER if token else None, token_in=args.token_in
        )
        print(json.dumps(shown, indent=2))
        return 0

    print(f"POST {args.url}")
    print(f"  evento ..... {payload['event']} ({payload['status']})")
    # An ABANDONED_CART has no sale and no payment block: only the checkout code.
    print(f"  pedido ..... {payload.get('sale_id') or '-'} (checkout {payload['checkout_id']})")
    print(f"  valor ...... {payload['total_price']}")
    print(f"  telefone ... {payload['customer']['phone_number']}")
    print(f"  expira em .. {payload.get('payment', {}).get('expires_at', '-')}")
    print(
        f"  token ...... {'sim' if token and args.token_in != 'none' else 'nao'} ({args.token_in})"
    )

    try:
        with httpx.Client(timeout=args.timeout) as client:
            resp = post_payload(
                client,
                args.url,
                payload,
                token=token,
                token_in=args.token_in,
                header_name=args.token_header,
            )
    except httpx.HTTPError as exc:
        print(f"\nFALHA de rede: {exc}")
        return 2

    print(f"\nHTTP {resp.status_code}")
    print(resp.text[:1000])
    if resp.status_code == 401:
        print(
            "\n401: o servidor esta em KIRVANO_TOKEN_MODE=enforce e nao aceitou o token.\n"
            "Tente --token-in body, ou confira KIRVANO_WEBHOOK_TOKEN dos dois lados."
        )
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
