"""Meta webhook processing: delivery statuses, inbound messages, opt-out
detection with auto-reply, template status/category updates.

Also hosts :func:`send_free_text`, the only sanctioned way for the panel to
send a free-form reply (it enforces the 24h customer-service window).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.alerts import record_alert
from app.models import (
    Contact,
    JobState,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
    OptOutSource,
    Order,
    RecoveryJob,
    TemplateStatus,
    WebhookEvent,
)
from app.optout import add_opt_out, cancel_jobs_for_phones
from app.phone import normalize_br
from app.scheduling import TEMPLATE_BLOCKING_STATUSES, TEMPLATE_WARNING_STATUSES
from app.whatsapp import (
    GraphClient,
    InboundMessage,
    SendResult,
    StatusUpdate,
    TemplateCategoryUpdate,
    TemplateStatusUpdate,
    parse_webhook_payload,
)

log = logging.getLogger(__name__)

SERVICE_WINDOW = timedelta(hours=24)

OPT_OUT_REPLY = (
    "Pronto, você não receberá mais avisos da Jornada com Meu Anjo. "
    "Se precisar de ajuda com seu pedido, é só responder aqui."
)
# SPEC line 80 asks for KEYWORD matching, so a keyword anywhere in the message counts
# ("pare de me enviar mensagens", "quero sair da lista", "me descadastre"). Matching the
# whole message instead — what this used to do — silently kept messaging people who had
# clearly asked us to stop, which is the failure Meta punishes with a quality downgrade.
OPT_OUT_WORDS = frozenset(
    {"sair", "saia", "parar", "pare", "stop", "cancelar", "descadastrar", "descadastre"}
)
# `\b` boundaries only: "parada", "cancelamento" or "separar" must not fire. "para" is
# deliberately absent — it is the most common Portuguese preposition ("para pagar").
_OPT_OUT_WORD_RE = re.compile(rf"\b(?:{'|'.join(sorted(OPT_OUT_WORDS))})\b")

# A keyword governed by a negated verb is the OPPOSITE intent: "nao quero cancelar meu
# pedido" is a customer reassuring us, not unsubscribing. Up to two words may sit between
# the verb and the keyword ("nao quero mais que cancelem").
_NEGATED_KEYWORD_RE = re.compile(
    r"\bnao\s+(?:quero|vou|posso|desejo|pretendo|consigo|preciso|pedi)\s+"
    rf"(?:\w+\s+){{0,2}}?(?:{'|'.join(sorted(OPT_OUT_WORDS))})\b"
)

OPT_OUT_PHRASE = "nao quero"
# Words that may follow "nao quero" while it still means "stop messaging me". Anything
# else after it turns the phrase into a negated wish ("nao quero perder essa oferta",
# "nao quero errar o pagamento") and must NOT opt the customer out.
_UNSUBSCRIBE_TAIL = frozenset(
    {
        "mais", "nada", "isso", "isto", "disso", "nenhum", "nenhuma", "nem",
        "receber", "recebe", "receba", "recebendo", "ser", "seja",
        "mensagem", "mensagens", "aviso", "avisos", "lembrete", "lembretes",
        "notificacao", "notificacoes", "cobranca", "cobrancas", "contato", "contatos",
        "whatsapp", "zap", "sms", "email", "emails",
        "de", "do", "da", "dos", "das", "essa", "esse", "essas", "esses", "esta", "este",
        "sua", "seu", "suas", "seus", "voces", "voce", "aqui", "obrigado", "obrigada",
        "por", "favor", "ok", "ja", "e", "nao",
    }
)  # fmt: skip
OPT_OUT_BUTTON = "nao quero receber"

# Status precedence: never move a message backwards (webhooks can arrive out of order).
_STATUS_RANK = {
    MessageStatus.SENT.value: 1,
    MessageStatus.DELIVERED.value: 2,
    MessageStatus.READ.value: 3,
    MessageStatus.FAILED.value: 4,
}


class WindowClosedError(Exception):
    """Raised by :func:`send_free_text` when the 24h window is closed (message is pt-BR)."""


# --- text helpers ------------------------------------------------------------------------


def normalize_text(text: str | None) -> str:
    """Lowercase, accent-stripped, punctuation-free, single-spaced."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _nao_quero_is_unsubscribe(norm: str) -> bool:
    """True when "nao quero" is the unsubscribe phrase rather than a negated wish.

    The phrase counts only when everything after it belongs to the small
    "stop messaging me" vocabulary: "nao quero", "nao quero mais",
    "nao quero receber mais mensagens". As soon as another verb or object follows
    ("nao quero perder essa oferta", "nao quero errar o pagamento") the sentence
    means the opposite and cancelling the reminder would be wrong.
    """
    index = norm.find(OPT_OUT_PHRASE)
    while index != -1:
        tail = norm[index + len(OPT_OUT_PHRASE) :].split()
        if all(token in _UNSUBSCRIBE_TAIL for token in tail):
            return True
        index = norm.find(OPT_OUT_PHRASE, index + 1)
    return False


def is_opt_out_text(text: str | None) -> bool:
    """Keyword-based opt-out detection (case/accent-insensitive), SPEC line 80.

    Two guards keep it from firing on the opposite intent: a keyword governed by a
    negated verb ("nao quero cancelar meu pedido") is ignored, and the "nao quero"
    phrase only counts when nothing but unsubscribe vocabulary follows it.
    """
    norm = normalize_text(text)
    if not norm:
        return False
    if _NEGATED_KEYWORD_RE.search(norm):
        return False
    return bool(_OPT_OUT_WORD_RE.search(norm)) or _nao_quero_is_unsubscribe(norm)


def is_opt_out_button(msg: InboundMessage) -> bool:
    if not msg.is_button_reply:
        return False
    for candidate in (
        msg.button_text,
        msg.button_payload,
        msg.interactive_title,
        msg.interactive_id,
    ):
        norm = normalize_text(candidate)
        if norm and (norm == OPT_OUT_BUTTON or is_opt_out_text(candidate)):
            return True
    return False


# --- window --------------------------------------------------------------------------------


def window_closes_at(contact: Contact | None) -> datetime | None:
    if contact is None or contact.last_inbound_at is None:
        return None
    return contact.last_inbound_at + SERVICE_WINDOW


def window_open(contact: Contact | None, now: datetime | None = None) -> bool:
    """True while we are inside the 24h customer-service window opened by an inbound message."""
    closes = window_closes_at(contact)
    if closes is None:
        return False
    return (now or clock.utcnow()) < closes


# --- upserts ------------------------------------------------------------------------------


def upsert_contact(
    session: Session,
    wa_id: str,
    *,
    phone: str | None = None,
    profile_name: str | None = None,
    inbound_at: datetime | None = None,
    outbound_at: datetime | None = None,
) -> Contact:
    contact = session.get(Contact, wa_id)
    if contact is None:
        contact = Contact(wa_id=wa_id)
        session.add(contact)
    if phone:
        forms = normalize_br(phone)
        contact.phone = forms.primary if forms else phone
    elif not contact.phone:
        forms = normalize_br(wa_id)
        contact.phone = forms.primary if forms else wa_id
    if profile_name:
        contact.profile_name = profile_name
    if inbound_at and (contact.last_inbound_at is None or inbound_at > contact.last_inbound_at):
        contact.last_inbound_at = inbound_at
    if outbound_at and (contact.last_outbound_at is None or outbound_at > contact.last_outbound_at):
        contact.last_outbound_at = outbound_at
    session.flush()
    return contact


def find_order_for_contact(session: Session, wa_id: str) -> Order | None:
    """Most recent order for this contact (by wa_id or any phone variant); pending first."""
    forms = normalize_br(wa_id)
    variants = forms.variants if forms else [wa_id]
    stmt = (
        select(Order)
        .where(
            or_(
                Order.wa_id == wa_id,
                Order.phone_e164.in_(variants),
                Order.phone_alt.in_(variants),
            )
        )
        .order_by(Order.created_at.desc())
        .limit(20)
    )
    orders = list(session.execute(stmt).scalars())
    if not orders:
        return None
    for o in orders:
        if o.status == "pending":
            return o
    return orders[0]


def upsert_template_status(
    session: Session,
    name: str,
    language: str | None,
    *,
    status: str | None = None,
    category: str | None = None,
    reason: str | None = None,
    template_id: str | None = None,
    now: datetime | None = None,
) -> TemplateStatus:
    """Create/update the last-seen status of a template. Language defaults to the row found."""
    now = now or clock.utcnow()
    lang = language or "pt_BR"
    row = session.get(TemplateStatus, (name, lang))
    if row is None and language is None:
        # Category updates omit the language: update whichever row we have for the name.
        row = session.execute(
            select(TemplateStatus).where(TemplateStatus.name == name).limit(1)
        ).scalar_one_or_none()
    if row is None:
        row = TemplateStatus(name=name, language=lang, updated_at=now)
        session.add(row)
    if status is not None:
        row.status = status.upper()
    if category is not None:
        row.category = category.upper()
    if reason is not None:
        row.reason = reason
    if template_id is not None:
        row.template_id = template_id
    row.updated_at = now
    session.flush()
    return row


def record_outbound_message(
    session: Session,
    *,
    wa_id: str | None,
    phone: str | None,
    message_id: str | None,
    kind: str,
    body: str | None,
    template_name: str | None = None,
    order_id: int | None = None,
    now: datetime | None = None,
) -> Message:
    now = now or clock.utcnow()
    msg = Message(
        direction=MessageDirection.OUT.value,
        order_id=order_id,
        wa_id=wa_id,
        phone=phone,
        wa_message_id=message_id,
        kind=kind,
        body=body,
        template_name=template_name,
        status=MessageStatus.SENT.value,
        status_updated_at=now,
        created_at=now,
    )
    session.add(msg)
    session.flush()
    if wa_id:
        upsert_contact(session, wa_id, phone=phone, outbound_at=now)
    return msg


# --- processing ----------------------------------------------------------------------------


@dataclass
class InboundResult:
    statuses_updated: int = 0
    statuses_unmatched: int = 0
    messages_stored: int = 0
    messages_duplicate: int = 0
    opt_outs: int = 0
    replies_sent: int = 0
    template_updates: int = 0
    other: int = 0
    pending_replies: list[tuple[str, str, int | None]] | None = None  # (wa_id, phone, order_id)


def record_status(
    session: Session, st: StatusUpdate, *, now: datetime | None = None
) -> Message | None:
    """Apply a delivery status to the matching outbound message row."""
    now = now or clock.utcnow()
    msg = session.execute(
        select(Message).where(Message.wa_message_id == st.message_id)
    ).scalar_one_or_none()
    if msg is None:
        return None
    new_rank = _STATUS_RANK.get(st.status, 0)
    cur_rank = _STATUS_RANK.get(msg.status or "", 0)
    if new_rank >= cur_rank:
        msg.status = st.status if st.status in _STATUS_RANK else msg.status
        msg.status_updated_at = st.timestamp or now
    if st.status == MessageStatus.FAILED.value:
        msg.error_code = str(st.error_code) if st.error_code is not None else None
        msg.error_text = (
            " — ".join(p for p in (st.error_title, st.error_message, st.error_details) if p)[:2000]
            or None
        )
        job = session.execute(
            select(RecoveryJob).where(RecoveryJob.wa_message_id == st.message_id)
        ).scalar_one_or_none()
        if job is not None and job.state == JobState.SENT.value:
            job.error_code = msg.error_code
            job.error_text = msg.error_text
            job.updated_at = now
        if st.error_code == 131050:
            wa_id = st.recipient_id or msg.wa_id
            add_opt_out(
                session,
                phone=msg.phone,
                wa_id=wa_id,
                source=OptOutSource.META_131050.value,
                note="Meta error 131050 (user opted out of marketing)",
                now=now,
            )
            # Cancel now, exactly like the inbound-text path. claim_due_jobs would
            # skip these jobs at fire time anyway, but leaving them "agendado" until
            # then makes the panel's Início counts lie for up to the whole delay.
            cancel_jobs_for_phones(session, [msg.phone], wa_id, reason="opted_out", now=now)
    if st.recipient_id and not msg.wa_id:
        msg.wa_id = st.recipient_id
    session.flush()
    return msg


def record_inbound(
    session: Session,
    msg: InboundMessage,
    *,
    now: datetime | None = None,
    result: InboundResult | None = None,
) -> Message | None:
    """Store an inbound message, update the contact, link the order, detect opt-out.

    Returns ``None`` for a duplicate delivery. Does not send the auto-reply — the
    caller does that *after* committing (see :func:`handle_meta_webhook`).
    """
    now = now or clock.utcnow()
    result = result if result is not None else InboundResult()
    exists = session.execute(
        select(Message.id).where(Message.wa_message_id == msg.message_id)
    ).scalar_one_or_none()
    if exists is not None:
        result.messages_duplicate += 1
        return None

    ts = msg.timestamp or now
    forms = normalize_br(msg.wa_id)
    phone = forms.primary if forms else msg.wa_id
    upsert_contact(session, msg.wa_id, phone=phone, profile_name=msg.profile_name, inbound_at=ts)
    order = find_order_for_contact(session, msg.wa_id)
    if order is not None and not order.wa_id:
        order.wa_id = msg.wa_id
        order.updated_at = now

    row = Message(
        direction=MessageDirection.IN.value,
        order_id=order.id if order else None,
        wa_id=msg.wa_id,
        phone=phone,
        wa_message_id=msg.message_id,
        kind=MessageKind.TEXT.value,
        body=msg.display_text,
        status=MessageStatus.RECEIVED.value,
        status_updated_at=ts,
        created_at=ts,
    )
    # SAVEPOINT, not a plain flush: `handle_meta_webhook` applies `value.statuses[]`
    # BEFORE `value.messages[]` in the same transaction, and Meta batches the two.
    # A full rollback on a concurrent redelivery would silently throw away every
    # delivery status already applied in this batch while the HTTP response still
    # reported them as updated. The savepoint discards only the duplicate row.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        result.messages_duplicate += 1
        return None
    result.messages_stored += 1

    source: str | None = None
    if is_opt_out_button(msg):
        source = OptOutSource.BUTTON.value
    elif msg.type == "text" and is_opt_out_text(msg.text):
        source = OptOutSource.TEXT.value
    if source:
        created = add_opt_out(
            session,
            phone=phone,
            wa_id=msg.wa_id,
            source=source,
            note=msg.display_text[:200],
            now=now,
        )
        cancel_jobs_for_phones(session, [phone], msg.wa_id, reason="opted_out", now=now)
        if created:
            result.opt_outs += 1
            if result.pending_replies is None:
                result.pending_replies = []
            # Reply once: only when this message created the opt-out.
            result.pending_replies.append((msg.wa_id, phone, order.id if order else None))
    return row


def apply_template_status(
    session: Session, upd: TemplateStatusUpdate, *, now: datetime | None = None
) -> TemplateStatus:
    row = upsert_template_status(
        session,
        upd.name,
        upd.language,
        status=upd.event,
        reason=upd.reason,
        template_id=upd.template_id,
        now=now,
    )
    if upd.event in TEMPLATE_BLOCKING_STATUSES or upd.event in TEMPLATE_WARNING_STATUSES:
        # FLAGGED / PENDING_DELETION are warnings: Meta still delivers on them, so the
        # operator is told without the reminder flow being stopped (see scheduling).
        blocking = upd.event in TEMPLATE_BLOCKING_STATUSES
        record_alert(
            session,
            "template_status",
            f"Modelo {upd.name} ({row.language}) está {upd.event}: {upd.reason or 'sem motivo'}",
            level="error" if blocking else "warning",
            context={"name": upd.name, "event": upd.event, "reason": upd.reason},
            now=now,
        )
    return row


def apply_template_category(
    session: Session, upd: TemplateCategoryUpdate, *, now: datetime | None = None
) -> TemplateStatus:
    row = upsert_template_status(
        session, upd.name, upd.language, category=upd.new_category, now=now
    )
    if (upd.new_category or "").upper() == "MARKETING":
        record_alert(
            session,
            "template_category",
            f"Modelo {upd.name} foi reclassificado como MARKETING (antes: {upd.previous_category})",
            level="warning",
            context={"name": upd.name, "previous": upd.previous_category, "new": upd.new_category},
            now=now,
        )
    return row


def _store_meta_event(session: Session, payload: dict, fields: list[str], now: datetime) -> None:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    evt = WebhookEvent(
        source="meta",
        external_key=f"{','.join(fields) or 'unknown'}|sha:{digest[:40]}"[:255],
        event=fields[0] if fields else None,
        payload=payload,
        headers_meta=None,
        received_at=now,
        processed_at=now,
        outcome="stored",
    )
    # Savepoint for the same reason as in `record_inbound`: an exact redelivery must
    # not roll back the statuses/messages this transaction has already applied.
    try:
        with session.begin_nested():
            session.add(evt)
            session.flush()
    except IntegrityError:
        log.info("duplicate Meta webhook %s", evt.external_key)  # processing is idempotent


def handle_meta_webhook(
    session: Session,
    payload: dict,
    *,
    client: GraphClient | None = None,
    now: datetime | None = None,
    store_event: bool = True,
) -> InboundResult:
    """Process one Meta webhook body. Commits. Never raises for payload problems.

    Auto-replies to a fresh opt-out are sent **after** the commit so the HTTP
    round-trip never sits inside the DB transaction.
    """
    now = now or clock.utcnow()
    events = parse_webhook_payload(payload)
    result = InboundResult()
    if store_event:
        _store_meta_event(session, payload, events.fields, now)

    for st in events.statuses:
        if record_status(session, st, now=now) is None:
            result.statuses_unmatched += 1
        else:
            result.statuses_updated += 1
    for msg in events.messages:
        record_inbound(session, msg, now=now, result=result)
    for upd in events.template_status_updates:
        apply_template_status(session, upd, now=now)
        result.template_updates += 1
    for cat in events.template_category_updates:
        apply_template_category(session, cat, now=now)
        result.template_updates += 1
    result.other = len(events.other)
    for other in events.other:
        log.info("meta webhook field=%s value_keys=%s", other.field, sorted(other.value.keys()))
    session.commit()

    for wa_id, phone, order_id in result.pending_replies or []:
        if client is None:
            log.warning("opt-out reply skipped (no Graph client) wa_id=%s", wa_id)
            continue
        res = client.send_text(wa_id, OPT_OUT_REPLY)
        if res.ok:
            record_outbound_message(
                session,
                wa_id=res.wa_id or wa_id,
                phone=phone,
                message_id=res.message_id,
                kind=MessageKind.TEXT.value,
                body=OPT_OUT_REPLY,
                order_id=order_id,
                now=now,
            )
            result.replies_sent += 1
        else:
            log.warning(
                "opt-out reply failed wa_id=%s: %s",
                wa_id,
                res.error.summary() if res.error else "?",
            )
        session.commit()
    return result


def send_free_text(
    session: Session,
    client: GraphClient,
    wa_id: str,
    body: str,
    *,
    now: datetime | None = None,
    order_id: int | None = None,
    force: bool = False,
) -> SendResult:
    """Panel reply: free text inside the 24h window (``force`` skips the local check).

    Raises :class:`WindowClosedError` (pt-BR message) when the window is closed.
    Records the outbound message on success. Does not commit.
    """
    now = now or clock.utcnow()
    contact = session.get(Contact, wa_id)
    if not force and not window_open(contact, now):
        raise WindowClosedError(
            "A janela de 24 horas está fechada: o cliente precisa enviar uma mensagem "
            "antes que você possa responder em texto livre."
        )
    text = body.strip()
    if not text:
        raise ValueError("Mensagem vazia")
    res = client.send_text(wa_id, text)
    if res.ok:
        if order_id is None:
            order = find_order_for_contact(session, wa_id)
            order_id = order.id if order else None
        record_outbound_message(
            session,
            wa_id=res.wa_id or wa_id,
            phone=contact.phone if contact else None,
            message_id=res.message_id,
            kind=MessageKind.TEXT.value,
            body=text,
            order_id=order_id,
            now=now,
        )
    return res
