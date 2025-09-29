from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, root_validator, validator

from .config import settings


class WebhookPayload(BaseModel):
    token: str
    event_id: str = Field(..., min_length=1)
    ts: str
    mode: str
    symbol: str
    side: Optional[str] = None
    size: Optional[Decimal] = None

    class Config:
        anystr_strip_whitespace = True

    @root_validator(pre=True)
    def check_templates(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in values.items():
            if isinstance(value, str) and "{{" in value:
                raise ValueError(f"field '{key}' contains template placeholder")
        return values

    @validator("token")
    def token_must_match(cls, v: str) -> str:
        if v != settings.webhook_token:
            raise ValueError("invalid token")
        return v

    @validator("mode")
    def mode_must_be_valid(cls, v: str) -> str:
        allowed = {"ENTRY", "CLOSE"}
        if v not in allowed:
            raise ValueError("mode must be ENTRY or CLOSE")
        return v

    @validator("symbol")
    def symbol_allowed(cls, v: str) -> str:
        if v not in settings.allowed_symbols:
            raise ValueError("symbol not allowed")
        return v

    @validator("ts")
    def ts_format(cls, v: str) -> str:
        try:
            parsed = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError("ts must be ISO8601 UTC with 'Z'") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")

    @root_validator
    def validate_entry_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        side = values.get("side")
        size = values.get("size")
        if mode == "ENTRY":
            if side not in {"BUY", "SELL"}:
                raise ValueError("ENTRY requires side BUY or SELL")
            if size is None:
                raise ValueError("ENTRY requires size")
            if size <= 0:
                raise ValueError("size must be positive")
        if mode == "CLOSE":
            if side is not None:
                raise ValueError("CLOSE must not include side")
            if size not in (None, Decimal(0)):
                raise ValueError("CLOSE must not include size")
        return values

    def timestamp(self) -> datetime:
        return datetime.strptime(self.ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class WebhookResponse(BaseModel):
    status: str
    detail: Optional[str] = None
    event_id: Optional[str] = None
    ignored: Optional[bool] = None
    duplicate: Optional[bool] = None
    payload: Optional[Dict[str, Any]] = None
