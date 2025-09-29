from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, validator


class WebhookBase(BaseModel):
    token: str
    event_id: str = Field(..., alias="event_id")
    ts: str
    mode: Literal["ENTRY", "CLOSE"]
    symbol: str

    @validator("ts")
    def validate_ts(cls, value: str) -> str:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class EntryPayload(WebhookBase):
    mode: Literal["ENTRY"]
    side: Literal["BUY", "SELL"]
    size: float


class ClosePayload(WebhookBase):
    mode: Literal["CLOSE"]
    side: Optional[str] = None
    size: Optional[float] = None


class WebhookResponse(BaseModel):
    status: str
    detail: str
    ignored: bool = False
    event_id: str


class StatusSnapshot(BaseModel):
    position_size: float
    position_side: Optional[str]
    last_events: list
    websocket_connected: bool
    retries: dict
