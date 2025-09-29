from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class WebhookPayload(BaseModel):
    token: str = Field(..., description="固定認証トークン")
    event_id: str = Field(..., description="TradingViewイベントID")
    ts: datetime = Field(..., description="TradingView送信時刻")
    symbol: str = Field(..., description="銘柄コード")
    size: float = Field(..., description="発注数量")
    mode: Literal["ENTRY", "CLOSE"] = Field(..., description="エントリー/クローズ")
    side: Optional[Literal["BUY", "SELL"]] = Field(
        None, description="注文方向（ENTRYのみ必須)"
    )
    entry_price_hint: Optional[float] = Field(
        None, description="参考価格（成行のため情報のみ)"
    )
    tp1_price: Optional[float] = Field(None, description="将来拡張用TP1")
    tp2_price: Optional[float] = Field(None, description="将来拡張用TP2")
    tp3_price: Optional[float] = Field(None, description="将来拡張用TP3")
    interval: Optional[str] = Field(None, description="TradingViewタイムフレーム")
    exchange: Optional[str] = Field(None, description="送信元取引所ラベル")

    @model_validator(mode="after")
    def validate_side_for_entry(self) -> "WebhookPayload":
        if self.mode == "ENTRY" and self.side is None:
            raise ValueError("ENTRYモードではsideが必須です")
        return self


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class WebhookResponse(BaseModel):
    status: Literal["ok", "duplicate", "error"]
    event_id: str
    action: Optional[Literal["entry", "close", "ignored", "noop"]] = None
    reason: Optional[str] = None


class PositionInfo(BaseModel):
    side: Literal["BUY", "SELL", "NONE"]
    size: float


class LastEventInfo(BaseModel):
    id: Optional[str] = None
    at: Optional[str] = None
    action: Optional[str] = None


class StatusResponse(BaseModel):
    position: PositionInfo
    last_event: LastEventInfo
    version: str


class StoredEvent(BaseModel):
    event_id: str
    received_at: datetime
    mode: str
    status: str
    action: Optional[str] = None
    response: Optional[str] = None
    error: Optional[str] = None
