"""Typed access to the ``settings`` table.

Precedence for every key: **DB row → environment (Settings) → hard-coded default**.
The panel writes DB rows; a deploy never has to change env vars for a template
switch, a delay change or a quiet-hours tweak.

A :class:`SettingsStore` loads the whole table once (lazily) and caches it for
its lifetime — one instance per request (FastAPI dependency) or per worker tick,
so a change made in the panel is picked up by the next tick / request without
being re-queried dozens of times inside one unit of work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import time
from typing import Literal

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Setting

SettingKind = Literal["bool", "int", "time", "str"]


@dataclass(frozen=True)
class SettingDef:
    """Metadata for one settings key (also drives the panel form)."""

    key: str
    kind: SettingKind
    default: Callable[[Settings], str]
    label: str  # pt-BR label for the panel
    help: str = ""  # pt-BR hint for the panel
    min: int | None = None
    max: int | None = None
    # Which panel page edits it: "pix" → Configurações, "cart" → Carrinho.
    group: str = "pix"


# --- abandoned-cart sequence -----------------------------------------------------------

CART_MAX_STEPS = 3
CART_DEFAULT_DELAYS = (60, 24 * 60, 48 * 60)  # minutes after the abandonment
CART_DEFAULT_TEMPLATE = "carrinho_abandonado_v1"
CART_DEFAULT_PARAMS = "first_name,product,coupon"
# A step later than this after the abandonment is never sent (scheduling.MAX_CART_AGE
# allows a day of slack on top for quiet hours and retries).
CART_MAX_DELAY_MINUTES = 3 * 24 * 60


@dataclass(frozen=True)
class CartStepConfig:
    """Everything the worker needs to send step ``step`` of a cart sequence."""

    step: int
    delay_minutes: int
    template_name: str
    language: str
    url_button_index: int  # -1 = the template has no URL button
    params: tuple[str, ...]


def _cart_step_defs(i: int) -> tuple[SettingDef, ...]:
    return (
        SettingDef(
            f"cart_step{i}_delay_minutes",
            "int",
            lambda s, i=i: str(CART_DEFAULT_DELAYS[i - 1]),
            f"Mensagem {i}: minutos após o abandono",
            "Contados a partir do momento em que a Kirvano avisou o abandono.",
            min=5,
            max=CART_MAX_DELAY_MINUTES,
            group="cart",
        ),
        SettingDef(
            f"cart_step{i}_template",
            "str",
            lambda s, i=i: CART_DEFAULT_TEMPLATE if i == 1 else "",
            f"Mensagem {i}: nome do modelo",
            "Nome exato do modelo de Marketing aprovado no WhatsApp Manager.",
            group="cart",
        ),
        SettingDef(
            f"cart_step{i}_url_button_index",
            "int",
            lambda s: "0",
            f"Mensagem {i}: índice do botão de link",
            "Posição do botão de link no modelo (0 = primeiro). Use -1 se não houver botão.",
            min=-1,
            max=9,
            group="cart",
        ),
        SettingDef(
            f"cart_step{i}_params",
            "str",
            lambda s, i=i: CART_DEFAULT_PARAMS if i == 1 else "first_name,product",
            f"Mensagem {i}: ordem dos parâmetros",
            "Chaves separadas por vírgula: first_name, customer_name, product, amount, "
            "amount_full, coupon, link. Deixe vazio se o modelo não tiver variáveis.",
            group="cart",
        ),
    )


SETTING_DEFS: tuple[SettingDef, ...] = (
    SettingDef(
        "enabled",
        "bool",
        lambda s: "true",
        "Lembretes ativados",
        "Quando desligado, nenhum lembrete novo é agendado nem enviado.",
    ),
    SettingDef(
        "delay_minutes",
        "int",
        lambda s: str(s.reminder_delay_minutes),
        "Minutos de espera",
        "Tempo após o PIX ser gerado até enviar o lembrete.",
        min=0,
        max=24 * 60,
    ),
    SettingDef(
        "quiet_start",
        "time",
        lambda s: s.quiet_start,
        "Início do horário silencioso",
        "Horário de Brasília. Lembretes não são enviados dentro deste intervalo.",
    ),
    SettingDef(
        "quiet_end",
        "time",
        lambda s: s.quiet_end,
        "Fim do horário silencioso",
        "Lembretes que caírem no silêncio são adiados para este horário.",
    ),
    SettingDef(
        "daily_recipient_limit",
        "int",
        lambda s: str(s.daily_recipient_limit),
        "Limite diário de contatos",
        "Máximo de contatos distintos em 24h (250 até a verificação do negócio).",
        min=1,
        max=1_000_000,
    ),
    SettingDef(
        "template_name",
        "str",
        lambda s: s.template_name,
        "Nome do modelo",
        "Nome exato do modelo aprovado no WhatsApp Manager.",
    ),
    SettingDef(
        "template_language",
        "str",
        lambda s: s.template_language,
        "Idioma do modelo",
        "Código do idioma do modelo, por exemplo pt_BR.",
    ),
    SettingDef(
        "url_button_index",
        "int",
        lambda s: s.template_url_button_index,
        "Índice do botão de link",
        "Posição do botão de URL no modelo (0 = primeiro). Use -1 se não houver botão.",
        min=-1,
        max=9,
    ),
    SettingDef(
        "template_params",
        "str",
        lambda s: s.template_params,
        "Ordem dos parâmetros",
        "Chaves separadas por vírgula: first_name, sale_id, amount, expiry, product, "
        "customer_name, amount_full, page_url.",
    ),
    SettingDef(
        "checkout_url",
        "str",
        lambda s: s.kirvano_checkout_url or "",
        "Link do checkout",
        "Usado na página do PIX expirado quando a Kirvano não envia o link de recuperação.",
    ),
    # --- abandoned cart (panel page "Carrinho") ---
    SettingDef(
        "cart_enabled",
        "bool",
        # Off until the client's Marketing template is approved and configured.
        lambda s: "false",
        "Recuperação de carrinho ativada",
        "Quando desligada, nenhum carrinho abandonado recebe mensagem.",
        group="cart",
    ),
    SettingDef(
        "cart_steps",
        "int",
        lambda s: "1",
        "Quantidade de mensagens",
        "Quantas mensagens cada carrinho abandonado recebe, na ordem abaixo (1 a 3).",
        min=1,
        max=CART_MAX_STEPS,
        group="cart",
    ),
    SettingDef(
        "cart_coupon",
        "str",
        lambda s: "",
        "Cupom",
        "Código enviado no parâmetro coupon. Precisa estar criado e ativo na Kirvano.",
        group="cart",
    ),
    SettingDef(
        "cart_template_language",
        "str",
        lambda s: s.template_language,
        "Idioma dos modelos",
        "Código do idioma dos modelos de carrinho, por exemplo pt_BR.",
        group="cart",
    ),
    SettingDef(
        "cart_checkout_url",
        "str",
        lambda s: s.kirvano_checkout_url or "",
        "Link do checkout (reserva)",
        "Para onde o botão leva quando a Kirvano não envia o link do próprio carrinho.",
        group="cart",
    ),
    *(d for i in range(1, CART_MAX_STEPS + 1) for d in _cart_step_defs(i)),
)

SETTING_KEYS: tuple[str, ...] = tuple(d.key for d in SETTING_DEFS)
_DEFS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in SETTING_DEFS}

_TRUE = {"1", "true", "yes", "on", "sim", "y", "t"}
_FALSE = {"0", "false", "no", "off", "nao", "não", "n", "f", ""}


class SettingValueError(ValueError):
    """Raised by :meth:`SettingsStore.set` on an invalid value (message is pt-BR)."""


def parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise SettingValueError(f"Valor inválido para verdadeiro/falso: {raw!r}")


def parse_time(raw: str) -> time:
    v = raw.strip()
    try:
        hh, mm = v.split(":", 1)
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise SettingValueError(f"Horário inválido (use HH:MM): {raw!r}") from exc


def _normalise(defn: SettingDef, value: object) -> str:
    """Validate ``value`` for ``defn`` and return its canonical string form."""
    if defn.kind == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        return "true" if parse_bool(str(value)) else "false"
    if defn.kind == "int":
        try:
            num = int(str(value).strip())
        except ValueError as exc:
            raise SettingValueError(f"Número inválido: {value!r}") from exc
        if defn.min is not None and num < defn.min:
            raise SettingValueError(f"{defn.label}: mínimo é {defn.min}")
        if defn.max is not None and num > defn.max:
            raise SettingValueError(f"{defn.label}: máximo é {defn.max}")
        return str(num)
    if defn.kind == "time":
        if isinstance(value, time):
            return value.strftime("%H:%M")
        return parse_time(str(value)).strftime("%H:%M")
    text = str(value if value is not None else "").strip()
    if defn.key in ("template_name", "template_language", "cart_template_language") and not text:
        raise SettingValueError(f"{defn.label} não pode ficar vazio")
    if defn.key.startswith("cart_step") and defn.key.endswith("_params"):
        from app.whatsapp import CART_PARAM_KEYS  # local: whatsapp imports this module

        keys = [p.strip() for p in text.split(",") if p.strip()]
        # Empty IS allowed here: a cart template with no variables takes no body
        # component at all (see whatsapp.build_cart_payload).
        unknown = [k for k in keys if k not in CART_PARAM_KEYS]
        if unknown:
            raise SettingValueError(
                f"{defn.label}: parâmetro desconhecido {', '.join(unknown)} "
                f"(use: {', '.join(sorted(CART_PARAM_KEYS))})"
            )
        return ",".join(keys)
    if defn.key == "template_params":
        # Mirrors app.panel.validate_setting so the check also holds for callers that
        # do not go through the form. An unknown key silently became an EMPTY template
        # parameter and Meta rejected every reminder with #132000/#132012.
        from app.whatsapp import TEMPLATE_PARAM_KEYS  # local: whatsapp imports this module

        keys = [p.strip() for p in text.split(",") if p.strip()]
        if not keys:
            raise SettingValueError(f"{defn.label}: informe pelo menos um parâmetro")
        unknown = [k for k in keys if k not in TEMPLATE_PARAM_KEYS]
        if unknown:
            raise SettingValueError(
                f"{defn.label}: parâmetro desconhecido {', '.join(unknown)} "
                f"(use: {', '.join(sorted(TEMPLATE_PARAM_KEYS))})"
            )
        return ",".join(keys)
    return text


class SettingsStore:
    """Read/write view over the settings table with typed accessors.

    Writes are flushed into the given session but **not committed** — the
    caller (request handler) owns the transaction boundary.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._cache: dict[str, str] | None = None

    # --- raw access -------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if self._cache is None:
            rows = self.session.execute(select(Setting)).scalars().all()
            self._cache = {r.key: r.value for r in rows}
        return self._cache

    def refresh(self) -> None:
        self._cache = None

    def get_raw(self, key: str) -> str | None:
        """DB value only (``None`` when the panel never set it)."""
        return self._load().get(key)

    def get(self, key: str) -> str:
        """Effective value (DB → env → default) as a string."""
        if key not in _DEFS_BY_KEY:
            raise KeyError(key)
        raw = self.get_raw(key)
        if raw is not None:
            return raw
        return _DEFS_BY_KEY[key].default(self.settings)

    def set(self, key: str, value: object) -> str:
        """Validate and store ``value`` for ``key``; returns the canonical string stored."""
        defn = _DEFS_BY_KEY.get(key)
        if defn is None:
            raise KeyError(key)
        text = _normalise(defn, value)
        row = self.session.get(Setting, key)
        now = clock.utcnow()
        if row is None:
            row = Setting(key=key, value=text, updated_at=now)
            self.session.add(row)
        else:
            row.value = text
            row.updated_at = now
        self.session.flush()
        self._load()[key] = text
        return text

    def set_many(self, values: Mapping[str, object]) -> dict[str, str]:
        """Validate ALL values first, then store them — so a bad field leaves the rest untouched."""
        cleaned = {
            k: _normalise(_DEFS_BY_KEY[k], v) for k, v in values.items() if k in _DEFS_BY_KEY
        }
        return {k: self.set(k, v) for k, v in cleaned.items()}

    def as_dict(self) -> dict[str, str]:
        return {k: self.get(k) for k in SETTING_KEYS}

    @staticmethod
    def definitions() -> Iterable[SettingDef]:
        return SETTING_DEFS

    # --- typed accessors ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return parse_bool(self.get("enabled"))

    @property
    def delay_minutes(self) -> int:
        return int(self.get("delay_minutes"))

    @property
    def quiet_start(self) -> time:
        return parse_time(self.get("quiet_start"))

    @property
    def quiet_end(self) -> time:
        return parse_time(self.get("quiet_end"))

    @property
    def daily_recipient_limit(self) -> int:
        return int(self.get("daily_recipient_limit"))

    @property
    def template_name(self) -> str:
        return self.get("template_name")

    @property
    def template_language(self) -> str:
        return self.get("template_language")

    @property
    def url_button_index(self) -> int:
        """0-based URL button position; ``-1`` means the template has no URL button."""
        return int(self.get("url_button_index"))

    @property
    def template_params(self) -> list[str]:
        return [p.strip() for p in self.get("template_params").split(",") if p.strip()]

    @property
    def checkout_url(self) -> str:
        return self.get("checkout_url")

    # --- abandoned cart -------------------------------------------------------------

    @property
    def cart_enabled(self) -> bool:
        return parse_bool(self.get("cart_enabled"))

    @property
    def cart_steps(self) -> int:
        return max(1, min(CART_MAX_STEPS, int(self.get("cart_steps"))))

    @property
    def cart_coupon(self) -> str:
        return self.get("cart_coupon")

    @property
    def cart_template_language(self) -> str:
        return self.get("cart_template_language")

    @property
    def cart_checkout_url(self) -> str:
        return self.get("cart_checkout_url")

    def cart_step(self, step: int) -> CartStepConfig:
        """Configuration of step ``step`` (1-based), whether or not it is enabled."""
        if not 1 <= step <= CART_MAX_STEPS:
            raise KeyError(step)
        params = self.get(f"cart_step{step}_params")
        return CartStepConfig(
            step=step,
            delay_minutes=int(self.get(f"cart_step{step}_delay_minutes")),
            template_name=self.get(f"cart_step{step}_template").strip(),
            language=self.cart_template_language,
            url_button_index=int(self.get(f"cart_step{step}_url_button_index")),
            params=tuple(p.strip() for p in params.split(",") if p.strip()),
        )

    def cart_step_configs(self) -> list[CartStepConfig]:
        """The ENABLED steps, in order (``cart_steps`` of them)."""
        return [self.cart_step(i) for i in range(1, self.cart_steps + 1)]

    @classmethod
    def group_definitions(cls, group: str) -> list[SettingDef]:
        return [d for d in SETTING_DEFS if d.group == group]


def get_settings_store(
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SettingsStore:
    """FastAPI dependency: a per-request store bound to the request session."""
    return SettingsStore(session, settings)
