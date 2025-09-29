from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, root_validator


class Mode(str, Enum):
    ENTRY = "ENTRY"
    CLOSE = "CLOSE"


class WebhookRequest(BaseModel):
    token: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    ts: str = Field(..., min_length=1)
    mode: Mode
    symbol: str = Field(..., min_length=1)
    side: Optional[Literal["BUY", "SELL"]] = None
    size: Optional[float] = None

    @root_validator
    def validate_entry_payload(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        side = values.get("side")
        size = values.get("size")
        if mode == Mode.ENTRY:
            if side is None:
                raise ValueError("ENTRY requires side")
            if size is None:
                raise ValueError("ENTRY requires size")
            if size <= 0:
                raise ValueError("ENTRY size must be greater than zero")
        return values


class WebhookResponse(BaseModel):
    status: str
    detail: str
    event_id: Optional[str]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class PositionSummary(BaseModel):
    symbol: str
    side: Optional[str]
    size: float


class LastEvent(BaseModel):
    event_id: Optional[str]
    mode: Optional[Mode]
    ts: Optional[datetime]
    status: Optional[str]
    detail: Optional[str]


class RetryStats(BaseModel):
    entry_retries: int = 0
    close_retries: int = 0


class StatusResponse(BaseModel):
    environment: Optional[str]
    last_event: LastEvent
    position: PositionSummary
    ws_connected: bool
    retry_stats: RetryStats
    version: Optional[str]

