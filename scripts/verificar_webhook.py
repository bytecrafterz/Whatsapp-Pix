"""Check that our Meta webhook is still ours - run after any third-party connects.

Why this exists
---------------
A third-party inbox (SAK, Digisac, Chatwoot...) needs a system-user token to
attach itself to the WhatsApp account. Whoever holds a token for an app can
rewrite that app's webhook callback. If the tool is handed OUR token instead of
its own, it points our app at its servers and the PIX automation silently stops
receiving Kirvano/Meta events - the reminders just quietly never fire.

Baseline captured 2026-09-09, before SAK was connected:

    app       1165568608850474 "Jornada API"
    callback  https://api.jornadaanjo.cloud/webhooks/meta
    fields    messages, message_template_status_update, template_category_update,
              message_template_quality_update, phone_number_quality_update,
              phone_number_name_update, account_update
    WABA 958025707339789 subscribed apps: Jornada API only

A second app appearing in the subscribed list is EXPECTED and healthy - that is
the third-party doing it correctly. What is NOT acceptable is our callback_url
changing, or fields disappearing.

    python -m scripts.verificar_webhook          # report
    python -m scripts.verificar_webhook --fix    # restore our callback + fields
"""

from __future__ import annotations

import argparse
import sys

import httpx

from app.config import get_settings

EXPECTED_CALLBACK = "https://api.jornadaanjo.cloud/webhooks/meta"
EXPECTED_FIELDS = {
    "messages",
    "message_template_status_update",
    "template_category_update",
    "message_template_quality_update",
    "phone_number_quality_update",
    "phone_number_name_update",
    "account_update",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="restore the callback and fields")
    args = ap.parse_args()

    s = get_settings()
    if not (s.meta_app_id and s.meta_app_secret and s.meta_access_token):
        print("META_APP_ID / META_APP_SECRET / META_ACCESS_TOKEN nao configurados", file=sys.stderr)
        return 2

    base = f"{s.meta_graph_base_url.rstrip('/')}/{s.meta_graph_version}"
    app_token = f"{s.meta_app_id}|{s.meta_app_secret}"
    problems: list[str] = []

    r = httpx.get(
        f"{base}/{s.meta_app_id}/subscriptions",
        params={"access_token": app_token},
        timeout=s.http_timeout_seconds,
    )
    r.raise_for_status()
    subs = [d for d in r.json().get("data", []) if d.get("object") == "whatsapp_business_account"]

    if not subs:
        problems.append("nosso app NAO tem assinatura de webhook - eventos nao chegam")
    else:
        sub = subs[0]
        callback = sub.get("callback_url", "")
        fields = {f["name"] for f in sub.get("fields", [])}
        print(f"callback : {callback}")
        print(f"ativo    : {sub.get('active')}")
        print(f"campos   : {', '.join(sorted(fields))}")
        if callback != EXPECTED_CALLBACK:
            problems.append(f"CALLBACK ALTERADO: {callback!r} (esperado {EXPECTED_CALLBACK!r})")
        if not sub.get("active"):
            problems.append("assinatura inativa")
        missing = EXPECTED_FIELDS - fields
        if missing:
            problems.append(f"campos faltando: {', '.join(sorted(missing))}")

    r2 = httpx.get(
        f"{base}/{s.meta_waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {s.meta_access_token}"},
        timeout=s.http_timeout_seconds,
    )
    r2.raise_for_status()
    apps = r2.json().get("data", [])
    print("\napps ligados a conta do WhatsApp:")
    ours = False
    for a in apps:
        info = a.get("whatsapp_business_api_data", a)
        name, app_id = info.get("name", "?"), str(info.get("id", "?"))
        mark = "  <- o nosso" if app_id == str(s.meta_app_id) else "  (terceiro - normal)"
        if app_id == str(s.meta_app_id):
            ours = True
        print(f"  {name} ({app_id}){mark}")
    if not ours:
        problems.append("NOSSO APP FOI REMOVIDO da conta do WhatsApp")

    if not problems:
        print("\nOK - o webhook continua nosso, nada foi alterado.")
        return 0

    print("\nPROBLEMAS:")
    for p in problems:
        print(f"  - {p}")
    if not args.fix:
        print("\nrode de novo com --fix para restaurar")
        return 1

    print("\nrestaurando...")
    fix = httpx.post(
        f"{base}/{s.meta_app_id}/subscriptions",
        data={
            "object": "whatsapp_business_account",
            "callback_url": EXPECTED_CALLBACK,
            "verify_token": s.meta_verify_token or "",
            "fields": ",".join(sorted(EXPECTED_FIELDS)),
            "access_token": app_token,
        },
        timeout=s.http_timeout_seconds,
    )
    print(fix.text)
    sub_again = httpx.post(
        f"{base}/{s.meta_waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {s.meta_access_token}"},
        timeout=s.http_timeout_seconds,
    )
    print(sub_again.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
