"""Request-scoped dependencies shared by every router.

They live here — not in ``app.main`` — so that ``app.panel`` and ``app.pages``
can import them without a circular import: ``app.main`` imports those modules at
the end of :func:`app.main.create_app`, so anything they import from ``app.main``
would be reaching into a half-initialised module.

Usage in a router written by another builder::

    from fastapi import APIRouter, Depends, Request
    from sqlalchemy.orm import Session

    from app.db import get_session
    from app.deps import get_graph_client, is_same_origin
    from app.settings_store import SettingsStore, get_settings_store

    router = APIRouter()

    @router.get("/painel")                      # sync def: runs in the threadpool
    def home(request: Request,
             session: Session = Depends(get_session),
             store: SettingsStore = Depends(get_settings_store)):
        ...
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from app.whatsapp import GraphClient

# Anti-clickjacking, sent on every HTML response we own.
#
# The panel's CSRF defence is `is_same_origin` + the per-process nonce, and BOTH are
# defeated by framing: inside an attacker's iframe the operator's own click submits the
# panel's own form, so `Origin` is the panel's origin and the nonce is the correct one
# (HTTP Basic credentials are cached by the browser, so no cookie is needed either).
# One tricked click on a transparent overlay could flip "Ativado" off and silently stop
# every reminder. nginx sends the same pair, but the app must be safe when reached
# directly too (dev, or a future proxy change).
FRAME_DENY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def get_graph_client(request: Request) -> GraphClient | None:
    """The process-wide Graph client created at startup.

    ``None`` when ``META_ACCESS_TOKEN`` is unset — routers must handle that and
    tell the operator, in pt-BR, that the token is missing instead of crashing.
    """
    return getattr(request.app.state, "graph_client", None)


def is_same_origin(request: Request) -> bool:
    """Cheap CSRF guard for panel POSTs: Origin/Referer must match the request host.

    Enough for a one-user HTTP-Basic panel (the spec asks for "simple same-origin
    check + POST-only"); a missing Origin *and* Referer is treated as same-origin
    because some browsers omit both on same-site form posts.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    host = request.headers.get("host") or urlsplit(str(request.url)).netloc
    return urlsplit(origin).netloc == host
