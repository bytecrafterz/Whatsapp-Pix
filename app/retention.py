"""Data retention: the 12-month limit the public privacy policy promises.

``app/templates/pages/privacidade.html`` §7 tells every data subject (and the ANPD)
that order and message data is "apagado ou anonimizado" 12 months after the order,
and that the consent evidence (IP + timestamp) follows the same clock. That page is
published under a real CNPJ, so the statement is a commitment: it has to be
*performed*, not merely written. Nothing enforced it before this module existed.

What a purge does, for rows older than the cutoff:

* **orders** — the identifying columns are nulled (name, e-mail, every phone form,
  wa_id, consent IP, and the payable PIX code / QR / recovery URL). The row itself
  SURVIVES with its ``sale_id``, amount, status and timestamps, so the Início
  counters ("pagos após lembrete", "expirados", ...) keep working. That is exactly
  the "ficam apenas números totais, sem identificar você" the page promises.
* **messages** — the body is nulled and the phone/wa_id cleared; direction, kind,
  status and timestamps stay, again for the counters.
* **contacts** — profile name and phone cleared for contacts with no recent activity.
* **webhook_events** — deleted outright. They are raw vendor payloads kept for
  debugging; after a year they are pure liability.

``opt_outs`` are deliberately NEVER purged: that list is the only thing keeping a
customer who said SAIR from being messaged again, which the same page also promises.

Idempotent: purging twice changes nothing the second time. Never commits — the
caller (``scripts/purge_old_data.py``, through ``db.session_scope``) owns the
transaction, exactly like the rest of the code base.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app import clock
from app.models import Contact, Message, Order, WebhookEvent

log = logging.getLogger("app.retention")

# 12 months, as published. Kept as a constant so the code and the page can be
# checked against each other in one grep.
RETENTION_DAYS = 365


@dataclass
class PurgeResult:
    """How much each table gave up (for the script's output and the log)."""

    cutoff: datetime
    orders_anonymised: int = 0
    messages_anonymised: int = 0
    contacts_anonymised: int = 0
    events_deleted: int = 0

    @property
    def total(self) -> int:
        return (
            self.orders_anonymised
            + self.messages_anonymised
            + self.contacts_anonymised
            + self.events_deleted
        )

    def summary(self) -> str:
        return (
            f"corte {self.cutoff.isoformat()}: "
            f"{self.orders_anonymised} pedidos anonimizados, "
            f"{self.messages_anonymised} mensagens anonimizadas, "
            f"{self.contacts_anonymised} contatos anonimizados, "
            f"{self.events_deleted} eventos apagados"
        )


def purge(
    session: Session,
    *,
    older_than_days: int = RETENTION_DAYS,
    now: datetime | None = None,
    dry_run: bool = False,
) -> PurgeResult:
    """Apply the retention policy. Does not commit.

    ``dry_run`` counts what *would* change without writing, so the operator can see
    the effect of a first run before it happens.
    """
    now = now or clock.utcnow()
    cutoff = now - timedelta(days=older_than_days)
    result = PurgeResult(cutoff=cutoff)

    # --- orders ---------------------------------------------------------------
    # `customer_name IS NOT NULL OR ...`: without it every already-purged row would
    # be counted (and re-written) on every run.
    order_ids = list(
        session.execute(
            select(Order.id).where(
                Order.created_at < cutoff,
                or_(
                    Order.customer_name.is_not(None),
                    Order.customer_email.is_not(None),
                    Order.phone_raw.is_not(None),
                    Order.phone_e164.is_not(None),
                    Order.phone_alt.is_not(None),
                    Order.wa_id.is_not(None),
                    Order.consent_ip.is_not(None),
                    Order.pix_code.is_not(None),
                ),
            )
        ).scalars()
    )
    result.orders_anonymised = len(order_ids)
    if order_ids and not dry_run:
        session.execute(
            update(Order)
            .where(Order.id.in_(order_ids))
            .values(
                customer_name=None,
                customer_email=None,
                customer_document=None,  # never written, nulled anyway for safety
                phone_raw=None,
                phone_e164=None,
                phone_alt=None,
                wa_id=None,
                consent_ip=None,
                pix_code=None,
                pix_qr_image_url=None,
                checkout_recovery_url=None,
                updated_at=now,
            )
        )

    # --- messages -------------------------------------------------------------
    message_ids = list(
        session.execute(
            select(Message.id).where(
                Message.created_at < cutoff,
                or_(
                    Message.body.is_not(None),
                    Message.phone.is_not(None),
                    Message.wa_id.is_not(None),
                ),
            )
        ).scalars()
    )
    result.messages_anonymised = len(message_ids)
    if message_ids and not dry_run:
        session.execute(
            update(Message)
            .where(Message.id.in_(message_ids))
            .values(body=None, phone=None, wa_id=None)
        )

    # --- contacts -------------------------------------------------------------
    # A contact is only touched when BOTH activity timestamps are old (or absent):
    # an old first message with a reply last week must keep its inbox entry.
    contact_ids = list(
        session.execute(
            select(Contact.wa_id).where(
                or_(Contact.last_inbound_at.is_(None), Contact.last_inbound_at < cutoff),
                or_(Contact.last_outbound_at.is_(None), Contact.last_outbound_at < cutoff),
                or_(Contact.profile_name.is_not(None), Contact.phone.is_not(None)),
            )
        ).scalars()
    )
    result.contacts_anonymised = len(contact_ids)
    if contact_ids and not dry_run:
        session.execute(
            update(Contact)
            .where(Contact.wa_id.in_(contact_ids))
            .values(profile_name=None, phone=None)
        )

    # --- webhook events -------------------------------------------------------
    event_ids = list(
        session.execute(select(WebhookEvent.id).where(WebhookEvent.received_at < cutoff)).scalars()
    )
    result.events_deleted = len(event_ids)
    if event_ids and not dry_run:
        session.execute(delete(WebhookEvent).where(WebhookEvent.id.in_(event_ids)))

    if not dry_run:
        session.flush()
    log.info("retention purge %s%s", "(simulacao) " if dry_run else "", result.summary())
    return result
