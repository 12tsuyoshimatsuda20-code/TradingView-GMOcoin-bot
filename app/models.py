"""Pydantic models for request and response payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, validator

SignalType = Literal["ENTRY", "CLOSE"]
SideType = Literal["BUY", "SELL"]


class SignalBase(BaseModel):
    type: SignalType
    symbol: str
    side: SideType
    id: str = Field(..., min_length=1)
    ts: int = Field(..., ge=0, description="Unix timestamp in milliseconds")
    note: Optional[str] = None

    class Config:
        extra = "forbid"

    @validator("symbol")
    def uppercase_symbol(cls, value: str) -> str:
        return value.upper()


class EntrySignal(SignalBase):
    type: Literal["ENTRY"]
    size: str = Field(..., description="Size as string per GMO specification")

    @validator("size")
    def ensure_decimal_string(cls, value: str) -> str:
        if not value or value.strip() == "":
            raise ValueError("size must not be empty")
        try:
            float(value)
        except ValueError as exc:
            raise ValueError("size must be a numeric string") from exc
        return value


class CloseSignal(SignalBase):
    type: Literal["CLOSE"]


TradingViewSignal = Annotated[Union[EntrySignal, CloseSignal], Field(discriminator="type")]


class WebhookResponse(BaseModel):
    id: str
    type: SignalType
    symbol: str
    side: SideType
    dry_run: bool = Field(..., alias="dryRun")
    duplicated: bool
    executed: bool
    message: str

    class Config:
        allow_population_by_field_name = True


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class StatusResponse(BaseModel):
    trading_enabled: bool = Field(..., alias="tradingEnabled")
    allowed_symbols: list[str] = Field(..., alias="allowedSymbols")
    last_event: Optional["EventRecord"] = Field(None, alias="lastEvent")
    server_time: datetime = Field(..., alias="serverTime")

    class Config:
        allow_population_by_field_name = True


class EventRecord(BaseModel):
    id: str
    type: SignalType
    symbol: str
    side: SideType
    size: Optional[str]
    ts: int
    received_at: int


StatusResponse.update_forward_refs()
