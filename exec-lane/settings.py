from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from decimal import Decimal
from typing import Optional

from pydantic import BaseSettings, Field, validator


class Settings(BaseSettings):
    webhook_token: str = Field(..., env="WEBHOOK_TOKEN")
    gmo_api_key: str = Field(..., env="GMO_API_KEY")
    gmo_api_secret: str = Field(..., env="GMO_API_SECRET")
    discord_webhook: Optional[str] = Field(None, env="DISCORD_WEBHOOK")

    log_level: str = Field("INFO", env="LOG_LEVEL")
    max_skew_seconds: int = Field(60, env="MAX_SKEW_SECONDS")
    qty_step: Decimal = Field(Decimal("0.01"), env="QTY_STEP")
    symbol: str = Field("BTC_JPY", env="SYMBOL")
    event_id_ttl_seconds: int = Field(900, env="EVENT_ID_TTL_SECONDS")

    log_rotation: str = Field("10 MB", env="LOG_ROTATION")
    log_retention: str = Field("7 days", env="LOG_RETENTION")
    discord_timeout: float = Field(5.0, env="DISCORD_TIMEOUT")

    class Config:
        env_file = Path(__file__).resolve().parent.parent / "config" / ".env"
        env_file_encoding = "utf-8"

    @validator("qty_step", pre=True)
    def _validate_qty_step(cls, v: str | Decimal) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    @property
    def log_directory(self) -> Path:
        base = Path(__file__).resolve().parent.parent / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
