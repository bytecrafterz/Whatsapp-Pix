# ARCHITECTURE — the contract for the panel / pages / deploy builders

Core built by the architect agent, 2026-09-08. `docs/SPEC.md` is the product contract;
**this file is the code contract**. If the two disagree, SPEC wins and this file is wrong —
say so instead of working around it.

What already exists and is tested (154 tests, `uv run pytest -q` green,
`uv run ruff check app tests` clean):

- the Kirvano webhook (parse → order upsert → schedule/cancel, idempotent),
- the Meta webhook (verification, signature, statuses, inbound, opt-out, template status),
- scheduling + the claim/race protocol, the sending worker,
- `/health`, config, models, settings store, phone normalisation, formatting, Jinja env.

What is **not** built and is yours: `GET /p/{page_token}` (PIX page), `GET /privacidade`,
`GET /painel` + sub-routes, `deploy/*.sh`, `scripts/*.py`.

---

## 1. Run it

```bash
uv venv .venv                       # once
uv pip install -r requirements.txt
uv run pytest -q                    # must stay green — do not break the core tests
uv run ruff check app tests
uv run ruff format app tests

uv run uvicorn app.main:app --reload --port 8000   # API
uv run python -m app.worker                        # worker (--once for a single tick)
```

Config comes from the **environment only** (no `.env` is read implicitly). Locally:
export the values from `secrets.env`; on the server systemd injects `/etc/pix-recovery/env`.
Never read, print or copy `secrets.env` from code.

---

## 2. Module map

| Module | Responsibility | May you edit it? |
|---|---|---|
| `app/config.py` | `Settings` (pydantic-settings), `get_settings()` | add a var only if SPEC needs it |
| `app/clock.py` | `utcnow()` — the single source of "now" (tests monkeypatch it) | no |
| `app/db.py` | engine, `sessionmaker`, `get_session` dependency, `session_scope()` | no |
| `app/models.py` | every ORM table | add columns only with a very good reason |
| `app/settings_store.py` | typed get/set over the `settings` table | you drive the Configurações form from it |
| `app/phone.py` | BR phone normalisation (13-digit ↔ 12-digit) | no |
| `app/kirvano.py` | Kirvano payload parsing, token check, `handle_event` | no |
| `app/scheduling.py` | `compute_run_at`, schedule/cancel, claim protocol, job transitions | no |
| `app/whatsapp.py` | Graph client, template payload, sanitiser, error mapping, webhook parsing | no |
| `app/inbound.py` | Meta webhook processing, opt-out auto-reply, `send_free_text` | no |
| `app/optout.py` | opt-out add/remove/query, cancel jobs of a phone | no |
| `app/worker.py` | polling loop, delivery, retries, batch kill switches | no |
| `app/retention.py` | the 12-month purge promised on `/privacidade` (run by `scripts/purge_old_data.py`) | no |
| `app/main.py` | FastAPI app: 4 routes + `include_extra_routers` | no (add your routes in your own module) |
| `app/deps.py` | `get_graph_client`, `is_same_origin`, `FRAME_DENY_HEADERS` | no |
| `app/queries.py` | read-only helpers for the panel/pages | **yes — add helpers here, not SQL in routes** |
| `app/format.py` | `fmt_brl`, `fmt_dt_sp`, `first_name` | yes (additive) |
| `app/templating.py` | shared Jinja2 environment | yes (additive filters) |
| `app/alerts.py` | operator alerts table | yes (additive) |
| **`app/panel.py`** | HTTP Basic panel — **you build this** | — |
| **`app/pages.py`** | `/p/{token}` + `/privacidade` — **you build this** | — |

Import direction: `panel`/`pages` → `queries`, `settings_store`, `inbound`, `optout`,
`whatsapp`, `format`, `templating`, `deps`, `db`, `models`. **Never import `app.main`**
(it imports you; see §7).

---

## 3. Data model (`app/models.py`)

All datetime columns are `UTCDateTime`: **aware UTC in, aware UTC out**, on SQLite and
Postgres alike. Binding a naive datetime raises — convert with
`dt.replace(tzinfo=UTC)` / `clock.ensure_aware(dt)` before writing, and render with
`fmt_dt_sp()` which converts to America/Sao_Paulo.

```
webhook_events   id, source('kirvano'|'meta'), external_key, event, sale_id, payload(JSON),
                 headers_meta(JSON), auth_debug(JSON), received_at, processed_at, outcome, error
                 unique(source, external_key)   ← idempotency: "EVENT|sale_id|created_at"

orders           id, sale_id*, checkout_id, offer_id, product_name, customer_name, customer_email,
                 customer_document (ALWAYS NULL — see §9), phone_raw, phone_e164 (13-digit),
                 phone_alt (12-digit), wa_id, amount_cents, currency, pix_code,
                 pix_qr_image_url (usually NULL — see §9), pix_expires_at, checkout_recovery_url,
                 status('pending'|'paid'|'expired'|'refused'|'refunded'|'chargeback'|'unknown'),
                 paid_at, consent_ip, consent_at, page_token*, created_at, updated_at
                 .phone_variants  → [phone_e164, phone_alt] without Nones

recovery_jobs    id, order_id* (unique = ONE reminder per order, ever), run_at,
                 state('scheduled'|'sending'|'sent'|'cancelled'|'skipped'|'failed'), reason,
                 attempts, claimed_at, sent_at, sent_to, wa_message_id, error_code, error_text,
                 created_at, updated_at

messages         id, direction('in'|'out'), order_id, wa_id, phone, wa_message_id*,
                 kind('template'|'text'), body, template_name,
                 status('sent'|'delivered'|'read'|'failed'|'received'), status_updated_at,
                 error_code, error_text, created_at

opt_outs         id, wa_id, phone* (one row per phone VARIANT), source('button'|'text'|'manual'|
                 'meta_131050'), note, created_at

settings         key*, value(text), updated_at            ← read/write only via SettingsStore
template_status  (name, language)*, status, category, reason, template_id, updated_at
contacts         wa_id*, phone, profile_name, last_inbound_at, last_outbound_at
worker_heartbeat id(=1), beat_at, pid, hostname, note
alerts           id, level('info'|'warning'|'error'), code, message, context(JSON),
                 created_at, resolved_at
```

`*` = unique/primary. Enums live in `app.models` as `StrEnum`s (`OrderStatus`, `JobState`,
`MessageDirection`, `MessageKind`, `MessageStatus`, `OptOutSource`) — compare with
`.value` strings, that is what is stored.

### Reason codes you must label in pt-BR

`job.reason` (panel Início / order detail). `app.queries.job_state_label()` and
`order_status_label()` already translate *states*; reasons are yours:

| reason | when | suggested pt-BR |
|---|---|---|
| `clamped_to_expiry` | fire time pulled back to expiry − 3 min | antecipado (PIX expira logo) |
| `quiet_hours` | postponed out of the silent window | adiado (horário silencioso) |
| `expires_too_soon` | PIX would expire before we could send | PIX expiraria antes |
| `disabled` | panel switch off at fire time (job kept, re-checked) | pausado (lembretes desligados) |
| `paid` / `expired` / `refused` / `refunded` / `chargeback` | cancelled by a Kirvano event | cancelado: pago / expirado / … |
| `opted_out` | customer said SAIR / pressed the button / Meta 131050 | cliente pediu para não receber |
| `order_paid`, `order_expired`, … | fire-time re-check found the order no longer pending | pedido já pago / expirado |
| `daily_limit` | 250-recipient rolling 24 h cap reached | limite diário atingido |
| `template_unavailable` / `template_not_configured` | template paused/disabled/missing | modelo indisponível |
| `no_phone` | no usable number on the order | sem telefone válido |
| `not_on_whatsapp` | 131026 on both number forms | número não tem WhatsApp |
| `retry_<code>` | queued for a backoff retry | nova tentativa |
| `max_attempts` | 3 failed attempts | falhou após 3 tentativas |
| `token_invalid` | 190/401 — sends paused | token do WhatsApp inválido |
| `stale_sending` | worker died mid-send; outcome unknown, never resent | envio interrompido |
| `worker_stopping` | claim released untouched on SIGTERM / batch kill switch | reagendado (o serviço estava reiniciando) |
| `record_failed` | outcome could not be recorded and NO request had been sent | reagendado (falha ao gravar o resultado) |
| `marketing_limit_24h` | 131049 | limite de marketing por usuário |

---

## 4. The functions you may call

Everything below is stable API. Nothing here commits unless the docstring says so —
**your route owns the transaction**: mutate, then `session.commit()`.

### Session & settings

```python
from app.db import get_session, session_scope          # dependency / context manager
from app.config import Settings, get_settings
from app.settings_store import SettingsStore, get_settings_store, SettingValueError, SettingDef

store.enabled                 -> bool
store.delay_minutes           -> int
store.quiet_start/quiet_end   -> datetime.time
store.daily_recipient_limit   -> int
store.template_name           -> str
store.template_language       -> str
store.url_button_index        -> int      # -1 = template has no URL button
store.template_params         -> list[str]
store.checkout_url            -> str      # fallback link for an expired PIX
store.get(key) / get_raw(key) -> str / str|None      # get_raw is None when never set in the panel
store.set(key, value)         -> str      # validates, raises SettingValueError (pt-BR message)
store.set_many({...})         -> dict     # validates ALL first, then writes — use this for the form
store.as_dict()               -> dict[str, str]
SettingsStore.definitions()   -> Iterable[SettingDef]   # key, kind, label (pt-BR), help, min, max
```

`SettingDef` drives the Configurações form: `kind` is `"bool" | "int" | "time" | "str"`,
`label`/`help` are already pt-BR. Keys: `enabled, delay_minutes, quiet_start, quiet_end,
daily_recipient_limit, template_name, template_language, url_button_index, template_params,
checkout_url`. Precedence per key: DB row → env var → hard-coded default.

### Read-only queries (`app.queries`)

```python
dashboard_counts(session, *, now=None) -> DashboardCounts
    # .pending_now .scheduled .sent_today .sent_7d .paid_after_reminder
    # .cancelled_paid .expired .failed
recent_orders(session, limit=50)        -> list[tuple[Order, RecoveryJob | None]]
order_by_page_token(session, token)     -> Order | None      # the PIX page lookup
order_by_sale_id(session, sale_id)      -> Order | None
job_for_order(session, order)           -> RecoveryJob | None
recent_events(session, limit=100, source=None) -> list[WebhookEvent]
conversations(session, limit=100)       -> list[Contact]
conversation_messages(session, wa_id, limit=200) -> list[Message]   # oldest first
last_message_for(session, wa_id)        -> Message | None
template_status_for(session, store)     -> TemplateStatus | None
worker_heartbeat_age(session, *, now=None) -> float | None   # seconds, None = never beat
job_state_label(state) / order_status_label(status) -> str   # pt-BR
start_of_today_sp(now)                  -> datetime          # midnight in São Paulo, as UTC
```

Need another read? Add it to `app/queries.py` with a test — do not write SQL in a route.

### Opt-outs (`app.optout`)

```python
is_opted_out(session, phones, wa_id=None) -> bool      # phones = order.phone_variants
add_opt_out(session, *, phone, wa_id, source, note=None, now=None) -> list[OptOut]
    # writes one row per phone variant; returns ONLY the rows it created
    # (empty list = already opted out → do not reply again). source: 'manual' from the panel.
remove_opt_out(session, phone_or_wa_id) -> int         # deletes every variant, returns count
list_opt_outs(session, limit=500)       -> list[OptOut]
cancel_jobs_for_phones(session, phones, wa_id, *, reason="opted_out", now=None) -> list[RecoveryJob]
```

Panel rule: after `add_opt_out` from the Descadastros form, also call
`cancel_jobs_for_phones(...)`, then commit.

### Conversations / replies (`app.inbound`)

```python
window_open(contact, now=None)     -> bool               # 24 h customer-service window
window_closes_at(contact)          -> datetime | None    # show as a countdown/badge
send_free_text(session, client, wa_id, body, *, order_id=None, force=False) -> SendResult
    # raises WindowClosedError (pt-BR message, show it as-is) when the window is closed;
    # records the outbound Message on success. Does NOT commit.
record_outbound_message(session, *, wa_id, phone, message_id, kind, body,
                        template_name=None, order_id=None, now=None) -> Message
```

`client` comes from `Depends(get_graph_client)` (`app.deps`) and is `None` when
`META_ACCESS_TOKEN` is unset — render a pt-BR warning instead of crashing.

### WhatsApp (`app.whatsapp`) — the panel rarely needs more than this

```python
GraphClient(settings).send_text(to, body)               -> SendResult   # prefer send_free_text
GraphClient(settings).fetch_template_status(name, language=None) -> dict | None
    # GET /{WABA_ID}/message_templates?name=... → {'name','status','category','language','id'}
    # feed it into inbound.upsert_template_status(...) to refresh the Modelo page
SendResult: .ok .to .wa_id .message_id .error(.code .message .summary()) .http_status
```

### Alerts (`app.alerts`)

```python
open_alerts(session, limit=50)    -> list[Alert]        # show on Início
resolve_alert(session, alert_id)  -> bool
record_alert(session, code, message, *, level="error", context=None, dedupe=False) -> Alert
# dedupe=True refreshes the open alert with the same code instead of adding a row:
# use it for one-condition problems (token_invalid, template_unavailable), never
# for per-job failures.
```

---

## 5. Jinja2 environment (`app/templating.py`)

```python
from app.templating import templates          # fastapi.templating.Jinja2Templates
return templates.TemplateResponse(request, "painel/inicio.html", {"...": ...})
```

- Templates live in **`app/templates/`**; put yours in `app/templates/painel/…` and
  `app/templates/pages/…`. Autoescape is on.
- **Every page must `{% extends "base.html" %}`.** `base.html` is a mobile-first pt-BR
  shell with all CSS inline (no external assets, no JS frameworks, no tracking — a hard
  requirement for the customer-facing pages).
- Blocks: `title`, `head`, `header`, `nav`, `content`, `footer`, `scripts`.
  Override `nav` for the panel menu; override `header`/`footer` on the PIX page if the
  brand header is not wanted.
- CSS classes already available: `.card`, `.btn`, `.btn.secondary`, `.badge{.ok|.warn|.bad}`,
  `.muted`, `.flash{.ok|.bad}`, `.table-wrap` (horizontal scroll for tables).
- Filters: `brl` (cents → `1.169,80`), `brl_symbol` (→ `R$ 1.169,80`), `dt_sp`
  (→ `15/09 às 18:00`), `dt_sp_full` (→ `15/09/2026 18:00`).
  Globals: `first_name`, `app_version`.
- Python-side: `from app.format import fmt_brl, fmt_dt_sp, fmt_dt_sp_full, first_name`.

---

## 6. Env vars

Runtime (server `/etc/pix-recovery/env`, local `secrets.env`):

| Var | Default | Notes |
|---|---|---|
| `APP_ENV` | `dev` | `dev` / `test` / `prod` |
| `LOG_LEVEL` | `INFO` | |
| `DATABASE_URL` | `sqlite:///./dev.db` | server: `postgresql+psycopg://…` |
| `API_DOMAIN` | `api.jornadaanjo.cloud` | used to build the public base URL |
| `PUBLIC_BASE_URL` | *(derived)* | overrides `https://{API_DOMAIN}` |
| `BUSINESS_TZ` | `America/Sao_Paulo` | quiet hours + human dates |
| `KIRVANO_WEBHOOK_TOKEN` | — | required in `enforce` mode |
| `KIRVANO_TOKEN_MODE` | `log` | `log` first; flip to `enforce` after the first real delivery |
| `KIRVANO_TZ` | `America/Sao_Paulo` | how naive Kirvano timestamps are read |
| `KIRVANO_CHECKOUT_URL` | — | default for `settings.checkout_url` |
| `KIRVANO_PIX_EXPIRY_MINUTES` | — | only a fallback when `payment.expires_at` is missing |
| `AUTH_DEBUG_MAX_ROWS` | `50` | log-mode header-name capture budget |
| `META_ACCESS_TOKEN` | — | system-user permanent token |
| `META_APP_SECRET` | — | when unset, webhook signatures are NOT verified (logged) |
| `META_VERIFY_TOKEN` | — | `hub.verify_token` for the GET handshake |
| `META_PHONE_NUMBER_ID` | `1347340825121720` | |
| `META_WABA_ID` | `958025707339789` | |
| `META_BUSINESS_ID`, `META_APP_ID` | — | informational |
| `META_GRAPH_VERSION` | `v23.0` | |
| `META_GRAPH_BASE_URL` | `https://graph.facebook.com` | |
| `HTTP_TIMEOUT_SECONDS` | `15` | |
| `PANEL_USER` | `admin` | HTTP Basic |
| `PANEL_PASSWORD` | — | **panel must refuse to serve when unset** |
| `REMINDER_DELAY_MINUTES` | `10` | default for `settings.delay_minutes` |
| `DAILY_RECIPIENT_LIMIT` | `250` | Meta unverified-tier cap |
| `QUIET_START` / `QUIET_END` | `22:00` / `08:00` | |
| `TEMPLATE_NAME` / `TEMPLATE_LANGUAGE` | `pix_pendente_v2` / `pt_BR` | |
| `TEMPLATE_URL_BUTTON_INDEX` | `1` | 0-based button position, `-1` = no URL button |
| `TEMPLATE_PARAMS` | `first_name,sale_id,amount,expiry` | body `{{1}}…{{n}}` order |
| `WORKER_POLL_SECONDS` | `5` | |
| `WORKER_BATCH_SIZE` | `20` | jobs claimed per tick |
| `WORKER_HEARTBEAT_FILE` | — | optional file heartbeat next to the DB row |
| `WORKER_MAX_ATTEMPTS` | `3` | |
| `WORKER_TOKEN_PAUSE_MINUTES` | `10` | pause after a 190/401 |

Deploy-only keys in `secrets.env` (`VPS_*`, `SSH_KEY_PATH`, …) are ignored by `Settings`
(`extra="ignore"`), so one env file can feed both the app and the deploy scripts.

---

## 7. How to add your routers

`app/main.py` calls `include_extra_routers(app)` at the end of `create_app()`. It imports
`app.panel` and `app.pages`, and for each mounts `module.router` (an `APIRouter`) and calls
`module.setup(app)` if defined. A missing module is skipped with a log line, so the core
still runs alone. Contract:

```python
# app/pages.py
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.queries import order_by_page_token
from app.templating import templates

router = APIRouter()          # REQUIRED name

@router.get("/p/{page_token}", response_class=HTMLResponse)
def pix_page(page_token: str, request: Request, session: Session = Depends(get_session)):
    order = order_by_page_token(session, page_token)
    ...
    response = templates.TemplateResponse(request, "pages/pix.html", {"order": order})
    response.headers["Cache-Control"] = "no-store"      # required by SPEC
    return response
```

Rules:

1. **Sync `def`, never `async def`** — the session dependency is sync and endpoints run in
   the threadpool. (`app.main.get_raw_body` is the only async dependency; you do not need it.)
2. One session per request via `Depends(get_session)`; commit explicitly.
3. Do not import `app.main` (circular). Shared dependencies live in `app/deps.py`.
4. Never call the Graph API from inside an open write transaction — commit first, then send
   (`send_free_text` is fine: it flushes but the caller commits after).
5. All user-facing strings pt-BR; code, comments, identifiers, log messages English.
6. `/p/{token}`: `Cache-Control: no-store`, no PII beyond first name + order id, unknown
   token → 404 page. Render the QR **server-side** from `order.pix_code` with `qrcode[pil]`
   (embed as a `data:` URI). Do **not** use `order.pix_qr_image_url` as an image source
   unless it is non-NULL — see §9.
7. Panel auth: HTTP Basic against `settings.panel_user` / `settings.panel_password`, using
   `hmac.compare_digest` on both fields, returning 401 with
   `WWW-Authenticate: Basic realm="Painel"`. If `panel_password` is unset, return 503 with a
   pt-BR message — never serve an unauthenticated panel. Mutations are POST-only and should
   check `app.deps.is_same_origin(request)` (the SPEC's "simple same-origin check").

---

## 8. The race-condition guarantee (do not weaken it)

The one promise of this system is: *nobody who already paid gets a reminder.*

1. The Kirvano webhook, in one transaction: `SELECT … FROM orders WHERE sale_id=? FOR UPDATE`
   (with `populate_existing`, so the row is genuinely re-read) → set status/paid_at →
   cancel the order's `scheduled` job → COMMIT.
2. The worker, in one transaction per due job: lock the **order** row first, then the job row
   with `FOR UPDATE SKIP LOCKED`, re-check *everything* (job still scheduled, order still
   pending, not opted out, expiry > now + 60 s, quiet hours, daily limit, template usable),
   flip to `sending`, COMMIT. Only then does it call Meta.

Both paths take the **order lock first**, so they serialise on it and cannot deadlock. A
payment landing "in the same minute" either commits first (worker then sees `paid` and skips)
or arrives after the `sending` flip (the message is already in flight; the webhook leaves the
job alone and only records the payment). `SKIP LOCKED` lets several workers run without
double-sending. On SQLite (tests/dev) SQLAlchemy drops `FOR UPDATE` — SQLite is single-writer,
so the invariant holds anyway.

Corollaries for you: never set `recovery_jobs.state` from the panel, never insert a second job
for an order (a unique constraint stops you), never "resend" a reminder — one nudge per order,
ever, is what keeps the template from being auto-paused by Meta.

---

## 9. Facts from the REAL payload that will bite you

From `tests/fixtures/kirvano_pix_generated.json` (a real capture; `tests/test_kirvano_fixture.py`
locks this behaviour in):

1. **`payment.qrcode_image` is NOT a URL** — it repeats the EMV copia-e-cola string. The parser
   stores `pix_qr_image_url` only when the value really starts with `http`, so for this merchant
   it is **NULL**. The PIX page must render the QR itself from `order.pix_code`.
2. **The CPF (`customer.document`) is never persisted** — dropped at parse time, stripped from
   the stored raw payload, and `orders.customer_document` stays NULL. Do not add it to any page,
   export or log. Ad cookies are stripped the same way.
3. **PIX validity is 24 h on this checkout** (not the doc's 1 h), so a 10-minute reminder is
   never near expiry — but the clamp stays, the merchant can change the setting.
4. **`fiscal.total_value` is a number** and wins over the `"R$ 97,00"` string for the amount.
5. **`ip` is stored** as `orders.consent_ip` + `consent_at` — opt-in evidence. Show it only in
   the panel (operator-only), never on a public page.
6. Top-level keys mix `snake_case` and `camelCase` and include undocumented fields; the parser
   ignores unknown keys and never assumes a convention.
7. `sale_id` / `checkout_id` are 8-char uppercase codes (`5LZEB2GJ`) — that is the "pedido"
   the customer sees.
8. `customer.address` is an object of nulls on PIX checkouts. There is no address data.
9. The merchant's support address, for the privacy page, is `jornadacommeuanjo@outlook.com`
   (`contactEmail` in the payload); the controller is CONNECT LT NEGOCIOS DIGITAIS LTDA,
   CNPJ 52.134.502/0001-09, Florianópolis/SC.

---

## 10. HTTP surface

| Route | Owner | Notes |
|---|---|---|
| `POST /webhooks/kirvano` | core | 200 always (except 401 on a bad token in `enforce` mode) |
| `GET /webhooks/meta` | core | `hub.challenge` as text/plain 200, else 403 |
| `POST /webhooks/meta` | core | 403 on a bad/missing signature when `META_APP_SECRET` is set; otherwise always 200 |
| `GET /health` | core | `{status, db, worker, worker_heartbeat_age_s, version, time}`; 503 only if the DB is down |
| `GET /p/{page_token}` | **pages** | no-store, 404 page on unknown token |
| `GET /privacidade` | **pages** | pt-BR privacy policy |
| `GET /painel`, `/painel/…` | **panel** | HTTP Basic; Início, Configurações, Conversas, Descadastros, Eventos, Modelo |

---

## 11. Testing conventions

`tests/conftest.py` gives you, ready to use:

- `settings` — `Settings` built from a fixed test environment (enforce mode, fake Meta token);
- `engine` / `session` — in-memory SQLite (StaticPool) with all tables created, wired into
  `app.db` so anything calling `get_sessionmaker()` sees the same database;
- `client` — `TestClient(create_app(settings))` (your routers are mounted automatically once
  `app/panel.py` / `app/pages.py` exist);
- `store` — a `SettingsStore` on the test session;
- `frozen_clock` — patches `app.clock.utcnow`; `.set(dt)` / `.advance(minutes=…)`;
- builders: `kirvano_payload(...)`, `meta_status_payload(...)`, `meta_text_payload(...)`,
  `meta_button_payload(...)`, `graph_success()`, `graph_error(code)`, `dumps(obj)`,
  and `GRAPH_MESSAGES_URL` for `respx.post(...)`.

Mock all HTTP with `respx` (`@respx.mock`); never let a test hit the network. Add your tests
as `tests/test_panel*.py` / `tests/test_pages*.py`. The whole suite must stay green — if a core
test fails because of your change, the change is wrong until proven otherwise.
