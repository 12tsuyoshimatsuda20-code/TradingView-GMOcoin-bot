"""Environment driven application settings."""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Dict, List

from pydantic import BaseSettings, Field, root_validator


class Settings(BaseSettings):
    webhook_token: str = Field(..., env="WEBHOOK_TOKEN")
    gmo_api_key: str = Field(..., env="GMO_API_KEY")
    gmo_api_secret: str = Field(..., env="GMO_API_SECRET")
    trading_enabled: bool = Field(True, env="TRADING_ENABLED")
    allowed_symbols: List[str] = Field(default_factory=list, env="ALLOWED_SYMBOLS")
    max_skew_seconds: int = Field(60, env="MAX_SKEW_SECONDS")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    port: int = Field(8000, env="PORT")
    timezone: str = Field("Asia/Tokyo", env="TZ")
    size_decimals: Dict[str, int] = Field(default_factory=dict)

    api_base: str = "https://api.coin.z.com"

    class Config:
        env_file = ".env"
        case_sensitive = False

    @root_validator(pre=True)
    def populate_size_decimals(cls, values: Dict[str, str]) -> Dict[str, str]:
        prefix = "SIZE_DECIMALS_"
        mapping: Dict[str, int] = {}
        for key, value in os.environ.items():
            if key.startswith(prefix) and value:
                symbol = key[len(prefix) :]
                try:
                    mapping[symbol] = int(value)
                except ValueError as exc:  # pragma: no cover - configuration error
                    raise ValueError(f"Invalid decimal setting for {symbol}: {value}") from exc
        values.setdefault("size_decimals", mapping)
        return values

    @root_validator
    def normalize_symbols(cls, values: Dict[str, object]) -> Dict[str, object]:
        symbols = values.get("allowed_symbols") or []
        if isinstance(symbols, str):
            symbols = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        values["allowed_symbols"] = symbols
        return values

    def get_size_decimals(self, symbol: str) -> int:
        key = symbol.upper()
        if key not in self.size_decimals:
            raise ValueError(f"Size decimal configuration missing for {key}")
        return self.size_decimals[key]


@lru_cache()
def get_settings() -> Settings:
    """Return cached settings instance."""

    return Settings()
