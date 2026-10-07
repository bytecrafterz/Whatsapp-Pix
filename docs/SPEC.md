# PIX Recovery via WhatsApp — Build Spec (verified facts, 2026-09-08)

Everything here was verified against Meta / Kirvano / Hostinger official docs on 2026-09-08 unless marked (assumption). Build agents MUST follow it; where the spec says "unknown", write tolerant code and log.

## Goal
Kirvano checkout → customer generates PIX → webhook → after a configurable delay (default 10 min), if still unpaid and not expired and not opted out, send ONE WhatsApp Cloud API template message with a link to a hosted PIX page. If paid/expired/refused before firing → never send. Client (non-technical) changes delay / on-off / quiet hours in a tiny web panel, sees inbound replies there and can reply within the 24h window.

## Runtime / stack
- Python 3.12, FastAPI (sync endpoints are fine), SQLAlchemy 2.x ORM, PostgreSQL on the server (psycopg 3), SQLite for unit tests. Jinja2 server-rendered HTML, no JS frameworks. httpx for Graph API. `qrcode[pil]` to render the PIX QR server-side.
- Two systemd services on Ubuntu 24.04: `pix-api` (uvicorn, 127.0.0.1:8000, behind nginx + Let's Encrypt) and `pix-worker` (polling loop, every 5 s).
- Dev machine is Windows: run with `py -3.12` / `uv`. `uv venv .venv && uv pip install -r requirements.txt && uv run pytest -q` must pass.
- Timezone: store UTC (timezone-aware datetimes). Kirvano timestamps are NAIVE local `YYYY-MM-DD HH:MM:SS` → interpret as `America/Sao_Paulo` (assumption; verify on first real event, make configurable `KIRVANO_TZ`).
- Config from env (pydantic-settings). Secrets live in `/etc/pix-recovery/env` on the server (0600), `secrets.env` locally (never committed).

## Identifiers (real)
- WABA_ID = 958025707339789 · PHONE_NUMBER_ID = 1347340825121720 · Graph API version v23.0
- API domain: api.jornadaanjo.cloud · VPS 179.199.147.30 (Hostinger KVM, Ubuntu 24.04)
- Brand "Jornada com Meu Anjo"; legal entity CONNECT LT NEGOCIOS DIGITAIS LTDA, CNPJ 52.134.502/0001-09, Florianópolis/SC, e-mail LT.CONNECT@OUTLOOK.COM (public CNPJ record) — used only in the privacy page.

## Kirvano webhooks (official doc "Configurando Integração via Webhook", updated 2026-04-08)
- POST JSON to our URL. Merchant sets an optional **Token** in the panel; the transport (header name vs body field) is UNDOCUMENTED. Implement `KIRVANO_TOKEN_MODE=log|enforce`: in `log` mode accept everything and record header NAMES + which known fields were present (never values) for the first 50 requests in a `webhook_events.auth_debug` column; in `enforce` mode accept the token from any of: headers `x-kirvano-token`, `token`, `x-token`, `x-webhook-token`, `security-token`, `authorization` (raw or `Bearer `), or body fields `token` / `security_token`. Constant-time compare.
- Always answer 200 quickly; do the work synchronously but cheaply (DB only, no HTTP calls). Retry policy unpublished → assume duplicates; idempotency key = (event, sale_id) (plus `created_at` if present). Store every raw payload.
- Event enums seen in the payload `event` field, with `status`:
  - `PIX_GENERATED` "PIX gerado" → status `PENDING` (this schedules the reminder)
  - `PIX_EXPIRED` "PIX expirado" → status `CANCELED`; carries `checkout_url` = https://pay.kirvano.com/recovery/<uuid> (link to generate a new PIX)
  - `SALE_APPROVED` "Compra aprovada" → status `APPROVED`; `payment.finished_at`. Since 2026-10-07 it also starts the post-sale follow-up for every approved sale, card or PIX (`app/postsale.py`, README §5 "Mensagens pós-venda"); `SALE_REFUNDED` / `SALE_CHARGEBACK` cancel what is still scheduled
  - `SALE_REFUSED`, `SALE_REFUNDED`, `SALE_CHARGEBACK`, `BANK_SLIP_GENERATED`, `BANK_SLIP_EXPIRED`, `SUBSCRIPTION_*` (ignore)
  - `ABANDONED_CART` (no sale_id, only checkout_id) — since 2026-10-07 the trigger of the abandoned-cart flow (`app/cart.py`, README §5 "Recuperação de carrinho abandonado"); `PIX_GENERATED` and `SALE_APPROVED` also close matching carts
  - Unknown events: store raw, log, 200.
- **AUTHORITATIVE EXAMPLE: `tests/fixtures/kirvano_pix_generated.json`** — a REAL payload captured from the client's own webhook log on 2026-09-08 (sale `5LZEB2GJ`, fired 10/07/2026 17:05), PII replaced but every key/type/format byte-faithful. Parse against THIS, not the doc sample. Key facts it establishes, several of which contradict the official doc sample:
  1. **PIX validity on his checkout is 24 h**: `created_at "2026-07-10 17:05:30"` → `expires_at "2026-07-11 17:05:30"`. (The doc sample's 1 h is not his setting.) A 10-min reminder is therefore never near expiry, and `expires_too_soon` will rarely fire — but keep the clamp, the setting is merchant-editable.
  2. **`payment.qrcode_image` is NOT a URL** — it holds the same EMV copia-e-cola string as `payment.qrcode`. So there is NO usable image URL from Kirvano: the PIX page MUST render the QR server-side from `payment.qrcode`. Never emit `qrcode_image` into an `<img src>`. Treat it as a duplicate string and ignore it (store `pix_qr_image_url` only if the value actually starts with `http`).
  3. **Extra fields not in the docs, with MIXED snake_case and camelCase at the top level**: `ip`, `cookies{fbp,sck,ttp,gclid,fbclid}`, `utm{...}`, `fiscal{...}`, `fee`, `commission`, `contactEmail`, `couponDiscount`, `automaticDiscount`, `affiliateCommission`, `coproductionCommission`, `payment_method` (duplicate of `payment.method`), `event_description`, `type`. The parser must ignore unknown keys and never assume a naming convention.
  4. **`fiscal.total_value` is a NUMBER (97)** alongside the string `total_price "R$ 97,00"`. Prefer `fiscal.total_value` when present and numeric; fall back to parsing `total_price`. Also `fiscal.original_value`, `fiscal.net_value`.
  5. **`ip` is present** → store it on the order with the event timestamp as opt-in evidence (which checkout notice was shown, when, from where). Cheap and valuable if Meta or a customer ever disputes consent.
  6. **`customer.document` is the CPF** → LGPD data minimisation: do NOT persist it. Drop it at parse time; never log it. Same for `cookies` and ad identifiers (keep `utm` only if a column already exists; otherwise drop).
  7. **`customer.address` is an object of nulls** on PIX checkouts — never assume address data.
  8. `products[0]`: `name "A Jornada com meu Anjo"`, `offer_name "Padrao 97 Sem Order"`, `format "community"`, `price "R$ 97,00"`, real `id`/`offer_id`/`category` UUIDs, `is_order_bump false`. Product id `1c5f17f3-8682-4015-9837-05d2d7807757`, offer id `56619168-3b4c-4052-b28b-238288ad2190`.
  9. `sale_id` is an 8-char uppercase code (`5LZEB2GJ`) — that is what `{{2}}` "pedido" shows the customer. `checkout_id` likewise (`XE11BWM0`).
  10. `customer.phone_number "5551994697674"` = 55 + DDD 51 + 9 digits, no `+`. Confirms the normaliser.
  11. `contactEmail "jornadacommeuanjo@outlook.com"` is the merchant's support address — use it on the privacy page as the controller contact instead of inventing one.
- The webhook log's "Dados enviados" shows the BODY ONLY — no headers. So the security-token transport is still unverified; ship `KIRVANO_TOKEN_MODE=log` first exactly as specified, then switch to `enforce` once the first real delivery reveals it.
- The Kirvano log detail has a **"Reenviar webhook"** (resend) button. After our webhook exists and one real PIX has fired, the three test scenarios can be re-run by replaying that event instead of paying again — cheaper than repeated R$1 sales. Verify on the first use whether resend targets the original webhook only.
- Observed volume: ~10 `Pix gerado` events in 90 minutes on a normal ad day (traffic is Facebook ads, `utm_source "FB"`). A launch can plausibly exceed the 250-unique-recipients/24 h unverified cap — the daily-limit skip path is a real code path, not a formality.
- `total_price` is a formatted string "R$ 97,00" → parse to Decimal (handle "R$ 1.169,80"). `SALE_APPROVED` example in docs is credit card; for PIX assume same shape with `payment.method="PIX"` and `payment.finished_at`.
- `customer.phone_number` = digits, country code first, no "+", e.g. `5511987654321`. Normalize anyway: strip non-digits; if 10–11 digits assume BR and prefix 55; keep both the 13-digit (with 9) and 12-digit (without 9) variants for BR mobiles (DDD + 9 + 8 digits ↔ DDD + 8 digits).
- PIX expiry: `payment.expires_at` present; merchant-configurable per checkout (doc example = 60 min). Never hardcode.
- Product filter: the merchant selects products in the Kirvano webhook form; still ignore `products[].is_order_bump == true` when naming the product.
- Kirvano has NO public REST API and NO test/sandbox mode; tests are real R$1,00 PIX. We cannot query status — our DB of events is the only source of truth.

## WhatsApp Cloud API (Meta developer docs)
- Send: `POST https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages`, header `Authorization: Bearer {token}`, body:
```json
{"messaging_product":"whatsapp","to":"5549988760799","type":"template",
 "template":{"name":"pix_pendente_v2","language":{"code":"pt_BR"},
  "components":[
   {"type":"body","parameters":[{"type":"text","text":"Maria"},{"type":"text","text":"D2RP8RQ7"},{"type":"text","text":"97,00"},{"type":"text","text":"15/09 às 18:00"}]},
   {"type":"button","sub_type":"url","index":"1","parameters":[{"type":"text","text":"abc123"}]}]}}
```
  Quick-reply buttons need no parameters at send time. Button `index` is the button's position in the template (0-based, as a string); make it configurable `TEMPLATE_URL_BUTTON_INDEX` (default "1": quick reply at 0, URL at 1).
- Success response: `{"messaging_product":"whatsapp","contacts":[{"input":"55...","wa_id":"55..."}],"messages":[{"id":"wamid.HBg..."}]}` → store `wa_id` (canonical; may differ from what we sent) and message id.
- Text (free-form, only inside the 24h customer-service window after an inbound message): `{"messaging_product":"whatsapp","to":"...","type":"text","text":{"body":"..."}}`.
- Parameter rules (enforced at send time): values cannot contain newline, tab, or 4+ consecutive spaces → error `(#131009) Parameter value is not valid` (subcode 2494073). Sanitize: collapse whitespace, strip control chars, trim, cap length (~60 chars for name). Rendered body incl. values must stay < 1024 chars.
- Error mapping (top-level `error.code`):
  - 131009 param invalid → sanitize harder, retry once
  - 131026 message undeliverable / not a WhatsApp user → retry once with the alternate 9th-digit form; then fail `not_on_whatsapp`
  - 132000 param count mismatch → code bug, fail, alert
  - 132001 template does not exist / wrong language → fail, alert (use `pt_BR`)
  - 132012 param format mismatch, 132018 param issue → sanitize, retry once
  - 132015 template paused, 132016 template disabled → fail, mark template unavailable, alert
  - 131047 re-engagement (outside 24h window) → only for free-text replies; surface in panel
  - 131049 per-user marketing limit (only if template got classified Marketing) → do NOT retry for 24h
  - 131050 user opted out of marketing → record opt-out
  - 130429 / 131056 / 80007 rate or pair-rate limit → exponential backoff, retry later (max 3)
  - 190 / 401 invalid or expired token → alert, stop worker sends
- Webhook verification (GET): query `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge` → if token matches `META_VERIFY_TOKEN` return `hub.challenge` as plain text 200, else 403.
- Webhook POST: header `X-Hub-Signature-256: sha256=<hex hmac-sha256(app_secret, raw_body)>`; if `META_APP_SECRET` is set, verify with constant-time compare, else log a warning. Always 200 (even on internal error; log it). Payload `{"object":"whatsapp_business_account","entry":[{"id":"<WABA_ID>","changes":[{"field":"messages","value":{...}}]}]}`.
  - `value.statuses[]`: `{id, status: sent|delivered|read|failed, timestamp, recipient_id, errors:[{code,title,message,error_data:{details}}], conversation, pricing}` → update message row.
  - `value.messages[]`: `{from, id, timestamp, type: text|button|interactive|image|..., text:{body}, button:{payload,text}, interactive:{type:"button_reply", button_reply:{id,title}}}` + `value.contacts[]:[{wa_id, profile:{name}}]` → store inbound, detect opt-out, mark service window open (24h from timestamp).
  - field `message_template_status_update`: `{event: APPROVED|REJECTED|PAUSED|PENDING_DELETION|DISABLED..., message_template_id, message_template_name, message_template_language, reason}`; field `template_category_update`: `{message_template_name, previous_category, new_category}`; `phone_number_quality_update`, `phone_number_name_update`, `account_update` → store, show in panel.
- Opt-out: inbound text matching (case/accent-insensitive) `sair|parar|pare|stop|cancelar|nao quero|não quero|descadastrar` or a button reply whose title/payload is "Não quero receber" → insert into `opt_outs` (by wa_id and by phone variants), cancel that phone's scheduled jobs, reply once (free-form, window is open): "Pronto, você não receberá mais avisos da Jornada com Meu Anjo. Se precisar de ajuda com seu pedido, é só responder aqui." Panel must allow manual opt-out add/remove.
- Messaging limit: 250 unique recipients per rolling 24h per business portfolio until business verification (then 2,000 → auto-scales). Keep a rolling-24h counter of distinct recipients; if reached, skip with reason `daily_limit` (log) — never queue past it. Configurable `DAILY_RECIPIENT_LIMIT` default 250.
- One reminder per order, ever. Never a second nudge (quality rating → template auto-pause).
- Pricing (2026, approx): Brazil Utility ≈ R$0,035 / Marketing ≈ R$0,32 per delivered template; free-form replies inside the 24h window free today. Show the template's actual category in the panel (from template status webhooks or `GET /{WABA_ID}/message_templates?name=...&fields=name,status,category,language`).

## Template (client creates it in WhatsApp Manager; code only references it)
- Name `pix_pendente_v2`, language `pt_BR`, category Utility (may be reclassified Marketing by Meta — code must not assume).
- Body: `Olá {{1}}, o pagamento do pedido {{2}} no valor de R$ {{3}} está pendente. Seu código PIX segue válido até {{4}}. Se já realizou o pagamento, desconsidere esta mensagem.`
- Footer: `Para não receber mais avisos, responda SAIR.`
- Buttons: [0] Quick reply `Não quero receber` · [1] URL (dynamic) `https://api.jornadaanjo.cloud/p/{{1}}` label `Ver código PIX`.
- Params at send: {{1}} customer first name (sanitized, fallback "cliente"), {{2}} `sale_id`, {{3}} amount formatted `97,00`, {{4}} expiry formatted `dd/mm às HH:MM` in America/Sao_Paulo; URL suffix = order.page_token (urlsafe, 16+ chars).
- Make template name / language / button index / param order configurable via settings so switching to a fallback template is a panel change, not a deploy.

## Scheduling & the race condition (the core promise)
- On `PIX_GENERATED`: upsert `orders` by `sale_id` (status pending, customer, phone variants, amount, pix code, qr image url, expires_at, page_token, product name, checkout_id, offer_id). If settings.enabled and not opted out and no job exists for the order: create `recovery_jobs` row, `run_at = now + delay_minutes`; if `expires_at` known and `run_at > expires_at - 3 min`: `run_at = expires_at - 3 min` if that is still in the future, else skip with reason `expires_too_soon`. If `run_at` falls in quiet hours (settings, default 22:00–08:00 America/Sao_Paulo): postpone to end of quiet hours (fire-time checks will skip it if the PIX expired meanwhile).
- On `SALE_APPROVED` / `PIX_EXPIRED` / `SALE_REFUSED` / `SALE_REFUNDED` / `SALE_CHARGEBACK`: update order status (paid / expired / refused / refunded / chargeback) and cancel any scheduled job for that order (`state='scheduled'` → `cancelled`, reason). Store `paid_at`.
- Worker loop (every 5 s): in ONE transaction select due jobs `state='scheduled' AND run_at <= now` with `FOR UPDATE SKIP LOCKED` (Postgres; no-op on SQLite), lock the order row too, re-check ALL of: job still scheduled, order.status == pending, not opted out (any phone variant / wa_id), `expires_at` is None or > now + 60 s, not in quiet hours, daily limit not reached, template configured. If any fails → `skipped` with reason (or postponed for quiet hours). Else set `state='sending'`, `claimed_at`, commit. Then send (HTTP, outside the transaction). Then `sent` (+ wa_id, message id, sent_at) or `failed` (+ error code/text, attempts). Retryable errors → back to `scheduled` with `run_at = now + backoff` and `attempts += 1` (max 3), but re-check expiry each time.
- Because the webhook handler that marks an order paid also cancels the job inside the same transaction with the order row locked, and the worker locks the order row before flipping to `sending`, a payment landing "in the same minute" can never race a send on Postgres. Document this in code comments and README.
- Idempotency: unique (order_id) on recovery_jobs; unique (event, sale_id, created_at) on webhook_events (nullable-safe).

## Data model (minimum)
`webhook_events`(id, source kirvano|meta, external_key unique-ish, event, payload JSON, headers_meta JSON, received_at, processed_at, error) · `orders`(id, sale_id unique, checkout_id, offer_id, product_name, customer_name, customer_email, customer_document?, phone_raw, phone_e164 (13-digit form), phone_alt (12-digit form), wa_id, amount_cents, currency, pix_code, pix_qr_image_url, pix_expires_at, checkout_recovery_url, status pending|paid|expired|refused|refunded|chargeback|unknown, paid_at, page_token unique, created_at, updated_at) · `recovery_jobs`(id, order_id unique, run_at, state scheduled|sending|sent|cancelled|skipped|failed, reason, attempts, claimed_at, sent_at, wa_message_id, error_code, error_text) · `messages`(id, direction in|out, order_id?, wa_id, phone, wa_message_id unique, kind template|text, body, template_name, status sent|delivered|read|failed|received, status_updated_at, error_code, error_text, created_at) · `opt_outs`(id, wa_id?, phone, source button|text|manual|meta_131050, note, created_at) · `settings`(key primary, value text, updated_at) with defaults `enabled=true, delay_minutes=10, quiet_start=22:00, quiet_end=08:00, daily_recipient_limit=250, template_name=pix_pendente_v2, template_language=pt_BR, url_button_index=1, checkout_url=<KIRVANO_CHECKOUT_URL>` · `template_status`(name, language, status, category, reason, updated_at) · `contacts`(wa_id primary, phone, profile_name, last_inbound_at, last_outbound_at).

## HTTP surface
- `POST /webhooks/kirvano` · `GET|POST /webhooks/meta` · `GET /p/{page_token}` (PIX page) · `GET /privacidade` (privacy policy, pt-BR) · `GET /health` (200 + db ok + worker heartbeat age) · `GET /painel` and sub-routes behind HTTP Basic auth (`PANEL_USER`, `PANEL_PASSWORD`).
- PIX page: shows order id, amount, product, QR (rendered server-side from `pix_code` with `qrcode`, fallback to Kirvano `qrcode_image` URL), the copia-e-cola in a read-only textarea with a "Copiar código" button (navigator.clipboard with fallback), expiry countdown, and state: pending → instructions; paid → "Pagamento confirmado"; expired → "Este PIX expirou" + button to `checkout_recovery_url` or `settings.checkout_url` to generate a new one. Mobile-first, pt-BR, no external assets, no tracking, no PII beyond first name and order id. Unknown token → 404 page. Cache-Control: no-store.
- Panel (pt-BR, mobile-friendly, plain CSS): Início (counts: pendentes agora, lembretes enviados hoje/7d, pagos após lembrete, cancelados por pagamento, expirados, falhas; last 50 orders with job state), Configurações (form: ativado, minutos de espera, horário silencioso, limite diário, nome/idioma do modelo, índice do botão, link do checkout), Conversas (list by contact: last message, window open/closed badge; conversation view with reply form → sends free text if window open, else explains), Descadastros (list, add by phone, remove), Eventos (last 100 webhook events, raw JSON collapsible), Modelo (template status/category as last seen). CSRF: simple same-origin check + POST-only. No JS frameworks.

## Deploy (Ubuntu 24.04 on Hostinger KVM)
- `deploy/setup_server.sh` (run once as root, idempotent): apt install python3.12-venv postgresql nginx certbot python3-certbot-nginx ufw fail2ban; system user `pixapp`; code at `/opt/pix-recovery`; DB `pixrecovery` with generated password; `/etc/pix-recovery/env` 0600 root:pixapp built from a provided env file; venv + requirements; nginx server block for `$API_DOMAIN` proxying to 127.0.0.1:8000 (client_max_body_size 1m, proxy headers, HSTS after TLS); `certbot --nginx -d $API_DOMAIN --non-interactive --agree-tos -m $LETSENCRYPT_EMAIL --redirect`; systemd units `pix-api.service` and `pix-worker.service` (Restart=always, EnvironmentFile, User=pixapp); `ufw allow OpenSSH, 80/tcp, 443/tcp; ufw --force enable`; fail2ban sshd jail; SSH hardening ONLY if `/root/.ssh/authorized_keys` contains the provided key: write `/etc/ssh/sshd_config.d/99-hardening.conf` with `PasswordAuthentication no`, `PermitRootLogin prohibit-password`, `PubkeyAuthentication yes`, `sshd -t` then `systemctl restart ssh`. Print a summary and a `curl https://$API_DOMAIN/health` check.
- `deploy/deploy.sh` (from Windows Git Bash): `scp`/`tar` the code to the server (exclude .venv, secrets.env, .git, tests), `pip install -r requirements.txt` in the venv, restart both services, tail status. Also `deploy/check.sh`: DNS → IP, ports 22/80/443 open, `/health` 200.
- Hostinger-specific: the hPanel managed firewall (if a group is active) must allow 22/80/443 — ufw alone is not enough; Hostinger "Reset SSH" in hPanel rewrites sshd_config (undoes hardening) — mention in README.
- Meta app must be switched to Live mode and needs a Privacy Policy URL (`https://api.jornadaanjo.cloud/privacidade`) before it can message real customers. After webhook config, run `POST /v23.0/{WABA_ID}/subscribed_apps` with the token (provide `scripts/subscribe_app.py`). Provide `scripts/check_template.py` (prints template status/category) and `scripts/send_test.py --to 55... --order <sale_id>`.

## Tests (pytest, SQLite in-memory)
FIRST test to write: load `tests/fixtures/kirvano_pix_generated.json` verbatim, POST it to `/webhooks/kirvano`, and assert the full happy path (order created with the right amount/phone/expiry/product/page_token, job scheduled at now+delay, CPF and cookies NOT persisted, unknown keys ignored). Every parser change must keep that test green.
Also cover: amount parsing ("R$ 169,80", "R$ 1.169,80"), naive timestamp → UTC via KIRVANO_TZ, phone normalization + alternate form, PIX_GENERATED creates order + job at now+delay, delay clamped before expiry, expires_too_soon skip, SALE_APPROVED cancels job, PIX_EXPIRED cancels job, worker skips when paid between schedule and fire, worker skips expired, quiet-hours postpone, daily limit skip, opt-out via text and via button cancels job and blocks scheduling, duplicate webhook idempotent, Meta GET challenge, Meta signature verify (valid/invalid/missing), status webhook updates message row, template payload builder (params order, URL button index), param sanitizer (newline/tab/spaces/length), error mapping (131026 → alternate number retry; 131049 → no retry), token acceptance in enforce mode from header and body, `/p/{token}` renders pending/paid/expired/404, panel requires auth.

## Non-goals (do not build)
Coexistence/BSP integration, second nudges, marketing campaigns, Kirvano API polling (none exists), multi-tenant.
