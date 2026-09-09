"""Every HTML response must refuse to be framed.

The panel's CSRF defence is `is_same_origin` + a per-process nonce, and framing
defeats both at once: inside an attacker's iframe the operator's own click submits
the panel's own form, so `Origin` is the panel's origin and the nonce is the right
one — and HTTP Basic credentials are cached by the browser, so no cookie is needed.
One tricked click on a transparent overlay could flip "Ativado" off (reminders stop
silently) or add/remove an opt-out.
"""

from __future__ import annotations

import base64

import pytest

from app.kirvano import handle_event
from app.models import Order
from tests.conftest import kirvano_payload

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:panel-pw").decode()}


PANEL_PATHS = [
    "/painel",
    "/painel/configuracoes",
    "/painel/conversas",
    "/painel/descadastros",
    "/painel/eventos",
    "/painel/modelo",
]


@pytest.mark.parametrize("path", PANEL_PATHS)
def test_panel_pages_are_not_framable(client, path):
    resp = client.get(path, headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert resp.headers["Cache-Control"] == "no-store"


def test_panel_error_and_redirect_responses_carry_the_headers(client):
    # A failed CSRF check renders the error page; it must not be framable either.
    resp = client.post("/painel/descadastros", headers=AUTH, data={"nonce": "wrong", "phone": "1"})
    assert resp.status_code == 403
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_public_pages_are_not_framable(client, session, settings, frozen_clock):
    from sqlalchemy import select

    from app import db

    with db.session_scope() as s:
        handle_event(s, kirvano_payload(), settings=settings)
        token = s.execute(select(Order.page_token)).scalar_one()

    for path in (f"/p/{token}", "/privacidade", "/p/does-not-exist"):
        resp = client.get(path)
        assert resp.headers["X-Frame-Options"] == "DENY", path
        assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'", path


def test_nginx_site_sends_the_same_headers():
    """The proxy must carry them too — the app is only reachable through it in prod."""
    from pathlib import Path

    conf = Path(__file__).resolve().parents[1] / "deploy" / "nginx-pix-api.conf"
    text = conf.read_text(encoding="utf-8")
    assert 'add_header X-Frame-Options "DENY" always;' in text
    assert "add_header Content-Security-Policy \"frame-ancestors 'none'\" always;" in text
