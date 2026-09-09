"""Print the status and the CATEGORY of the WhatsApp message templates.

``GET /{WABA_ID}/message_templates?name=...&fields=name,status,category,language,id``

Why it matters: the reminder only sends while the template is APPROVED, and the
category decides the price (Utility ~ R$0,035 vs Marketing ~ R$0,32 per message)
and the rules (a Marketing template hits the per-user limit 131049 and honours
131050 opt-outs). Meta may re-classify a template at any time, so check it before
a launch and whenever sends start failing with 132001/132015/132016.

Usage::

    uv run python scripts/check_template.py                 # the configured template
    uv run python scripts/check_template.py --all           # every template of the WABA
    uv run python scripts/check_template.py --name pix_pendente_v3 --language pt_BR
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings  # noqa: E402

FIELDS = "name,status,category,language,id,quality_score,rejected_reason"

# pt-BR reading of each status, for the operator.
STATUS_HINTS: dict[str, str] = {
    "APPROVED": "aprovado, pode enviar",
    "PENDING": "em analise pela Meta (costuma levar minutos)",
    "IN_APPEAL": "em recurso",
    "REJECTED": "rejeitado: corrija e reenvie (veja rejected_reason)",
    "PAUSED": "pausado por qualidade: os envios falham com #132015",
    "DISABLED": "desativado pela Meta: os envios falham com #132016",
    "PENDING_DELETION": "marcado para exclusao",
}


def templates_url(settings: Settings) -> str:
    return f"{settings.graph_base}/{settings.meta_waba_id}/message_templates"


def auth_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.meta_access_token or ''}"}


def fetch_templates(
    client: httpx.Client, settings: Settings, *, name: str | None = None, limit: int = 50
) -> httpx.Response:
    params: dict[str, Any] = {"fields": FIELDS, "limit": limit}
    if name:
        params["name"] = name
    return client.get(templates_url(settings), params=params, headers=auth_headers(settings))


def select_templates(
    data: Any, *, name: str | None = None, language: str | None = None
) -> list[dict[str, Any]]:
    """Filter ``{"data": [...]}`` by name/language. Tolerates a missing/odd body."""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if name and item.get("name") != name:
            continue
        if language and item.get("language") != language:
            continue
        out.append(item)
    return out


def format_template(item: dict[str, Any]) -> str:
    """One-line pt-BR description of a template row."""
    status = str(item.get("status") or "?").upper()
    category = str(item.get("category") or "?").upper()
    hint = STATUS_HINTS.get(status, "status desconhecido")
    line = (
        f"{item.get('name', '?')} [{item.get('language', '?')}]  "
        f"status={status} ({hint})  categoria={category}"
    )
    if item.get("rejected_reason") and status == "REJECTED":
        line += f"  motivo={item['rejected_reason']}"
    return line


def category_warning(items: list[dict[str, Any]]) -> str | None:
    """Warn when Meta re-classified the template as Marketing (10x the price)."""
    for item in items:
        if str(item.get("category") or "").upper() == "MARKETING":
            return (
                "ATENCAO: a Meta classificou o modelo como MARKETING.\n"
                "  - preco ~ R$0,32 por mensagem (Utility ~ R$0,035);\n"
                "  - vale o limite por usuario (#131049) e o opt-out de marketing (#131050).\n"
                "  Peca revisao em business.facebook.com/business-support-home >\n"
                "  Template Category Updates (prazo de 60 dias)."
            )
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check_template.py",
        description="Mostra status e categoria dos modelos de mensagem da WABA.",
    )
    p.add_argument("--name", default=None, help="padrao: TEMPLATE_NAME do ambiente")
    p.add_argument("--language", default=None, help="ex.: pt_BR (padrao: TEMPLATE_LANGUAGE)")
    p.add_argument("--all", action="store_true", help="lista todos os modelos da conta")
    p.add_argument("--timeout", type=float, default=None)
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()

    if not settings.meta_access_token:
        print("META_ACCESS_TOKEN nao esta definido no ambiente. Exporte o secrets.env primeiro.")
        return 2

    name = None if args.all else (args.name or settings.template_name)
    language = None if args.all else (args.language or settings.template_language)

    print(f"WABA ..... {settings.meta_waba_id}")
    print(f"modelo ... {name or '(todos)'} {language or ''}\n")

    timeout = args.timeout or settings.http_timeout_seconds
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = fetch_templates(client, settings, name=name)
    except httpx.HTTPError as exc:
        print(f"FALHA de rede: {exc}")
        return 2

    try:
        data = resp.json()
    except ValueError:
        print(f"HTTP {resp.status_code}: resposta nao e JSON\n{resp.text[:300]}")
        return 1

    if resp.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        print(f"HTTP {resp.status_code} erro #{err.get('code')}: {err.get('message')}")
        return 1

    # `name` is already filtered server-side; re-filtering also applies --language.
    items = select_templates(data, name=name, language=language)
    if not items:
        print("Nenhum modelo encontrado com esse nome/idioma.")
        print("Confira o nome no Gerenciador do WhatsApp > Modelos de mensagem,")
        print("ou ajuste 'Nome do modelo' no painel (Configuracoes).")
        return 1

    for item in items:
        print("  " + format_template(item))
    warning = category_warning(items)
    if warning:
        print("\n" + warning)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
