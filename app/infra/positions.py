from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from loguru import logger

from .gmocoin_client import GMOCoinClient


@dataclass
class PositionState:
    side: Literal["BUY", "SELL", "NONE"]
    size: float

    @property
    def has_position(self) -> bool:
        return self.side != "NONE" and self.size > 0


class PositionsService:
    def __init__(self, client: GMOCoinClient) -> None:
        self._client = client

    async def fetch_state(self, symbol: str) -> PositionState:
        response = await self._client.fetch_open_positions(symbol)
        positions: Sequence[dict] = response.get("data", []) if isinstance(response, dict) else []
        buy_size = 0.0
        sell_size = 0.0
        for entry in positions:
            try:
                size = float(entry.get("size", 0))
            except (TypeError, ValueError):
                logger.warning("Unexpected size format in position: %s", entry)
                continue
            side = entry.get("side")
            if side == "BUY":
                buy_size += size
            elif side == "SELL":
                sell_size += size
            else:
                logger.warning("Unknown side in position payload: %s", entry)

        net = buy_size - sell_size
        if net > 0:
            return PositionState("BUY", round(net, 8))
        if net < 0:
            return PositionState("SELL", round(abs(net), 8))
        return PositionState("NONE", 0.0)
