"""Switch the live reminder to whichever template Meta approved.

Four variants were submitted so a rejection does not cost another ~20 h round
trip. They differ in the ways Meta plausibly objects to, so one being refused
says little about the others:

    v2  4 params, quick-reply + URL button   (the preferred one)
    v3  4 params, NO buttons                 (safest shape)
    v4  4 params, URL button only            (no quick reply)
    v5  2 params, NO buttons                 (minimal, closest to Meta's
                                              own "account alert" examples)

Each needs different runtime settings - the parameter list and the 0-based
index of the URL button - and those live in the settings table, so switching is
a database write, never a deploy.

    python -m scripts.usar_modelo --list          # status of every variant
    python -m scripts.usar_modelo --auto          # pick the best APPROVED one
    python -m scripts.usar_modelo --use v3        # force a specific variant

--auto prefers v2, then v4, then v3, then v5: buttons first (they convert
better), richer parameters before poorer ones.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from app.config import get_settings
from app.db import session_scope
from app.settings_store import SettingsStore

#: name -> (template_params, url_button_index). Must match what was submitted.
VARIANTS: dict[str, tuple[str, int]] = {
    "pix_pendente_v2": ("first_name,sale_id,amount,expiry", 1),
    "pix_pendente_v3": ("first_name,sale_id,amount,expiry", -1),
    "pix_pendente_v4": ("first_name,sale_id,amount,expiry", 0),
    "pix_pendente_v5": ("first_name,sale_id", -1),
}

#: best first - buttons beat no buttons, more data beats less
PREFERENCE = ["pix_pendente_v2", "pix_pendente_v4", "pix_pendente_v3", "pix_pendente_v5"]


def fetch_statuses(settings) -> dict[str, dict]:
    """Live status of every variant, straight from the Graph API."""
    base = f"{settings.meta_graph_base_url.rstrip('/')}/{settings.meta_graph_version}"
    url = f"{base}/{settings.meta_waba_id}/message_templates"
    r = httpx.get(
        url,
        params={"fields": "name,status,category,rejected_reason", "limit": 50},
        headers={"Authorization": f"Bearer {settings.meta_access_token}"},
        timeout=settings.http_timeout_seconds,
    )
    r.raise_for_status()
    out: dict[str, dict] = {}
    for t in r.json().get("data", []):
        if t.get("name") in VARIANTS:
            out[t["name"]] = t
    return out


def apply(name: str) -> None:
    params, button_index = VARIANTS[name]
    with session_scope() as session:
        store = SettingsStore(session)
        store.set_many(
            {
                "template_name": name,
                "template_language": "pt_BR",
                "template_params": params,
                "url_button_index": str(button_index),
            }
        )
    print(f"ativo: {name}")
    print(f"  parametros      : {params}")
    print(f"  botao de link   : {'nenhum' if button_index < 0 else f'indice {button_index}'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show the status of every variant")
    ap.add_argument("--auto", action="store_true", help="activate the best APPROVED variant")
    ap.add_argument("--use", choices=sorted(VARIANTS), help="activate a specific variant")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.meta_access_token:
        print("META_ACCESS_TOKEN nao configurado", file=sys.stderr)
        return 2

    if args.use:
        # Deliberately allowed even when PENDING: the operator may know the
        # approval landed a second ago. Sending with an unapproved template
        # fails with 132001 and is logged, so this cannot break anything silently.
        st = fetch_statuses(settings).get(args.use, {})
        if st.get("status") != "APPROVED":
            print(f"AVISO: {args.use} esta {st.get('status', 'DESCONHECIDO')}, nao APPROVED")
        apply(args.use)
        return 0

    statuses = fetch_statuses(settings)
    if args.list or not args.auto:
        print(f"{'MODELO':<20} {'STATUS':<10} {'CATEGORIA':<10} MOTIVO")
        for n in PREFERENCE:
            t = statuses.get(n)
            if not t:
                print(f"{n:<20} {'-':<10} {'-':<10} nao existe")
                continue
            print(
                f"{n:<20} {t.get('status', '?'):<10} {t.get('category', '?'):<10} "
                f"{t.get('rejected_reason') or '-'}"
            )
        if not args.auto:
            return 0

    for n in PREFERENCE:
        t = statuses.get(n)
        if t and t.get("status") == "APPROVED":
            print()
            apply(n)
            if t.get("category") != "UTILITY":
                print(f"  AVISO: categoria {t.get('category')} - custo por mensagem bem maior")
            return 0

    print("\nnenhuma variante APPROVED ainda - nada alterado", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
