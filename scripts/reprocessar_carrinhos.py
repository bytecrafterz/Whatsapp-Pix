"""Give the abandoned carts lost to the shared-checkout bug their first message.

From 2026-10-07 until the fix went live (2026-10-09, 08:56 São Paulo), Kirvano's
ABANDONED_CART events all carried the same checkout code ("null"), so every cart was
matched to the first one, which had already bought, and stored as ``ignored``: no cart,
no message. The raw events are still in ``webhook_events``. This script feeds them,
oldest first, through the fixed cart code, so each customer is checked exactly like a
live cart (opted out, bought since, PIX open, PIX reminder received...) and the worker
sends the first message right away instead of 10 minutes after the abandonment.

On top of the live checks it skips:

* customers who already have a newer cart (the live webhook handled them after the fix);
* customers who bought or generated a PIX with the same e-mail but another phone — a
  live cart would have been closed by that sale, which here arrived before the cart.

Preview first; nothing is saved without ``--enviar``::

    python -m scripts.reprocessar_carrinhos                # list who would get it
    python -m scripts.reprocessar_carrinhos --enviar       # create the carts (worker sends)
    python -m scripts.reprocessar_carrinhos --horas 12     # only the last 12 hours

On the server (same user and environment as the services)::

    bash deploy/run_remote.sh scripts.reprocessar_carrinhos [--enviar]

Running it twice is safe: a replayed event is marked ``processed`` and never picked again.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import clock, db  # noqa: E402
from app.cart import EVENT_ABANDONED_CART, handle_abandoned_cart, phone_forms  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.format import fmt_dt_sp  # noqa: E402
from app.kirvano import EventResult, KirvanoPayload, parse_payload  # noqa: E402
from app.models import (  # noqa: E402
    Cart,
    CartJob,
    Order,
    OrderStatus,
    PostSale,
    PostSaleStatus,
    WebhookEvent,
)
from app.panel import reason_label  # noqa: E402
from app.scheduling import _recipient_key, recipients_last_24h  # noqa: E402
from app.settings_store import SettingsStore  # noqa: E402

DEFAULT_HOURS = 24
# Script-only reasons (the panel labels cover the ones app.cart writes).
EXTRA_LABELS = {
    "newer_cart": "já tem um carrinho mais recente",
    "purchased_after_email": "comprou depois (mesmo e-mail, outro telefone)",
    "pix_generated_email": "gerou PIX depois (mesmo e-mail, outro telefone)",
}


@dataclass
class Line:
    """One replayed event, as printed."""

    abandoned_at: datetime
    who: str
    product: str
    reason: str
    first_run_at: datetime | None = None
    recipient: str | None = None

    @property
    def sends(self) -> bool:
        return self.first_run_at is not None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reprocessar_carrinhos",
        description="Reprocessa os carrinhos abandonados ignorados pelo bug do checkout.",
    )
    p.add_argument(
        "--horas",
        type=int,
        default=DEFAULT_HOURS,
        help=f"so carrinhos abandonados nas ultimas N horas (padrao {DEFAULT_HOURS})",
    )
    p.add_argument(
        "--enviar",
        action="store_true",
        help="grava os carrinhos e agenda a mensagem (sem isto e so uma previa)",
    )
    return p


def _who(payload: KirvanoPayload) -> str:
    """First name and the phone's last 4 digits: enough to recognise, nothing more."""
    first = (payload.customer.name or "").split()
    digits = "".join(ch for ch in payload.customer.phone_number or "" if ch.isdigit())
    tail = f"…{digits[-4:]}" if len(digits) >= 4 else "sem telefone"
    return f"{(first[0] if first else 'cliente')[:14]:<14} {tail}"


def _newer_cart(session: Session, forms: list[str], email: str | None, moment: datetime) -> bool:
    conds = []
    if forms:
        conds += [Cart.phone_e164.in_(forms), Cart.phone_alt.in_(forms)]
    if email:
        conds.append(func.lower(Cart.customer_email) == email)
    if not conds:
        return False
    return bool(
        session.execute(select(exists().where(Cart.abandoned_at >= moment, or_(*conds)))).scalar()
    )


def _bought_by_email(session: Session, email: str | None, moment: datetime) -> str | None:
    """A sale or PIX after ``moment`` under the same e-mail (any phone)."""
    if not email:
        return None
    sold = session.execute(
        select(
            exists().where(
                func.lower(PostSale.customer_email) == email,
                PostSale.paid_at >= moment,
                PostSale.status == PostSaleStatus.ACTIVE.value,
            )
        )
    ).scalar()
    if sold:
        return "purchased_after_email"
    statuses = set(
        session.execute(
            select(Order.status).where(
                func.lower(Order.customer_email) == email, Order.created_at >= moment
            )
        ).scalars()
    )
    if OrderStatus.PAID.value in statuses:
        return "purchased_after_email"
    return "pix_generated_email" if statuses else None


def lost_events(
    session: Session, settings: Settings, since: datetime
) -> list[tuple[datetime, WebhookEvent, KirvanoPayload]]:
    """Ignored ABANDONED_CART events abandoned since ``since``, oldest first."""
    rows = session.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.source == "kirvano",
            WebhookEvent.event == EVENT_ABANDONED_CART,
            WebhookEvent.outcome == "ignored",
            # Delivery comes after the abandonment, so this only narrows the scan.
            WebhookEvent.received_at >= since,
        )
        .order_by(WebhookEvent.id)
    ).scalars()
    found = []
    for evt in rows:
        payload = parse_payload(evt.payload or {}, settings.kirvano_tz)
        abandoned_at = min(payload.created_at or evt.received_at, evt.received_at)
        if abandoned_at >= since:
            found.append((abandoned_at, evt, payload))
    found.sort(key=lambda item: (item[0], item[1].id))
    return found


def replay(
    session: Session, store: SettingsStore, settings: Settings, *, since: datetime, now: datetime
) -> list[Line]:
    """Feed every lost event through the cart code. Does not commit."""
    lines: list[Line] = []
    for abandoned_at, evt, payload in lost_events(session, settings, since):
        c = payload.customer
        forms = phone_forms([c.phone_number])
        email = (c.email or "").strip().lower() or None
        line = Line(abandoned_at, _who(payload), (payload.product_name or "?")[:30], "")
        lines.append(line)

        skip = "newer_cart" if _newer_cart(session, forms, email, abandoned_at) else None
        skip = skip or _bought_by_email(session, email, abandoned_at)
        if skip:
            line.reason = skip
            continue

        result = EventResult(outcome="unknown", event=payload.event)
        cart = handle_abandoned_cart(session, payload, store, now, result)
        line.reason = result.reason or result.outcome
        evt.outcome = result.outcome
        evt.processed_at = now
        # A new dict, not an in-place change: the JSON column only notices assignment.
        evt.headers_meta = {**(evt.headers_meta or {}), "reprocessado_em": now.isoformat()}
        if (result.reason or "").startswith("scheduled_"):
            first = (
                session.execute(
                    select(CartJob.run_at).where(CartJob.cart_id == cart.id).order_by(CartJob.step)
                )
                .scalars()
                .first()
            )
            line.first_run_at = first
            line.recipient = _recipient_key(cart.phone_e164 or cart.phone_alt)
    session.flush()
    return lines


def _label(reason: str) -> str:
    if reason.startswith("scheduled_"):
        return "recebe"
    return EXTRA_LABELS.get(reason) or reason_label(reason) or reason


def report(lines: list[Line], *, hours: int, already: int, limit: int, now: datetime) -> str:
    out = []
    for line in lines:
        when = fmt_dt_sp(line.abandoned_at, "%d/%m %H:%M")
        if line.sends:
            at = line.first_run_at or now
            verdict = "RECEBE" + ("" if at <= now else f" às {fmt_dt_sp(at, '%d/%m %H:%M')}")
        else:
            verdict = f"pula: {_label(line.reason)}"
        out.append(f"{when}  {line.who}  {line.product:<30}  -> {verdict}")
    sending = [line for line in lines if line.sends]
    new = {line.recipient for line in sending if line.recipient}
    out.append("")
    out.append(
        f"{len(lines)} carrinho(s) perdido(s) nas últimas {hours} h: "
        f"{len(sending)} recebem a mensagem, {len(lines) - len(sending)} pulado(s)."
    )
    total = already + len(new)
    out.append(
        f"Limite diário: {already} contato(s) nas últimas 24 h + {len(new)} novo(s) "
        f"= {total} de {limit}."
    )
    if total > limit:
        out.append(
            f"ATENÇÃO: {total - limit} ficariam sem mensagem (limite diário). "
            "Use --horas menor ou aumente o limite no painel."
        )
    return "\n".join(out)


def main(
    argv: list[str] | None = None,
    settings: Settings | None = None,
    session: Session | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.horas < 1:
        print("--horas precisa ser pelo menos 1")
        return 2
    settings = settings or get_settings()
    own = session is None
    session = session or db.get_sessionmaker()()
    try:
        store = SettingsStore(session, settings)
        if not store.cart_enabled:
            print("A recuperação de carrinho está DESLIGADA no painel: ligue antes de reprocessar.")
            return 1
        now = clock.utcnow()
        already = len(recipients_last_24h(session, now))
        lines = replay(session, store, settings, since=now - timedelta(hours=args.horas), now=now)
        print(
            report(
                lines,
                hours=args.horas,
                already=already,
                limit=store.daily_recipient_limit,
                now=now,
            )
        )
        if not args.enviar:
            session.rollback()
            print("\nPRÉVIA: nada foi salvo. Para enviar de verdade, rode de novo com --enviar.")
            return 0
        session.commit()
        print("\nGravado. O worker envia as mensagens nos próximos minutos (Painel › Carrinho).")
        return 0
    except BaseException:
        session.rollback()
        raise
    finally:
        if own:
            session.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
