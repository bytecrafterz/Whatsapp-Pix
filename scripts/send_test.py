"""Send ONE test template message through the WhatsApp Cloud API.

    uv run python scripts/send_test.py --to 5551994697674 --order 5LZEB2GJ
    uv run python scripts/send_test.py --to 55... --order X --print     # only show the JSON
    uv run python scripts/send_test.py --to 55... --text "oi"           # free text (24 h window)

This is a smoke test for the credentials, the phone number and the template — it
does NOT touch the database and does NOT create a reminder job. The reminder
itself is always sent by the worker, exactly once per order.

The parameters are built from the ENVIRONMENT (TEMPLATE_NAME, TEMPLATE_LANGUAGE,
TEMPLATE_URL_BUTTON_INDEX, TEMPLATE_PARAMS) and mirror
``app.whatsapp.build_template_payload``. The panel can override those keys in the
database; this script cannot see that, so if you changed them in the panel pass
``--template`` / ``--params`` / ``--button-index`` explicitly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402

# Meta rejects newline, tab and 4+ consecutive spaces in a parameter (#131009).
_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# pt-BR reading of the errors the operator can actually hit here.
ERROR_HINTS: dict[int, str] = {
    131009: "parametro invalido: quebra de linha, tabulacao ou 4+ espacos seguidos.",
    131026: "numero nao esta no WhatsApp ou nao pode receber: tente a outra forma "
    "do 9o digito (o worker faz isso sozinho).",
    132000: "quantidade de parametros diferente do modelo: ajuste TEMPLATE_PARAMS.",
    132001: "modelo inexistente nesse idioma: confira nome e pt_BR.",
    132012: "formato de parametro nao bate com a amostra do modelo.",
    132015: "modelo PAUSADO por qualidade.",
    132016: "modelo DESATIVADO pela Meta.",
    131047: "fora da janela de 24 h: texto livre so depois de o cliente escrever.",
    131049: "limite de marketing por usuario nas ultimas 24 h.",
    131050: "usuario optou por nao receber marketing.",
    130429: "limite de envios atingido: espere e tente de novo.",
    190: "token invalido ou expirado: gere outro (README > rotacionar token).",
    100: "parametro da chamada errado (PHONE_NUMBER_ID?).",
}


def sanitize(value: object, max_len: int = 60) -> str:
    """Collapse whitespace, drop control chars, trim and cap — same rules as the app."""
    s = "" if value is None else str(value)
    s = _CTRL.sub("", _WS.sub(" ", s)).strip()
    return s[:max_len].rstrip() if max_len else s


def first_name(full_name: str | None) -> str:
    return sanitize((full_name or "").split(" ")[0], 30) or "cliente"


def fmt_expiry(dt: datetime, tz_name: str = "America/Sao_Paulo") -> str:
    """``dd/mm as HH:MM`` in Sao Paulo — the {{4}} format of the template."""
    local = dt.astimezone(ZoneInfo(tz_name))
    return f"{local:%d/%m} às {local:%H:%M}"


def param_values(
    settings: Settings,
    *,
    name: str,
    sale_id: str,
    amount: str,
    expiry: str,
    page_token: str,
    product: str = "A Jornada com meu Anjo",
) -> dict[str, str]:
    """Every value a template parameter key can take (same keys as app.whatsapp)."""
    return {
        "first_name": first_name(name),
        "customer_name": sanitize(name) or "cliente",
        "sale_id": sanitize(sale_id, 64),
        "amount": sanitize(amount, 20),
        "amount_full": sanitize(f"R$ {amount}", 24),
        "expiry": sanitize(expiry, 40),
        "product": sanitize(product, 120),
        "page_url": settings.page_url(page_token),
    }


def build_template_payload(
    settings: Settings,
    *,
    to: str,
    values: dict[str, str],
    template_name: str,
    language: str,
    params: list[str],
    button_index: int,
    page_token: str,
) -> dict[str, Any]:
    """The exact body of ``POST /{PHONE_NUMBER_ID}/messages`` for a template."""
    body_params = [{"type": "text", "text": values.get(key, "")} for key in params]
    components: list[dict[str, Any]] = [{"type": "body", "parameters": body_params}]
    if button_index >= 0:
        # The dynamic URL button takes only the SUFFIX of the link
        # (https://api.jornadaanjo.cloud/p/{{1}} -> the page token).
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": str(button_index),
                "parameters": [{"type": "text", "text": page_token}],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }


def build_text_payload(to: str, body: str) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}


def messages_url(settings: Settings) -> str:
    return f"{settings.graph_base}/{settings.meta_phone_number_id}/messages"


def send(client: httpx.Client, settings: Settings, payload: dict[str, Any]) -> httpx.Response:
    return client.post(
        messages_url(settings),
        json=payload,
        headers={
            "Authorization": f"Bearer {settings.meta_access_token or ''}",
            "Content-Type": "application/json",
        },
    )


def describe_response(resp: httpx.Response) -> str:
    """pt-BR summary: message id on success, code + hint on failure."""
    try:
        data: Any = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    if isinstance(data, dict) and "error" in data:
        err = data.get("error") or {}
        try:
            code = int(err.get("code"))
        except (TypeError, ValueError):
            code = 0
        hint = ERROR_HINTS.get(code, "veja a tabela de erros no README.")
        return (
            f"HTTP {resp.status_code} erro #{code} "
            f"(subcode {err.get('error_subcode')}): {err.get('message')}\n  -> {hint}"
        )
    messages = (data.get("messages") or [{}]) if isinstance(data, dict) else [{}]
    contacts = (data.get("contacts") or [{}]) if isinstance(data, dict) else [{}]
    return (
        f"HTTP {resp.status_code} enviado.\n"
        f"  wa_id ....... {contacts[0].get('wa_id')}\n"
        f"  message_id .. {messages[0].get('id')}\n"
        "  (entregue/lido chegam depois pelo webhook: /painel > Conversas)"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="send_test.py",
        description="Envia UMA mensagem de teste (modelo ou texto livre) pela Cloud API.",
    )
    p.add_argument("--to", required=True, help="destino: 55 + DDD + numero, so digitos")
    p.add_argument("--order", default="TESTE123", help="codigo do pedido = {{2}}")
    p.add_argument("--name", default="Maria", help="nome do cliente = {{1}}")
    p.add_argument("--amount", default="97,00", help="valor sem 'R$' = {{3}}")
    p.add_argument("--expiry", default=None, help="{{4}}; padrao: daqui a 24 h (dd/mm as HH:MM)")
    p.add_argument("--page-token", default="teste123", help="sufixo do botao de URL")
    p.add_argument("--template", default=None, help="padrao: TEMPLATE_NAME")
    p.add_argument("--language", default=None, help="padrao: TEMPLATE_LANGUAGE")
    p.add_argument("--params", default=None, help="ordem dos parametros, separados por virgula")
    p.add_argument("--button-index", type=int, default=None, help="-1 = modelo sem botao de URL")
    p.add_argument("--text", default=None, help="envia texto livre (so na janela de 24 h)")
    p.add_argument("--print", dest="print_only", action="store_true", help="so imprime o JSON")
    p.add_argument("--timeout", type=float, default=None)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()

    if not settings.meta_access_token and not args.print_only:
        print("META_ACCESS_TOKEN nao esta definido no ambiente. Exporte o secrets.env primeiro.")
        return 2

    to = re.sub(r"\D", "", args.to)
    if args.text:
        payload = build_text_payload(to, args.text)
    else:
        expiry = args.expiry or fmt_expiry(
            datetime.now(tz=ZoneInfo(settings.business_tz)) + timedelta(hours=24),
            settings.business_tz,
        )
        values = param_values(
            settings,
            name=args.name,
            sale_id=args.order,
            amount=args.amount,
            expiry=expiry,
            page_token=args.page_token,
        )
        params = [
            p.strip() for p in (args.params or settings.template_params).split(",") if p.strip()
        ]
        button_index = (
            args.button_index
            if args.button_index is not None
            else int(settings.template_url_button_index)
        )
        payload = build_template_payload(
            settings,
            to=to,
            values=values,
            template_name=args.template or settings.template_name,
            language=args.language or settings.template_language,
            params=params,
            button_index=button_index,
            page_token=args.page_token,
        )

    if args.print_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"POST {messages_url(settings)}")
    print(f"  para ....... {to}")
    if args.text:
        print(f"  texto ...... {args.text[:80]}")
    else:
        tpl = payload["template"]
        print(f"  modelo ..... {tpl['name']} ({tpl['language']['code']})")
        body_params = payload["template"]["components"][0]["parameters"]
        print(f"  parametros . {[p['text'] for p in body_params]}")
        print(f"  link ....... {settings.page_url(args.page_token)}")

    try:
        with httpx.Client(timeout=args.timeout or settings.http_timeout_seconds) as client:
            resp = send(client, settings, payload)
    except httpx.HTTPError as exc:
        print(f"\nFALHA de rede: {exc}")
        return 2

    print()
    print(describe_response(resp))
    return 0 if resp.status_code < 400 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
