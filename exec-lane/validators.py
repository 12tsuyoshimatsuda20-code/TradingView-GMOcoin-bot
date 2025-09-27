from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, validator


class BasePayload(BaseModel):
    token: str
    event_id: str
    ts: datetime = Field(..., alias="ts")
    mode: Literal["ENTRY", "CLOSE"]
    symbol: str

    class Config:
        allow_population_by_field_name = True
        anystr_strip_whitespace = True

    @validator("symbol")
    def validate_symbol(cls, v: str) -> str:
        if v != "BTC_JPY":
            raise ValueError("symbol must be BTC_JPY")
        return v


class EntryPayload(BasePayload):
    side: Literal["BUY", "SELL"]
    size: float

    @validator("size")
    def validate_size(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("size must be positive")
        return v


class ClosePayload(BasePayload):
    side: Optional[str] = None
    size: Optional[float] = None
