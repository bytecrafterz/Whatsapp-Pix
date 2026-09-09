"""PIX recovery via WhatsApp — application package.

Module map (see docs/ARCHITECTURE.md for the full contract):

- config          env-driven settings (pydantic-settings)
- clock           single source of "now" (patched in tests)
- db              engine / session factory / FastAPI dependency
- models          SQLAlchemy ORM tables
- settings_store  typed get/set over the `settings` table
- phone           Brazilian phone normalisation
- kirvano         Kirvano webhook parsing + event handling (order upsert, schedule/cancel)
- scheduling      run_at computation, job claim/mark helpers (race-condition core)
- whatsapp        Graph API client, payload builder, sanitiser, error classification
- inbound         Meta webhook processing (statuses, inbound messages, opt-out, template status)
- optout          opt-out helpers shared by inbound/worker/panel
- worker          polling loop (`python -m app.worker`)
- main            FastAPI app (webhooks + health) and `include_extra_routers` hook
- deps            request dependencies shared with app.panel / app.pages (no cycles)
- format          pt-BR rendering helpers (fmt_brl, fmt_dt_sp)
- templating      Jinja2 environment shared by panel/pages
- queries         read-only query helpers for panel/pages
- alerts          operator alerts table helper
"""

__version__ = "0.1.0"
