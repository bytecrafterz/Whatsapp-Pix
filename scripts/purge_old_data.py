"""Apply the 12-month retention policy promised on /privacidade.

The public privacy page (§7) states that order and message data — including the
consent evidence — is deleted or anonymised 12 months after the order. This script
is what performs it; ``deploy/pix-retention.timer`` runs it once a day on the server.

It only ever touches rows older than the cutoff, keeps the aggregate counters intact
(the Início page still works) and NEVER removes an opt-out: that list is what keeps a
customer who said SAIR from being messaged again.

Usage::

    uv run python scripts/purge_old_data.py --dry-run     # count, write nothing
    uv run python scripts/purge_old_data.py               # apply (365 days)
    uv run python scripts/purge_old_data.py --days 180    # a shorter policy
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.retention import RETENTION_DAYS, purge  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="purge_old_data.py",
        description="Apaga/anonimiza dados antigos conforme a politica de privacidade.",
    )
    p.add_argument(
        "--days",
        type=int,
        default=RETENTION_DAYS,
        help=f"idade minima em dias (padrao {RETENTION_DAYS} = 12 meses)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="apenas conta o que seria apagado, sem gravar nada",
    )
    return p


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.days < 1:
        print("--days precisa ser pelo menos 1")
        return 2

    # session_scope commits on success and rolls back on any error, so a half-applied
    # purge is impossible.
    with db.session_scope() as session:
        result = purge(session, older_than_days=args.days, dry_run=args.dry_run)

    prefix = "SIMULACAO (nada foi gravado): " if args.dry_run else ""
    print(f"{prefix}{result.summary()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
