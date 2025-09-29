from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import aiohttp
from loguru import logger


class DiscordNotifier:
    SUCCESS_COLOR = 0x10B981
    INFO_COLOR = 0x6B7280
    ERROR_COLOR = 0xDC2626

    def __init__(
        self,
        webhook_url: Optional[str],
        *,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: int = 10,
    ) -> None:
        self.webhook_url = webhook_url
        self._session = session
        self._timeout = timeout
        self._session_owner = session is None
        self._lock = asyncio.Lock()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            async with self._lock:
                if self._session is None:
                    timeout = aiohttp.ClientTimeout(total=self._timeout)
                    self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and self._session_owner:
            await self._session.close()

    async def send_embed(
        self,
        *,
        title: str,
        description: str,
        color: int,
        fields: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> None:
        if not self.webhook_url:
            logger.warning("Discord webhook URL is not configured; skipping notification")
            return

        payload: Dict[str, Any] = {
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
        if fields:
            payload["embeds"][0]["fields"] = list(fields)

        try:
            session = await self._ensure_session()
            async with session.post(self.webhook_url, json=payload) as response:
                if response.status >= 400:
                    body = await response.text()
                    logger.error(
                        "Failed to deliver Discord notification: status=%s body=%s",
                        response.status,
                        body,
                    )
        except Exception:
            logger.exception("Unexpected error while sending Discord notification")

    async def notify_entry_success(self, event_id: str, side: str, size: float) -> None:
        await self.send_embed(
            title="ENTRY Executed",
            description=f"event_id={event_id}",
            color=self.SUCCESS_COLOR,
            fields=[
                {"name": "Side", "value": side, "inline": True},
                {"name": "Size", "value": f"{size:.5f}", "inline": True},
            ],
        )

    async def notify_close_success(self, event_id: str, closed_side: str, size: float) -> None:
        await self.send_embed(
            title="CLOSE Executed",
            description=f"event_id={event_id}",
            color=self.SUCCESS_COLOR,
            fields=[
                {"name": "Closed Side", "value": closed_side, "inline": True},
                {"name": "Size", "value": f"{size:.5f}", "inline": True},
            ],
        )

    async def notify_ignored(self, event_id: str, reason: str) -> None:
        await self.send_embed(
            title="ENTRY Ignored",
            description=f"event_id={event_id}\n{reason}",
            color=self.INFO_COLOR,
        )

    async def notify_no_position(self, event_id: str) -> None:
        await self.send_embed(
            title="CLOSE Skipped",
            description=f"event_id={event_id}\nNo open position",
            color=self.INFO_COLOR,
        )

    async def notify_error(self, event_id: str, message: str) -> None:
        await self.send_embed(
            title="Execution Error",
            description=f"event_id={event_id}\n{message}",
            color=self.ERROR_COLOR,
        )
