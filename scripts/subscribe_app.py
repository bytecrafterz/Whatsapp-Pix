"""Subscribe the Meta app to the WABA's webhooks (``POST /{WABA_ID}/subscribed_apps``).

Configuring the callback URL in the App Dashboard is not enough: the app also has
to be subscribed to the WhatsApp Business Account, otherwise no `messages`,
`statuses` or `message_template_status_update` event ever reaches us. Run this
once, after the webhook URL has been verified and the app is Live.

Usage::

    uv run python scripts/subscribe_app.py            # subscribe (POST)
    uv run python scripts/subscribe_app.py --list     # only show what is subscribed
    uv run python scripts/subscribe_app.py --delete   # unsubscribe (rarely needed)

Reads ``META_ACCESS_TOKEN`` / ``META_WABA_ID`` from the environment. The token is
never printed: only a masked fingerprint, so a screenshot of the output is safe.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402


def subscribed_apps_url(settings: Settings) -> str:
    """``https://graph.facebook.com/v23.0/{WABA_ID}/subscribed_apps``."""
    return f"{settings.graph_base}/{settings.meta_waba_id}/subscribed_apps"


def auth_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.meta_access_token or ''}"}


def mask_token(token: str | None) -> str:
    """Enough to tell two tokens apart in a screenshot, useless to an attacker."""
    if not token:
        return "(ausente)"
    return f"{token[:6]}...{token[-4:]} ({len(token)} caracteres)"


def response_summary(resp: httpx.Response) -> str:
    """One-line pt-BR summary of a Graph response (error message included)."""
    try:
        data: Any = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    if isinstance(data, dict) and "error" in data:
        err = data.get("error") or {}
        return f"HTTP {resp.status_code} erro #{err.get('code')}: {err.get('message')}"
    return f"HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:400]}"


def subscribe(client: httpx.Client, settings: Settings) -> httpx.Response:
    return client.post(subscribed_apps_url(settings), headers=auth_headers(settings))


def list_subscriptions(client: httpx.Client, settings: Settings) -> httpx.Response:
    return client.get(subscribed_apps_url(settings), headers=auth_headers(settings))


def unsubscribe(client: httpx.Client, settings: Settings) -> httpx.Response:
    return client.delete(subscribed_apps_url(settings), headers=auth_headers(settings))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="subscribe_app.py",
        description="Inscreve o app da Meta nos webhooks da conta do WhatsApp (WABA).",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="apenas lista as inscricoes")
    group.add_argument("--delete", action="store_true", help="remove a inscricao do app")
    p.add_argument("--timeout", type=float, default=None)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()

    if not settings.meta_access_token:
        print("META_ACCESS_TOKEN nao esta definido no ambiente. Exporte o secrets.env primeiro.")
        return 2

    print(f"WABA ..... {settings.meta_waba_id}")
    print(f"token .... {mask_token(settings.meta_access_token)}")
    print(f"endpoint . {subscribed_apps_url(settings)}\n")

    timeout = args.timeout or settings.http_timeout_seconds
    try:
        with httpx.Client(timeout=timeout) as client:
            if args.list:
                resp = list_subscriptions(client, settings)
            elif args.delete:
                resp = unsubscribe(client, settings)
            else:
                resp = subscribe(client, settings)
    except httpx.HTTPError as exc:
        print(f"FALHA de rede: {exc}")
        return 2

    print(response_summary(resp))
    if resp.status_code >= 400:
        print(
            "\nDicas:\n"
            "  #200 / #10  -> o usuario do sistema precisa de whatsapp_business_management\n"
            "                 e do ativo WABA atribuido (Controle total).\n"
            "  #190        -> token invalido ou expirado: gere outro (README > rotacionar token).\n"
            "  #100        -> WABA_ID errado em META_WABA_ID."
        )
        return 1
    if not args.list:
        print("\nPronto. Confira com: uv run python scripts/subscribe_app.py --list")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
