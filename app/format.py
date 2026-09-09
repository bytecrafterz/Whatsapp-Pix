"""pt-BR rendering helpers shared by the template builder, the panel and the PIX page."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

SP_TZ = ZoneInfo("America/Sao_Paulo")


def fmt_brl(cents: int | None, *, symbol: bool = False) -> str:
    """Format integer cents as ``1.169,80`` (or ``R$ 1.169,80`` with ``symbol=True``).

    ``None`` renders as ``0,00`` so templates never crash on a missing amount.
    """
    value = int(cents or 0)
    negative = value < 0
    value = abs(value)
    reais, cent = divmod(value, 100)
    reais_str = f"{reais:,}".replace(",", ".")
    out = f"{reais_str},{cent:02d}"
    if negative:
        out = "-" + out
    return f"R$ {out}" if symbol else out


def fmt_dt_sp(dt: datetime | None, fmt: str = "%d/%m às %H:%M", tz: ZoneInfo = SP_TZ) -> str:
    """Render an aware datetime in America/Sao_Paulo, e.g. ``15/09 às 18:00``."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        raise ValueError("fmt_dt_sp expects an aware datetime")
    return dt.astimezone(tz).strftime(fmt)


def fmt_dt_sp_full(dt: datetime | None, tz: ZoneInfo = SP_TZ) -> str:
    """``dd/mm/aaaa HH:MM`` in São Paulo time — for panel listings."""
    return fmt_dt_sp(dt, "%d/%m/%Y %H:%M", tz)


def first_name(full_name: str | None, fallback: str = "cliente") -> str:
    """First word of a customer name, title-cased; ``fallback`` when empty."""
    if not full_name:
        return fallback
    parts = full_name.strip().split()
    if not parts:
        return fallback
    name = parts[0]
    # Keep genuine casing for things like "D'Ávila"; only fix all-caps / all-lower.
    if name.isupper() or name.islower():
        name = name.capitalize()
    return name
