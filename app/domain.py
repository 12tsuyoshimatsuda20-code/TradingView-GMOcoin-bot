from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from loguru import logger

from .infra.gmocoin_client import GMOCoinClient
from .infra.positions import PositionState, PositionsService
from .notify import DiscordNotifier


@dataclass
class DomainResult:
    action: Literal["entry", "close", "ignored", "noop"]
    position: PositionState
    details: dict


class TradingService:
    def __init__(
        self,
        *,
        symbol: str,
        entry_policy: str,
        positions_service: PositionsService,
        gmocoin_client: GMOCoinClient,
        notifier: DiscordNotifier,
    ) -> None:
        self.symbol = symbol
        self.entry_policy = entry_policy
        self.positions_service = positions_service
        self.gmocoin_client = gmocoin_client
        self.notifier = notifier

    async def process_entry(self, payload) -> DomainResult:
        state = await self.positions_service.fetch_state(self.symbol)
        if state.has_position:
            if self.entry_policy == "ignore":
                reason = (
                    f"Existing position detected (side={state.side}, size={state.size:.5f})."
                )
                await self.notifier.notify_ignored(payload.event_id, reason)
                return DomainResult("ignored", state, {"reason": "position_exists"})
            logger.error("Unsupported entry policy: %s", self.entry_policy)
            raise ValueError(f"Unsupported entry policy: {self.entry_policy}")

        await self.gmocoin_client.place_market_order(
            self.symbol, payload.side, float(payload.size)
        )
        await self.notifier.notify_entry_success(
            payload.event_id, payload.side, float(payload.size)
        )
        logger.info(
            "ENTRY executed: event=%s side=%s size=%s",
            payload.event_id,
            payload.side,
            payload.size,
        )
        return DomainResult(
            "entry",
            PositionState(payload.side, float(payload.size)),
            {"side": payload.side, "size": float(payload.size)},
        )

    async def process_close(self, payload) -> DomainResult:
        state = await self.positions_service.fetch_state(self.symbol)
        if not state.has_position:
            await self.notifier.notify_no_position(payload.event_id)
            return DomainResult("noop", state, {"reason": "no_position"})

        close_side = "SELL" if state.side == "BUY" else "BUY"
        await self.gmocoin_client.place_market_order(
            self.symbol, close_side, float(state.size)
        )
        await self.notifier.notify_close_success(
            payload.event_id, state.side, float(state.size)
        )
        logger.info(
            "CLOSE executed: event=%s closed_side=%s size=%s",
            payload.event_id,
            state.side,
            state.size,
        )
        return DomainResult(
            "close",
            PositionState("NONE", 0.0),
            {"closed_side": state.side, "size": float(state.size)},
        )
