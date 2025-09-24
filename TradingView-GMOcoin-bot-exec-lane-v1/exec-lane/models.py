"""Pydantic models for webhook payloads and runtime state."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field, validator


class WebhookPayload(BaseModel):
    token: str = Field(..., description="Shared secret from TradingView")
    event_id: str = Field(..., description="Unique identifier for deduplication")
    ts: str = Field(..., description="ISO8601 timestamp from TradingView (UTC)")
    mode: Literal["ENTRY", "CLOSE"]
    symbol: str
    side: Optional[Literal["BUY", "SELL"]] = Field(
        default=None, description="Order side. Required for ENTRY"
    )
    size: Optional[float] = Field(default=None, description="Order size. Required for ENTRY")

    @validator("ts")
    def validate_ts(cls, value: str) -> str:
        candidate = value
        if candidate.endswith("Z"):
            candidate = candidate.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("ts must be ISO8601 with timezone") from exc
        return value

    @validator("size")
    def validate_size(cls, value: Optional[float], values: Dict[str, object]) -> Optional[float]:
        mode = values.get("mode")
        if mode == "ENTRY" and (value is None or value <= 0):
            raise ValueError("size must be positive for ENTRY")
        return value

    @validator("side")
    def validate_side(cls, value: Optional[str], values: Dict[str, object]) -> Optional[str]:
        mode = values.get("mode")
        if mode == "ENTRY" and value is None:
            raise ValueError("side is required for ENTRY")
        return value


class Settings(BaseModel):
    webhook_token: str
    allowed_symbols: str = "BTC_JPY"
    entry_policy: Literal["ignore"] = "ignore"
    max_skew_seconds: int = 60
    qty_step: float = 0.01
    env: str = "prod"
    discord_webhook_url: Optional[str] = None

    @validator("max_skew_seconds")
    def positive_skew(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("MAX_SKEW_SECONDS must be positive")
        return value

    @validator("qty_step")
    def positive_step(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("QTY_STEP must be positive")
        return value


class BotState(BaseModel):
    last_event_id: Optional[str] = None
    last_event_ts: Optional[str] = None
    ws_connected: bool = False
    retry_stats: Dict[str, int] = Field(default_factory=dict)
    position_cache: Dict[str, object] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True
