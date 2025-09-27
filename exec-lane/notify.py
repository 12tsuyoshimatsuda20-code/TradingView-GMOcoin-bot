from __future__ import annotations

import json
from typing import Any, Dict

from aiohttp import ClientSession
from loguru import logger


class DiscordNotifier:
    def __init__(self, session: ClientSession, webhook_url: str | None) -> None:
        self._session = session
        self._webhook_url = webhook_url

    def enabled(self) -> bool:
        return bool(self._webhook_url)

    async def _post(self, content: str) -> None:
        if not self.enabled():
            logger.debug("discord notifier disabled")
            return
        payload: Dict[str, Any] = {"content": content[:1900]}
        try:
            async with self._session.post(self._webhook_url, json=payload, timeout=10) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    logger.error(
                        "Discord webhook failed",
                        extra={"status": resp.status, "body": body},
                    )
        except Exception as exc:  # pragma: no cover - network failure logging
            logger.exception("Failed to post Discord notification", exc_info=exc)

    async def notify_entry_ok(self, *, event_id: str, side: str, size: float, price: float | None, latency_ms: float) -> None:
        price_part = f" @ {price:.0f} JPY" if price else ""
        content = (
            f"ENTRY OK | event={event_id} | side={side} | size={size:.2f} BTC{price_part}"
            f" | latency={latency_ms:.0f} ms"
        )
        await self._post(content)

    async def notify_close_ok(
        self,
        *,
        event_id: str,
        closed_side: str,
        closed_qty: float,
        pnl: float | None,
        latency_ms: float,
    ) -> None:
        pnl_part = f" | pnl={pnl:.0f} JPY" if pnl is not None else ""
        content = (
            f"CLOSE OK | event={event_id} | closed_side={closed_side} | qty={closed_qty:.2f} BTC"
            f"{pnl_part} | latency={latency_ms:.0f} ms"
        )
        await self._post(content)

    async def notify_error(self, *, event_id: str, message: str) -> None:
        content = f"ERROR | event={event_id} | {message}"
        await self._post(content)
