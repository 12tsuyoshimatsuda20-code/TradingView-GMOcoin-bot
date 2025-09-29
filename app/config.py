import os
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _parse_decimal(value: str | None, *, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return default


def _parse_int(value: str | None, *, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _parse_list(value: str | None) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    webhook_token: str = field(default_factory=lambda: _get_env("WEBHOOK_TOKEN", ""))
    gmo_api_key: str = field(default_factory=lambda: _get_env("GMO_API_KEY", ""))
    gmo_api_secret: str = field(default_factory=lambda: _get_env("GMO_API_SECRET", ""))
    allowed_symbols: List[str] = field(
        default_factory=lambda: _parse_list(_get_env("ALLOWED_SYMBOLS", "BTC_JPY"))
    )
    entry_policy: str = field(default_factory=lambda: _get_env("ENTRY_POLICY", "ignore"))
    max_skew_seconds: int = field(
        default_factory=lambda: _parse_int(_get_env("MAX_SKEW_SECONDS"), default=60)
    )
    qty_step: Decimal = field(
        default_factory=lambda: _parse_decimal(_get_env("QTY_STEP"), default=Decimal("0.01"))
    )
    notify_discord_webhook_url: str | None = field(
        default_factory=lambda: _get_env("NOTIFY_DISCORD_WEBHOOK_URL")
    )
    environment: str = field(default_factory=lambda: _get_env("ENV", "prod") or "prod")
    ws_enabled: bool = field(
        default_factory=lambda: _parse_bool(_get_env("WS_ENABLED"), default=True)
    )
    dry_run: bool = field(
        default_factory=lambda: _parse_bool(_get_env("DRY_RUN"), default=False)
    )
    discord_timeout: float = field(
        default_factory=lambda: float(_get_env("DISCORD_TIMEOUT", "5"))
    )
    idempotency_ttl: timedelta = field(
        default_factory=lambda: timedelta(
            seconds=_parse_int(_get_env("IDEMPOTENCY_TTL_SECONDS"), default=600)
        )
    )
    retry_limit: int = field(
        default_factory=lambda: _parse_int(_get_env("RETRY_LIMIT"), default=3)
    )
    log_level: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO") or "INFO")

    def ensure_valid(self) -> None:
        if not self.webhook_token:
            raise ValueError("WEBHOOK_TOKEN must be set")
        if not self.gmo_api_key or not self.gmo_api_secret:
            raise ValueError("GMO API credentials must be set")
        if self.qty_step <= 0:
            raise ValueError("QTY_STEP must be positive")
        if "BTC_JPY" not in self.allowed_symbols:
            raise ValueError("BTC_JPY must be included in ALLOWED_SYMBOLS")
        if self.entry_policy != "ignore":
            raise ValueError("ENTRY_POLICY must be 'ignore' for this release")


settings = Settings()
