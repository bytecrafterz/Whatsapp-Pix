"""Polling worker: ``python -m app.worker``.

Every ``WORKER_POLL_SECONDS`` (default 5):
  1. write a heartbeat (table row id=1 + optional file) for ``/health``;
  2. fail jobs stuck in ``sending`` (a previous worker died mid-request);
  3. claim due jobs under lock (``scheduling.claim_due_jobs`` — the full re-check
     happens there, inside one transaction);
  4. for each claimed job, call the Graph API **outside** any transaction and
     then record the outcome (sent / failed / requeued) in a short transaction.

SIGTERM/SIGINT set a stop flag; the current send finishes, any job still claimed
but untouched is handed back to ``scheduled``, and the loop exits — so systemd's
``Restart=always`` never interrupts an in-flight send nor strands a claim.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, db
from app.alerts import record_alert
from app.cart import (
    ClaimedCartJob,
    claim_due_cart_jobs,
    reap_stale_cart_sending,
    requeue_cart_job,
)
from app.config import Settings, get_settings
from app.inbound import record_outbound_message, upsert_template_status
from app.models import (
    Cart,
    CartJob,
    MessageKind,
    OptOutSource,
    Order,
    PostSale,
    PostSaleJob,
    WorkerHeartbeat,
)
from app.optout import add_opt_out, cancel_jobs_for_phones
from app.postsale import (
    ClaimedPostSaleJob,
    claim_due_post_sale_jobs,
    reap_stale_post_sale_sending,
    requeue_post_sale_job,
)
from app.scheduling import (
    DISABLED_RECHECK,
    AnyJob,
    ClaimedJob,
    claim_due_jobs,
    mark_failed,
    mark_sent,
    postpone,
    reap_stale_sending,
    requeue,
)
from app.settings_store import SettingsStore, StepConfig
from app.whatsapp import (
    ALERT_CODES,
    ErrorAction,
    GraphClient,
    SendResult,
    classify_error,
    fail_reason_for,
    step_template_preview,
    template_preview,
)

log = logging.getLogger("app.worker")


@dataclass
class DeliveryOutcome:
    """What happened while trying to deliver one reminder (possibly two HTTP calls)."""

    result: SendResult
    action: ErrorAction | None  # None when result.ok
    attempts: list[SendResult] = field(default_factory=list)
    reason: str | None = None  # final failure reason override
    alt_tried: bool = False
    hard_tried: bool = False

    @property
    def request_issued(self) -> bool:
        """True unless we can PROVE no HTTP request ever left this process.

        Only the "no phone on the order" path returns a result without touching the
        network. A network-level failure (timeout, reset) is counted as issued: the
        request may well have reached Meta, and "one reminder per order, ever"
        forbids gambling on it.
        """
        return any(a.to for a in self.attempts)


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
        client: GraphClient | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # An INJECTED factory belongs to the caller (tests, scripts): the worker
        # commits through those sessions but never closes them. Sessions it opens
        # itself are closed at the end of every tick so connections are not leaked.
        self._session_factory = session_factory
        self._owns_sessions = session_factory is None
        self.client = client or GraphClient(self.settings)
        self.poll_seconds = poll_seconds or self.settings.worker_poll_seconds
        self.paused_until: datetime | None = None
        # Set by _apply on a 132015/132016 and cleared at the top of every tick: the
        # template Meta just refused must not be re-tried for the rest of THIS batch,
        # but the next tick re-reads template_status (which _apply updated) instead of
        # trusting a stale in-process flag.
        self.template_blocked = False
        # Same idea for the cart and post-sale templates, per NAME: one paused template
        # must not stop the PIX reminders, nor the steps that use another template.
        self.step_templates_blocked: set[str] = set()
        self._stop = False
        self._warned_unconfigured = False

    # --- lifecycle ----------------------------------------------------------------

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        return db.get_sessionmaker()()

    def stop(self, *_args: object) -> None:
        log.info("stop requested; finishing current tick")
        self._stop = True

    def install_signal_handlers(self) -> None:
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):  # not in main thread / unsupported on this OS
                pass

    def run_forever(self) -> None:
        self.install_signal_handlers()
        log.info(
            "worker started poll=%ss batch=%s", self.poll_seconds, self.settings.worker_batch_size
        )
        while not self._stop:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the loop alive, systemd restarts on crash
                log.exception("worker tick failed")
            # Sleep in small slices so SIGTERM is honoured within ~0.5 s.
            while not self._stop and time.monotonic() - started < self.poll_seconds:
                time.sleep(0.5)
        log.info("worker stopped")

    # --- one tick ---------------------------------------------------------------------

    def heartbeat(self, session: Session, now: datetime, note: str | None = None) -> None:
        session.merge(
            WorkerHeartbeat(
                id=1, beat_at=now, pid=os.getpid(), hostname=socket.gethostname()[:128], note=note
            )
        )
        session.commit()
        if self.settings.worker_heartbeat_file:
            try:
                Path(self.settings.worker_heartbeat_file).write_text(now.isoformat())
            except OSError as exc:  # pragma: no cover - filesystem trouble is not fatal
                log.warning("heartbeat file write failed: %s", exc)

    def _kill_switch(self, now: datetime) -> tuple[datetime, str] | None:
        """``(retry_at, reason)`` when the rest of the batch must NOT be sent.

        A 190/401 (dead token) or a 132015/132016 (template paused/disabled) hit by an
        earlier job in the SAME batch applies to every remaining job: the credential
        or the template is broken, not that one message. The claims are already
        committed, so they have to be handed back to ``scheduled`` explicitly.
        """
        if self.paused_until is not None and now < self.paused_until:
            return self.paused_until, "token_invalid"
        if self.template_blocked:
            return now + DISABLED_RECHECK, "template_unavailable"
        if self._stop:
            # Shutting down: an untouched claim is worth more back in `scheduled`
            # (the restarted worker sends it) than left in `sending` for the reaper.
            return now, "worker_stopping"
        return None

    def _step_kill_switch(self, now: datetime, step: StepConfig) -> tuple[datetime, str] | None:
        """:meth:`_kill_switch` for cart and post-sale steps: per-template blocking."""
        if self.paused_until is not None and now < self.paused_until:
            return self.paused_until, "token_invalid"
        if step.template_name in self.step_templates_blocked:
            return now + DISABLED_RECHECK, "template_unavailable"
        if self._stop:
            return now, "worker_stopping"
        return None

    def run_once(self, now: datetime | None = None) -> int:
        """Process one tick; returns the number of jobs claimed."""
        now = now or clock.utcnow()
        session = self._session()
        self.template_blocked = False
        self.step_templates_blocked = set()
        try:
            if self.paused_until and now < self.paused_until:
                self.heartbeat(session, now, note="paused_token_invalid")
                return 0
            self.heartbeat(session, now)
            reap_stale_sending(session, now=now)
            reap_stale_cart_sending(session, now=now)
            reap_stale_post_sale_sending(session, now=now)
            if not self.client.configured:
                if not self._warned_unconfigured:
                    log.error("META_ACCESS_TOKEN not configured — worker will not send")
                    self._warned_unconfigured = True
                return 0
            store = SettingsStore(session, self.settings)
            claimed = claim_due_jobs(session, store, now=now, limit=self.settings.worker_batch_size)
            for item in claimed:
                blocked = self._kill_switch(now)
                if blocked is not None:
                    retry_at, reason = blocked
                    postpone(session, item.job, retry_at, reason=reason, now=now)
                    session.commit()
                    continue
                try:
                    self._process(session, store, item, now)
                except Exception:  # noqa: BLE001 - one bad job must not abandon the batch
                    # Letting this escape left every REMAINING claim in `sending` with
                    # zero HTTP attempts made; the reaper then failed them as
                    # `stale_sending` and, because a reminder is never resent, they
                    # were lost for good.
                    log.exception("job %s aborted; continuing with the batch", item.job.id)
            # Abandoned-cart steps go AFTER the PIX reminders: a pending payment is
            # worth more than a lost cart when the daily contact limit is close.
            cart_claimed = claim_due_cart_jobs(
                session, store, now=now, limit=self.settings.worker_batch_size
            )
            for cart_item in cart_claimed:
                blocked = self._step_kill_switch(now, cart_item.step)
                if blocked is not None:
                    retry_at, reason = blocked
                    postpone(session, cart_item.job, retry_at, reason=reason, now=now)
                    session.commit()
                    continue
                try:
                    self._process_cart(session, store, cart_item, now)
                except Exception:  # noqa: BLE001 - same isolation as the PIX batch
                    log.exception("cart job %s aborted; continuing", cart_item.job.id)
            # Post-sale last: the customer already paid, so when the daily contact limit
            # is close a pending PIX or a lost cart is worth more than a thank-you.
            post_claimed = claim_due_post_sale_jobs(
                session, store, now=now, limit=self.settings.worker_batch_size
            )
            for post_item in post_claimed:
                blocked = self._step_kill_switch(now, post_item.step)
                if blocked is not None:
                    retry_at, reason = blocked
                    postpone(session, post_item.job, retry_at, reason=reason, now=now)
                    session.commit()
                    continue
                try:
                    self._process_post_sale(session, post_item, now)
                except Exception:  # noqa: BLE001 - same isolation as the PIX batch
                    log.exception("post-sale job %s aborted; continuing", post_item.job.id)
            return len(claimed) + len(cart_claimed) + len(post_claimed)
        finally:
            if self._owns_sessions:
                session.close()
            else:
                # Leave no half-open transaction on a session we do not own.
                session.rollback()

    def _process(
        self, session: Session, store: SettingsStore, item: ClaimedJob, now: datetime
    ) -> None:
        self._send_and_record(
            session,
            item.job,
            deliver=lambda: self._deliver(item.order, store),
            record=lambda outcome: self._apply(session, store, item, outcome, now),
            now=now,
        )

    def _process_cart(
        self, session: Session, store: SettingsStore, item: ClaimedCartJob, now: datetime
    ) -> None:
        self._send_and_record(
            session,
            item.job,
            deliver=lambda: self._deliver_cart(item, store),
            record=lambda outcome: self._apply_cart(session, store, item, outcome, now),
            now=now,
        )

    def _process_post_sale(self, session: Session, item: ClaimedPostSaleJob, now: datetime) -> None:
        self._send_and_record(
            session,
            item.job,
            deliver=lambda: self._deliver_via(
                lambda to=None, hard=False: self.client.send_post_sale_template(
                    item.sale, item.step, to=to, hard=hard
                ),
                alt=item.sale.phone_alt,
            ),
            record=lambda outcome: self._apply_post_sale(session, item, outcome, now),
            now=now,
        )

    def _send_and_record(
        self,
        session: Session,
        job: AnyJob,
        *,
        deliver: Callable[[], DeliveryOutcome],
        record: Callable[[DeliveryOutcome], None],
        now: datetime,
    ) -> None:
        # HTTP happens here, with NO open transaction (claim already committed).
        outcome = deliver()
        try:
            record(outcome)
            session.commit()
        except Exception:
            session.rollback()
            log.exception("failed to record outcome for job %s", job.id)
            if not outcome.request_issued:
                # Nothing ever reached Meta, so releasing the claim cannot duplicate a
                # message; a fresh transaction puts the job back in `scheduled`.
                try:
                    postpone(session, job, now, reason="record_failed", now=now)
                    session.commit()
                    return
                except Exception:  # noqa: BLE001 - DB still broken; fall through
                    session.rollback()
                    log.exception("could not release job %s back to scheduled", job.id)
            # A request DID go out (or we cannot tell): the job stays in `sending` and
            # the stale-sending reaper fails it later. Never resend — the customer may
            # already have the message.
            raise

    # --- delivery -------------------------------------------------------------------------

    def _deliver(self, order: Order, store: SettingsStore) -> DeliveryOutcome:
        return self._deliver_via(
            lambda to=None, hard=False: self.client.send_template(order, store, to=to, hard=hard),
            alt=order.phone_alt,
        )

    def _deliver_cart(self, item: ClaimedCartJob, store: SettingsStore) -> DeliveryOutcome:
        coupon = store.cart_coupon
        return self._deliver_via(
            lambda to=None, hard=False: self.client.send_cart_template(
                item.cart, item.step, coupon, to=to, hard=hard
            ),
            alt=item.cart.phone_alt,
        )

    def _deliver_via(self, send: Callable[..., SendResult], *, alt: str | None) -> DeliveryOutcome:
        """One send plus at most one corrective retry (other number form / harder sanitising).

        ``send(to=None, hard=False)`` posts one template; it is the only thing that
        differs between a PIX reminder and a cart step.
        """
        first = send()
        attempts = [first]
        if first.ok:
            return DeliveryOutcome(first, None, attempts)
        action = classify_error(first.error)

        if action == ErrorAction.RETRY_ALT_NUMBER and alt and alt != first.to:
            # 131026: the 13-digit form is not on WhatsApp; try the 12-digit form once.
            second = send(to=alt)
            attempts.append(second)
            if second.ok:
                return DeliveryOutcome(second, None, attempts, alt_tried=True)
            action2 = classify_error(second.error)
            if action2 == ErrorAction.RETRY_ALT_NUMBER:
                return DeliveryOutcome(
                    second, ErrorAction.FAIL, attempts, "not_on_whatsapp", alt_tried=True
                )
            return DeliveryOutcome(second, action2, attempts, alt_tried=True)

        if action == ErrorAction.SANITIZE_RETRY:
            second = send(hard=True)
            attempts.append(second)
            if second.ok:
                return DeliveryOutcome(second, None, attempts, hard_tried=True)
            action2 = classify_error(second.error)
            if action2 == ErrorAction.SANITIZE_RETRY:
                return DeliveryOutcome(
                    second, ErrorAction.FAIL, attempts, "param_invalid", hard_tried=True
                )
            return DeliveryOutcome(second, action2, attempts, hard_tried=True)

        if action == ErrorAction.RETRY_ALT_NUMBER:
            return DeliveryOutcome(first, ErrorAction.FAIL, attempts, "not_on_whatsapp")
        return DeliveryOutcome(first, action, attempts)

    # --- recording ------------------------------------------------------------------------

    def _apply(
        self,
        session: Session,
        store: SettingsStore,
        item: ClaimedJob,
        outcome: DeliveryOutcome,
        now: datetime,
    ) -> None:
        job, order = item.job, item.order
        res = outcome.result
        if res.ok:
            mark_sent(
                session, job, wa_id=res.wa_id, message_id=res.message_id, sent_to=res.to, now=now
            )
            if res.wa_id:
                order.wa_id = res.wa_id
            order.updated_at = now
            record_outbound_message(
                session,
                wa_id=res.wa_id or res.to,
                phone=res.to,
                message_id=res.message_id,
                kind=MessageKind.TEMPLATE.value,
                body=template_preview(store, res.params),
                template_name=store.template_name,
                order_id=order.id,
                now=now,
            )
            log.info("reminder sent order=%s to=%s id=%s", order.sale_id, res.to, res.message_id)
            return

        err = res.error
        code = err.code_str if err else "unknown"
        text = err.summary() if err else "unknown error"
        action = outcome.action or ErrorAction.FAIL
        reason = outcome.reason or fail_reason_for(err)
        log.warning(
            "reminder failed order=%s code=%s action=%s: %s", order.sale_id, code, action, text
        )

        if action == ErrorAction.OPT_OUT:
            add_opt_out(
                session,
                phone=res.to or order.phone_e164,
                wa_id=order.wa_id,
                source=OptOutSource.META_131050.value,
                note=text[:200],
                now=now,
            )
            # Same as the inbound path: an opt-out cancels this customer's OTHER
            # scheduled jobs right away instead of leaving them showing "agendado".
            cancel_jobs_for_phones(
                session,
                [res.to or order.phone_e164, order.phone_alt],
                order.wa_id,
                reason="opted_out",
                now=now,
            )
            mark_failed(session, job, reason="opted_out", error_code=code, error_text=text, now=now)
        elif action == ErrorAction.BACKOFF:
            requeue(
                session,
                job,
                order,
                error_code=code,
                error_text=text,
                now=now,
                max_attempts=self.settings.worker_max_attempts,
            )
        elif action == ErrorAction.NO_RETRY_24H:
            # Spec: do NOT retry for 24h. requeue() re-checks expiry, so in practice
            # this ends as skipped/expires_too_soon — never a second nudge.
            requeue(
                session,
                job,
                order,
                error_code=code,
                error_text=text,
                now=now,
                delay=timedelta(hours=24),
                max_attempts=self.settings.worker_max_attempts,
            )
            if job.state == "scheduled":
                job.reason = "marketing_limit_24h"
        elif action == ErrorAction.TOKEN_INVALID:
            pause = timedelta(minutes=self.settings.worker_token_pause_minutes)
            self.paused_until = now + pause
            postpone(session, job, now + pause, reason="token_invalid", now=now)
            job.error_code, job.error_text = code, text
            record_alert(
                session,
                "token_invalid",
                "Token do WhatsApp inválido ou expirado — envios pausados. Gere um novo token.",
                context={"code": code, "job_id": job.id},
                now=now,
                # One dead token is ONE problem, not one per job in the batch.
                dedupe=True,
            )
        elif action == ErrorAction.TEMPLATE_UNAVAILABLE:
            # Kill switch for the rest of this batch (see Worker._kill_switch): Meta
            # has just told us the template is not sendable, so the remaining claims
            # must not POST to it.
            self.template_blocked = True
            status = "PAUSED" if code == "132015" else "DISABLED"
            upsert_template_status(
                session,
                store.template_name,
                store.template_language,
                status=status,
                reason=text,
                now=now,
            )
            mark_failed(session, job, reason=reason, error_code=code, error_text=text, now=now)
            record_alert(
                session,
                "template_unavailable",
                f"Modelo {store.template_name} está {status} — lembretes não serão enviados.",
                context={"code": code, "job_id": job.id},
                now=now,
                dedupe=True,  # one paused template is one problem
            )
        else:
            mark_failed(session, job, reason=reason, error_code=code, error_text=text, now=now)
            if err is not None and err.code in ALERT_CODES:
                record_alert(
                    session,
                    f"graph_{code}",
                    f"Falha no envio do pedido {order.sale_id}: {text}",
                    context={"code": code, "job_id": job.id, "reason": reason},
                    now=now,
                )

    def _apply_cart(
        self,
        session: Session,
        store: SettingsStore,
        item: ClaimedCartJob,
        outcome: DeliveryOutcome,
        now: datetime,
    ) -> None:
        """Record the outcome of one cart step (no commit). Mirrors :meth:`_apply`."""
        job, cart = item.job, item.cart
        self._apply_step(
            session,
            job=job,
            target=cart,
            step=item.step,
            outcome=outcome,
            now=now,
            requeue_job=lambda **kw: requeue_cart_job(session, job, cart, **kw),
            flow="cart",
            label=f"cart={cart.id}",
            what=f"da mensagem {job.step} do carrinho {cart.id}",
            template_alert=(
                f"Modelo de carrinho {item.step.template_name} está {{status}} — "
                "mensagens de carrinho com ele não serão enviadas."
            ),
        )

    def _apply_post_sale(
        self, session: Session, item: ClaimedPostSaleJob, outcome: DeliveryOutcome, now: datetime
    ) -> None:
        """Record the outcome of one post-sale step (no commit)."""
        job, sale = item.job, item.sale
        # Link the message to the PIX order when there is one, so the conversation
        # shows it next to the reminder; a card sale has no order row.
        order_id = session.execute(
            select(Order.id).where(Order.sale_id == sale.sale_id)
        ).scalar_one_or_none()
        self._apply_step(
            session,
            job=job,
            target=sale,
            step=item.step,
            outcome=outcome,
            now=now,
            requeue_job=lambda **kw: requeue_post_sale_job(session, job, sale, **kw),
            flow="post_sale",
            label=f"sale={sale.sale_id}",
            what=f"da mensagem {job.step} do pós-venda da venda {sale.sale_id}",
            template_alert=(
                f"Modelo de pós-venda {item.step.template_name} está {{status}} — "
                "mensagens de pós-venda com ele não serão enviadas."
            ),
            order_id=order_id,
        )

    def _apply_step(
        self,
        session: Session,
        *,
        job: CartJob | PostSaleJob,
        target: Cart | PostSale,
        step: StepConfig,
        outcome: DeliveryOutcome,
        now: datetime,
        requeue_job: Callable[..., object],
        flow: str,
        label: str,
        what: str,
        template_alert: str,
        order_id: int | None = None,
    ) -> None:
        """Shared recorder of one cart or post-sale step; mirrors :meth:`_apply`.

        ``flow`` names the alert codes and log lines, ``label`` identifies the cart or
        sale in the log, ``what`` and ``template_alert`` are the pt-BR alert texts.
        """
        res = outcome.result
        if res.ok:
            mark_sent(
                session, job, wa_id=res.wa_id, message_id=res.message_id, sent_to=res.to, now=now
            )
            if res.wa_id:
                target.wa_id = res.wa_id
            target.updated_at = now
            # Same messages table as the PIX reminders: Meta's delivered/read webhooks
            # land on this row, and the panel pages join it through wa_message_id.
            record_outbound_message(
                session,
                wa_id=res.wa_id or res.to,
                phone=res.to,
                message_id=res.message_id,
                kind=MessageKind.TEMPLATE.value,
                body=step_template_preview(step, res.params),
                template_name=step.template_name,
                order_id=order_id,
                now=now,
            )
            log.info(
                "%s step sent %s step=%s to=%s id=%s",
                flow,
                label,
                job.step,
                res.to,
                res.message_id,
            )
            return

        err = res.error
        code = err.code_str if err else "unknown"
        text = err.summary() if err else "unknown error"
        action = outcome.action or ErrorAction.FAIL
        reason = outcome.reason or fail_reason_for(err)
        log.warning(
            "%s step failed %s step=%s code=%s action=%s: %s",
            flow,
            label,
            job.step,
            code,
            action,
            text,
        )
        context_key = f"{flow}_job_id"

        if action == ErrorAction.OPT_OUT:
            add_opt_out(
                session,
                phone=res.to or target.phone_e164,
                wa_id=target.wa_id,
                source=OptOutSource.META_131050.value,
                note=text[:200],
                now=now,
            )
            cancel_jobs_for_phones(
                session,
                [res.to or target.phone_e164, target.phone_alt],
                target.wa_id,
                reason="opted_out",
                now=now,
            )
            mark_failed(session, job, reason="opted_out", error_code=code, error_text=text, now=now)
        elif action == ErrorAction.BACKOFF:
            requeue_job(
                error_code=code,
                error_text=text,
                now=now,
                max_attempts=self.settings.worker_max_attempts,
            )
        elif action == ErrorAction.NO_RETRY_24H:
            # 131049, Meta's per-user marketing cap. A PIX reminder waits a day and
            # tries again; a cart or post-sale step is not worth that, and insisting
            # is exactly what drags the number's quality rating down.
            mark_failed(
                session,
                job,
                reason="marketing_limit_24h",
                error_code=code,
                error_text=text,
                now=now,
            )
        elif action == ErrorAction.TOKEN_INVALID:
            pause = timedelta(minutes=self.settings.worker_token_pause_minutes)
            self.paused_until = now + pause
            postpone(session, job, now + pause, reason="token_invalid", now=now)
            job.error_code, job.error_text = code, text
            record_alert(
                session,
                "token_invalid",
                "Token do WhatsApp inválido ou expirado — envios pausados. Gere um novo token.",
                context={"code": code, context_key: job.id},
                now=now,
                dedupe=True,
            )
        elif action == ErrorAction.TEMPLATE_UNAVAILABLE:
            self.step_templates_blocked.add(step.template_name)
            status = "PAUSED" if code == "132015" else "DISABLED"
            upsert_template_status(
                session, step.template_name, step.language, status=status, reason=text, now=now
            )
            mark_failed(session, job, reason=reason, error_code=code, error_text=text, now=now)
            record_alert(
                session,
                f"{flow}_template_unavailable",
                template_alert.format(status=status),
                context={"code": code, context_key: job.id},
                now=now,
                dedupe=True,
            )
        else:
            mark_failed(session, job, reason=reason, error_code=code, error_text=text, now=now)
            if err is not None and err.code in ALERT_CODES:
                record_alert(
                    session,
                    f"graph_{code}",
                    f"Falha no envio {what}: {text}",
                    context={"code": code, context_key: job.id, "reason": reason},
                    now=now,
                )


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.create_all()
    worker = Worker(settings)
    if argv and "--once" in argv:
        return 0 if worker.run_once() >= 0 else 1
    worker.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
