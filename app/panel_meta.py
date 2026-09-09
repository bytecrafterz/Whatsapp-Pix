"""Graph API helper behind the panel's "Modelo" page.

The Modelo page shows the last known status/category of the WhatsApp template
(fed by the ``message_template_status_update`` / ``template_category_update``
webhooks). Those webhooks only fire when something *changes*, so the operator
also needs a "check now" button: this module performs that on-demand read.

It deliberately reuses :class:`app.whatsapp.GraphClient` — the only place the
``META_ACCESS_TOKEN`` is ever put on the wire (Authorization header, never
logged) — and :func:`app.inbound.upsert_template_status` to persist the answer,
so the panel never talks HTTP or writes template rows itself.

Nothing here commits: the calling route owns the transaction. The Graph call
happens before any write, so no HTTP request is ever made while a write
transaction is open (ARCHITECTURE §7 rule 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.inbound import upsert_template_status
from app.models import TemplateStatus
from app.whatsapp import GraphClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a manual refresh; ``message`` is pt-BR and shown as-is."""

    ok: bool
    row: TemplateStatus | None
    message: str


def refresh_template_status(
    session: Session,
    client: GraphClient | None,
    name: str,
    language: str | None = None,
    *,
    now: datetime | None = None,
) -> RefreshResult:
    """Ask Meta for ``name``'s current status/category and store what comes back.

    ``client`` is ``None`` when ``META_ACCESS_TOKEN`` is unset — the panel must
    show the pt-BR explanation instead of crashing.
    """
    if client is None or not client.configured:
        return RefreshResult(
            ok=False,
            row=None,
            message=(
                "Não foi possível consultar a Meta: o token do WhatsApp "
                "(META_ACCESS_TOKEN) não está configurado no servidor."
            ),
        )
    if not name:
        return RefreshResult(False, None, "Nenhum modelo configurado em Configurações.")

    data = client.fetch_template_status(name, language)
    if data is None and language:
        # The merchant may have created the template in another language than the
        # one configured here; a nameless-language retry still tells the operator
        # what exists in the account instead of a bare "not found".
        data = client.fetch_template_status(name)
    if not data:
        return RefreshResult(
            ok=False,
            row=None,
            message=(
                f"A Meta não retornou nenhum modelo chamado “{name}”. "
                "Confira o nome em Configurações e se o modelo existe no "
                "Gerenciador do WhatsApp."
            ),
        )

    row = upsert_template_status(
        session,
        data.get("name") or name,
        data.get("language") or language,
        status=data.get("status"),
        category=data.get("category"),
        template_id=str(data["id"]) if data.get("id") is not None else None,
        now=now,
    )
    log.info("template status refreshed from panel: %s=%s", row.name, row.status)
    return RefreshResult(
        ok=True,
        row=row,
        message=(
            f"Status atualizado: {row.status or 'desconhecido'}"
            f" · categoria {row.category or 'desconhecida'}."
        ),
    )
