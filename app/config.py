"""Application configuration.

All configuration comes from **environment variables only** (pydantic-settings).
On the server systemd injects ``/etc/pix-recovery/env``; on the dev machine the
developer exports ``secrets.env`` into the shell. No ``.env`` file is read
implicitly, so secrets never leak through a stray file in the working directory.

Runtime-tunable values (delay, quiet hours, template name, ...) have their
*defaults* here but are overridable from the panel through
``app.settings_store`` (DB wins over env; env wins over the hard-coded default).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide, immutable configuration read from the environment."""

    model_config = SettingsConfigDict(
        env_file=None,  # environment only — never read a file implicitly
        extra="ignore",  # secrets.env also carries deploy-only keys (VPS_*, SSH_*)
        case_sensitive=False,
    )

    # --- general ---------------------------------------------------------------
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./dev.db"
    api_domain: str = "api.jornadaanjo.cloud"
    public_base_url: str | None = None  # derived from api_domain when unset
    business_tz: str = "America/Sao_Paulo"  # quiet hours + human-facing dates

    # --- Kirvano ---------------------------------------------------------------
    kirvano_webhook_token: str | None = None
    kirvano_token_mode: Literal["log", "enforce"] = "log"
    kirvano_tz: str = "America/Sao_Paulo"  # Kirvano timestamps are naive local time
    kirvano_checkout_url: str | None = None
    kirvano_pix_expiry_minutes: int | None = None  # only a fallback when expires_at is absent
    auth_debug_max_rows: int = 50  # log-mode: how many requests get header-name capture

    # --- Meta / WhatsApp Cloud API ---------------------------------------------
    meta_access_token: str | None = None
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None
    meta_phone_number_id: str = "1347340825121720"
    meta_waba_id: str = "958025707339789"
    meta_business_id: str | None = None
    meta_app_id: str | None = None
    meta_graph_version: str = "v23.0"
    meta_graph_base_url: str = "https://graph.facebook.com"
    http_timeout_seconds: float = 15.0

    # --- Panel -----------------------------------------------------------------
    panel_user: str = "admin"
    panel_password: str | None = None

    # --- Reminder defaults (overridable in the panel via settings_store) -------
    reminder_delay_minutes: int = 10
    daily_recipient_limit: int = 250
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    template_name: str = "pix_pendente_v2"
    template_language: str = "pt_BR"
    template_url_button_index: str = "1"  # 0-based position of the URL button; "-1" = none
    template_params: str = "first_name,sale_id,amount,expiry"  # body {{1}}..{{n}} order

    # --- Worker ----------------------------------------------------------------
    worker_poll_seconds: float = 5.0
    worker_batch_size: int = 20
    worker_heartbeat_file: str | None = None
    worker_max_attempts: int = 3
    worker_token_pause_minutes: int = 10  # stop sends this long after a 190/401

    @field_validator("template_url_button_index", mode="before")
    @classmethod
    def _coerce_index(cls, value: object) -> str:
        # Accept ints from the environment/tests but always keep the string form
        # Meta expects in the payload.
        return str(value).strip()

    # --- derived helpers ---------------------------------------------------------
    @property
    def base_url(self) -> str:
        """Public https base URL without trailing slash (used for the PIX page link)."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return f"https://{self.api_domain}"

    def page_url(self, page_token: str) -> str:
        return f"{self.base_url}/p/{page_token}"

    @property
    def graph_base(self) -> str:
        return f"{self.meta_graph_base_url.rstrip('/')}/{self.meta_graph_version}"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def meta_configured(self) -> bool:
        return bool(self.meta_access_token and self.meta_phone_number_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance; FastAPI dependency (override it in tests)."""
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
