from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp
from loguru import logger


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str], timeout: float = 5.0) -> None:
        self._webhook_url = webhook_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self._webhook_url:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            logger.info("Discord notifier initialized")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def send(self, content: str, embeds: Optional[list[Dict[str, Any]]] = None) -> None:
        if not self._webhook_url:
            return
        if self._session is None:
            await self.start()
        assert self._session is not None
        payload: Dict[str, Any] = {"content": content}
        if embeds:
            payload["embeds"] = embeds
        try:
            async with self._session.post(self._webhook_url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "Discord notification failed", status=resp.status, body=body
                    )
        except Exception as exc:
            logger.warning("Discord notification error", error=str(exc))

