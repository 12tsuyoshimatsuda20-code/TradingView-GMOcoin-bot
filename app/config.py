from __future__ import annotations

from functools import lru_cache
from typing import Optional, Set

from pydantic import BaseSettings, Field, validator


class Settings(BaseSettings):
    webhook_token: str = Field(..., env="WEBHOOK_TOKEN")
    gmo_api_key: str = Field(..., env="GMO_API_KEY")
    gmo_api_secret: str = Field(..., env="GMO_API_SECRET")
    allowed_symbols: str = Field("BTC_JPY", env="ALLOWED_SYMBOLS")
    entry_policy: str = Field("ignore", env="ENTRY_POLICY")
    max_skew_seconds: int = Field(60, env="MAX_SKEW_SECONDS")
    qty_step: float = Field(0.01, env="QTY_STEP")
    discord_webhook_url: Optional[str] = Field(None, env="DISCORD_WEBHOOK_URL")
    env: str = Field("dev", env="ENV")
    idempotency_ttl_seconds: int = Field(600, env="IDEMPOTENCY_TTL_SECONDS")
    status_cache_size: int = Field(100, env="STATUS_CACHE_SIZE")
    gmo_base_url: str = Field("https://api.coin.z.com", env="GMO_BASE_URL")

    @validator("entry_policy")
    def validate_entry_policy(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"ignore"}:
            raise ValueError("ENTRY_POLICY must be 'ignore'")
        return normalized

    @validator("allowed_symbols")
    def normalize_symbols(cls, value: str) -> str:
        return value.replace(" ", "")

    @property
    def allowed_symbol_set(self) -> Set[str]:
        return {symbol for symbol in self.allowed_symbols.split(",") if symbol}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
