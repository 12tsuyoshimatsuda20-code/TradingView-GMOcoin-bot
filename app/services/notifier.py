from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import aiohttp
from loguru import logger


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str]) -> None:
        self._webhook_url = webhook_url
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._webhook_url is None:
            return
        async with self._lock:
            if self._session is None:
                timeout = aiohttp.ClientTimeout(total=10)
                self._session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None

    async def notify(self, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if self._webhook_url is None:
            logger.debug("Discord webhook URL not configured; skipping notification")
            return
        async with self._lock:
            session = self._session
        if session is None:
            logger.warning("Discord session not ready; skipping notification")
            return
        payload: Dict[str, Any] = {"content": message}
        if extra:
            payload.update(extra)
        try:
            async with session.post(self._webhook_url, json=payload) as response:
                if response.status >= 400:
                    logger.warning(
                        "Discord notification failed: status=%s body=%s",
                        response.status,
                        await response.text(),
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Discord notification exception: %s", exc)
