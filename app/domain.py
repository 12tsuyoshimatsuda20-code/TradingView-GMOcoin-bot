from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from loguru import logger

from .infra.gmocoin_client import GMOCoinClient
from .infra.positions import PositionState, PositionsService
from .notify import notify_discord


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
        discord_webhook: Optional[str],
    ) -> None:
        self.symbol = symbol
        self.entry_policy = entry_policy
        self.positions_service = positions_service
        self.gmocoin_client = gmocoin_client
        self.discord_webhook = discord_webhook

    async def _safe_notify(
        self,
        title: str,
        description: str,
        color: Literal["green", "gray", "red"],
        fields: list[dict] | None = None,
    ) -> None:
        try:
            await notify_discord(
                self.discord_webhook,
                title,
                description,
                color,
                fields,
            )
        except Exception as exc:
            logger.debug("Discord notify suppressed (domain): {}", repr(exc))

    async def process_entry(self, payload) -> DomainResult:
        state = await self.positions_service.fetch_state(self.symbol)
        if state.has_position:
            if self.entry_policy == "ignore":
                reason = (
                    f"Existing position detected (side={state.side}, size={state.size:.5f})."
                )
                await self._safe_notify(
                    "ENTRY ignored",
                    f"event_id={payload.event_id}\n{reason}",
                    "gray",
                )
                return DomainResult("ignored", state, {"reason": "position_exists"})
            logger.error("Unsupported entry policy: %s", self.entry_policy)
            raise ValueError(f"Unsupported entry policy: {self.entry_policy}")

        await self.gmocoin_client.place_market_order(
            self.symbol, payload.side, float(payload.size)
        )
        await self._safe_notify(
            "ENTRY executed",
            f"event_id={payload.event_id}",
            "green",
            [
                {"name": "symbol", "value": payload.symbol, "inline": True},
                {"name": "side", "value": payload.side, "inline": True},
                {"name": "size", "value": f"{float(payload.size):.5f}", "inline": True},
            ],
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
            await self._safe_notify(
                "CLOSE skipped",
                f"event_id={payload.event_id}\nNo open position",
                "gray",
            )
            return DomainResult("noop", state, {"reason": "no_position"})

        close_side = "SELL" if state.side == "BUY" else "BUY"
        await self.gmocoin_client.place_market_order(
            self.symbol, close_side, float(state.size)
        )
        await self._safe_notify(
            "CLOSE executed",
            f"event_id={payload.event_id}",
            "green",
            [
                {"name": "symbol", "value": payload.symbol, "inline": True},
                {"name": "closed_side", "value": state.side, "inline": True},
                {"name": "size", "value": f"{float(state.size):.5f}", "inline": True},
            ],
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
