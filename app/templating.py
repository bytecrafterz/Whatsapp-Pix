"""Shared Jinja2 environment for the panel and the public pages.

Templates live in ``app/templates/``; every page extends ``base.html``.
Filters available in templates: ``brl`` (cents → "1.169,80"), ``brl_symbol``
(cents → "R$ 1.169,80"), ``dt_sp`` ("15/09 às 18:00"), ``dt_sp_full``
("15/09/2026 18:00"); globals: ``first_name``, ``app_version``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import __version__
from app.format import first_name, fmt_brl, fmt_dt_sp, fmt_dt_sp_full

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.autoescape = True
templates.env.filters["brl"] = fmt_brl
templates.env.filters["brl_symbol"] = lambda cents: fmt_brl(cents, symbol=True)
templates.env.filters["dt_sp"] = fmt_dt_sp
templates.env.filters["dt_sp_full"] = fmt_dt_sp_full
templates.env.globals["first_name"] = first_name
templates.env.globals["app_version"] = __version__
