"""Panel password stored in the database, so the operator can rotate it himself.

Why this exists
---------------
``PANEL_PASSWORD`` lives in ``/etc/pix-recovery/env`` and changing it means SSH
plus a service restart — i.e. the client would depend on the developer forever
just to rotate a password. That is a bad handover, so the panel keeps an
optional override in the ``settings`` table:

* no row  -> authenticate against ``settings.panel_password`` (the env value)
* a row   -> authenticate against the stored hash, and the env value STOPS working

The env value therefore doubles as a recovery path: delete the row (or run
``scripts/reset_panel_password.py``) and the file-based password is live again.

Hashing is PBKDF2-HMAC-SHA256 from the standard library — no new dependency, and
strong enough for a single-operator panel behind HTTPS. Verification is
constant-time via :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting

#: settings key holding "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>"
PASSWORD_KEY = "panel_password_hash"

ITERATIONS = 600_000
SALT_BYTES = 16
MIN_LENGTH = 10


class PasswordError(ValueError):
    """Raised with a pt-BR message ready to show in the panel."""


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored hash."""
    try:
        algorithm, raw_iterations, salt_hex, hash_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        # A corrupted row must not crash the login screen; it simply fails to match.
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def get_password_hash(session: Session) -> str | None:
    """The stored hash, or ``None`` when the env password is still in charge."""
    row = session.execute(select(Setting).where(Setting.key == PASSWORD_KEY)).scalar_one_or_none()
    if row is None or not row.value:
        return None
    return row.value


def validate_new_password(new: str, confirm: str) -> None:
    """Raise :class:`PasswordError` with a pt-BR message when the pair is unusable."""
    if not new:
        raise PasswordError("Digite a nova senha.")
    if new != confirm:
        raise PasswordError("A confirmação não confere com a nova senha.")
    if len(new) < MIN_LENGTH:
        raise PasswordError(f"A nova senha precisa ter pelo menos {MIN_LENGTH} caracteres.")
    if new.strip() != new:
        raise PasswordError("A senha não pode começar nem terminar com espaço.")
    if not new.isprintable():
        raise PasswordError("A senha tem caracteres inválidos. Use letras, números e símbolos.")


def set_password(session: Session, new_password: str) -> None:
    """Store (or replace) the panel password hash. Caller commits."""
    encoded = hash_password(new_password)
    now = datetime.now(UTC)
    row = session.execute(select(Setting).where(Setting.key == PASSWORD_KEY)).scalar_one_or_none()
    if row is None:
        session.add(Setting(key=PASSWORD_KEY, value=encoded, updated_at=now))
    else:
        row.value = encoded
        row.updated_at = now


def clear_password(session: Session) -> bool:
    """Drop the override so the env password works again. Returns True if a row went."""
    row = session.execute(select(Setting).where(Setting.key == PASSWORD_KEY)).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    return True


def check_panel_password(session: Session, candidate: str, env_password: str | None) -> bool:
    """Authenticate ``candidate`` against the stored hash, else the env password.

    Always constant-time on the branch that runs, and never reveals which
    mechanism is in force.
    """
    stored = get_password_hash(session)
    if stored is not None:
        return verify_password(candidate, stored)
    if not env_password:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), env_password.encode("utf-8"))
