"""FastAPI application: the two webhooks and /health.

The panel (``app.panel``) and the public pages (``app.pages``) are separate
modules written by other builders; :func:`include_extra_routers` mounts them
when present so the core runs alone. Endpoints are plain ``def`` (threadpool)
with a request-scoped session dependency; only the raw-body dependency is
``async`` because Starlette exposes the body as a coroutine.
"""

from __future__ import annotations

import hmac
import importlib
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__, clock, db
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_graph_client
from app.inbound import handle_meta_webhook
from app.kirvano import check_token, handle_event
from app.queries import worker_heartbeat_age
from app.whatsapp import GraphClient, verify_signature

log = logging.getLogger("app.main")

EXTRA_ROUTER_MODULES = ("app.panel", "app.pages")


# --- dependencies ------------------------------------------------------------------


async def get_raw_body(request: Request) -> bytes:
    """Async dependency so sync endpoints can see the exact bytes (needed for HMAC)."""
    return await request.body()


# --- app factory ---------------------------------------------------------------------


def include_extra_routers(app: FastAPI) -> list[str]:
    """Mount ``app.panel`` / ``app.pages`` if importable. Each must expose ``router``.

    Returns the list of modules that were mounted. ImportError → skipped (core runs alone).
    """
    mounted: list[str] = []
    for name in EXTRA_ROUTER_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            log.info("optional module %s not mounted: %s", name, exc)
            continue
        router = getattr(module, "router", None)
        if router is None:
            log.warning("module %s has no `router`; skipped", name)
            continue
        app.include_router(router)
        setup = getattr(module, "setup", None)
        if callable(setup):
            setup(app)
        mounted.append(name)
    return mounted


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        db.create_all()
        app.state.graph_client = GraphClient(settings) if settings.meta_configured else None
        if not settings.meta_app_secret:
            log.warning("META_APP_SECRET not set: Meta webhook signatures are NOT verified")
        if settings.kirvano_token_mode == "log":
            log.warning("KIRVANO_TOKEN_MODE=log: Kirvano webhooks are accepted without a token")
        yield
        client = getattr(app.state, "graph_client", None)
        if client is not None:
            client.close()

    app = FastAPI(
        title="PIX Recovery",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    # --- Kirvano -----------------------------------------------------------------

    @app.post("/webhooks/kirvano")
    def kirvano_webhook(
        request: Request,
        raw: bytes = Depends(get_raw_body),
        session: Session = Depends(get_session),
        settings: Settings = Depends(get_settings),
    ) -> JSONResponse:
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            log.warning("kirvano webhook: invalid JSON (%d bytes)", len(raw))
            return JSONResponse({"ok": False, "outcome": "invalid_json"}, status_code=200)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "outcome": "invalid_json"}, status_code=200)

        headers = dict(request.headers)
        check = check_token(settings, headers, body)
        if not check.accepted:
            log.warning("kirvano webhook rejected: %s (source=%s)", check.reason, check.source)
            return JSONResponse({"ok": False, "error": "invalid_token"}, status_code=401)

        result = handle_event(session, body, headers, settings=settings)
        return JSONResponse(
            {
                "ok": result.outcome != "error",
                "outcome": result.outcome,
                "event": result.event,
                "reason": result.reason,
            },
            status_code=200,
        )

    # --- Meta ----------------------------------------------------------------------

    @app.get("/webhooks/meta")
    def meta_verify(request: Request, settings: Settings = Depends(get_settings)):
        q = request.query_params
        mode = q.get("hub.mode")
        token = q.get("hub.verify_token") or ""
        challenge = q.get("hub.challenge") or ""
        expected = settings.meta_verify_token or ""
        if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
            return PlainTextResponse(challenge, status_code=200)
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhooks/meta")
    def meta_webhook(
        request: Request,
        raw: bytes = Depends(get_raw_body),
        session: Session = Depends(get_session),
        settings: Settings = Depends(get_settings),
        client: GraphClient | None = Depends(get_graph_client),
    ) -> JSONResponse:
        signature = request.headers.get("x-hub-signature-256")
        if settings.meta_app_secret:
            if not verify_signature(settings.meta_app_secret, raw, signature):
                log.warning("meta webhook: bad or missing signature")
                return JSONResponse({"ok": False, "error": "invalid_signature"}, status_code=403)
        else:
            log.warning("meta webhook: META_APP_SECRET unset, signature not verified")
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            log.warning("meta webhook: invalid JSON")
            return JSONResponse({"ok": False, "outcome": "invalid_json"}, status_code=200)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "outcome": "invalid_json"}, status_code=200)
        try:
            result = handle_meta_webhook(session, payload, client=client)
        except Exception:  # noqa: BLE001 - spec: always 200, log the error
            log.exception("meta webhook processing failed")
            return JSONResponse({"ok": False, "outcome": "error"}, status_code=200)
        return JSONResponse(
            {
                "ok": True,
                "statuses": result.statuses_updated,
                "messages": result.messages_stored,
                "opt_outs": result.opt_outs,
                "template_updates": result.template_updates,
            },
            status_code=200,
        )

    # --- health ----------------------------------------------------------------------

    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> JSONResponse:
        now = clock.utcnow()
        db_ok = True
        age: float | None = None
        try:
            session.execute(text("SELECT 1"))
            age = worker_heartbeat_age(session, now=now)
        except Exception as exc:  # noqa: BLE001
            db_ok = False
            log.error("health: db check failed: %s", exc)
        worker_ok = age is not None and age < 60
        body = {
            "status": "ok" if db_ok and worker_ok else "degraded",
            "db": "ok" if db_ok else "error",
            "worker_heartbeat_age_s": round(age, 1) if age is not None else None,
            "worker": "ok" if worker_ok else "stale",
            "version": __version__,
            "time": now.isoformat(),
        }
        return JSONResponse(body, status_code=200 if db_ok else 503)

    include_extra_routers(app)
    return app


app = create_app()
