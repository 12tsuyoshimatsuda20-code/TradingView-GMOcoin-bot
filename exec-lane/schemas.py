from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Mode(str, Enum):
    ENTRY = "ENTRY"
    CLOSE = "CLOSE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class WebhookPayload(BaseModel):
    token: str = Field(..., min_length=8, max_length=128)
    event_id: str = Field(..., min_length=3, max_length=128)
    ts: datetime
    mode: Mode
    symbol: Literal["BTC_JPY"]
    side: Optional[Side] = None
    size: Optional[float] = Field(default=None, ge=0.0)

    @field_validator("ts")
    @classmethod
    def ensure_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v

    @field_validator("size")
    @classmethod
    def normalize_size(cls, v: Optional[float], info):  # noqa: ANN001
        if info.data.get("mode") == Mode.ENTRY and v is None:
            raise ValueError("size required for ENTRY")
        return v

    @field_validator("side")
    @classmethod
    def ensure_side(cls, v: Optional[Side], info):  # noqa: ANN001
        if info.data.get("mode") == Mode.ENTRY and v is None:
            raise ValueError("side required for ENTRY")
        return v


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class StatusResponse(BaseModel):
    position_qty: float
    position_side: Optional[str]
    last_event_id: Optional[str]
    last_event_ts: Optional[str]
    retry_stats: dict
    ws_connected: bool
    env: str


class WebhookResponse(BaseModel):
    ok: bool = True
    ignored: bool = False
    duplicate: bool = False
    message: Optional[str] = None
    event_id: Optional[str] = None
    gmo_response: Optional[dict] = None


class DiscordMessage(BaseModel):
    level: Literal["info", "warning", "error"]
    title: str
    message: str
    url: Optional[HttpUrl] = None
    extra: Optional[dict] = None
