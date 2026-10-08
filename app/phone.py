"""Brazilian phone number normalisation.

WhatsApp identifies Brazilian mobiles inconsistently: numbers registered before
the 9th-digit migration may still be known to Meta as ``55 DD XXXXXXXX`` (12
digits) while Kirvano sends ``55 DD 9XXXXXXXX`` (13 digits). We therefore keep
BOTH forms for every BR mobile: ``primary`` (13-digit, with the 9) is what we
try first, ``alternate`` (12-digit) is the retry on error 131026.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NON_DIGITS = re.compile(r"\D+")


@dataclass(frozen=True)
class PhoneForms:
    """Normalised forms of one phone number."""

    primary: str
    alternate: str | None
    country: str  # "BR" or "other"
    kind: str  # "mobile" | "landline" | "unknown"

    @property
    def variants(self) -> list[str]:
        """Unique list of dialable forms, primary first."""
        out = [self.primary]
        if self.alternate and self.alternate != self.primary:
            out.append(self.alternate)
        return out


def digits_only(raw: str | None) -> str:
    return _NON_DIGITS.sub("", raw or "")


def normalize_br(raw: str | None) -> PhoneForms | None:
    """Normalise a phone string into :class:`PhoneForms`; ``None`` when unusable.

    Rules (spec "Identifiers"): strip non-digits; drop a leading international
    ``00`` or trunk ``0``; 10–11 digits → assume Brazil and prefix ``55``; for BR
    mobiles produce both the 13-digit (with 9) and 12-digit (without 9) forms.
    """
    digits = digits_only(raw)
    if not digits:
        return None

    if (raw or "").strip().startswith("+") and not digits.startswith("55"):
        # "+1 (447) 330-1229" is already a full international number. Without this, its
        # 11 digits pass for a Brazilian number without the 55 (DDD 14) and the message
        # would go to a stranger in Bauru.
        if len(digits) < 8 or len(digits) > 15:
            return None
        return PhoneForms(primary=digits, alternate=None, country="other", kind="unknown")

    if digits.startswith("00") and len(digits) > 12:
        digits = digits[2:]  # "0055..." international dialling prefix
    elif digits.startswith("0") and len(digits) in (11, 12):
        digits = digits[1:]  # "011 9..." trunk prefix

    if len(digits) in (10, 11):
        digits = "55" + digits

    if not digits.startswith("55") or len(digits) not in (12, 13):
        # E.164 caps a number at 15 digits. Anything longer is garbage, and keeping it
        # used to break the whole event on PostgreSQL ("value too long for type
        # character varying(20)" on orders.phone_e164) — SQLite never complained.
        if len(digits) < 8 or len(digits) > 15:
            return None
        return PhoneForms(primary=digits, alternate=None, country="other", kind="unknown")

    ddd = digits[2:4]
    local = digits[4:]

    if len(local) == 9:
        if local[0] != "9":
            # 9 digits not starting with 9 is not a valid BR number; keep as-is, no alternate.
            return PhoneForms(primary=digits, alternate=None, country="BR", kind="unknown")
        return PhoneForms(
            primary=digits,
            alternate=f"55{ddd}{local[1:]}",
            country="BR",
            kind="mobile",
        )

    # 8-digit local part: mobiles start with 6-9 (pre-migration form), landlines with 2-5.
    if local[0] in "6789":
        return PhoneForms(
            primary=f"55{ddd}9{local}",
            alternate=digits,
            country="BR",
            kind="mobile",
        )
    return PhoneForms(primary=digits, alternate=None, country="BR", kind="landline")


def variants(raw: str | None) -> list[str]:
    """All dialable forms of ``raw`` (empty list when unusable)."""
    forms = normalize_br(raw)
    return forms.variants if forms else []


def same_number(a: str | None, b: str | None) -> bool:
    """True when two raw strings denote the same BR number in any form."""
    va, vb = set(variants(a)), set(variants(b))
    return bool(va & vb)
