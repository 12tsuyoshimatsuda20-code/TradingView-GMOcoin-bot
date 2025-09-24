from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from loguru import logger

try:
    from pybotters.helpers.gmocoin import GMOCoinHelper
except Exception:  # pragma: no cover - optional dependency for runtime only
    GMOCoinHelper = None  # type: ignore


class GMOWebSocketSupervisor:
    """Minimal supervisor to track WS connectivity (v1 placeholder)."""

    def __init__(self, client, symbol: str) -> None:
        self._client = client
        self._symbol = symbol
        self._connected = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if GMOCoinHelper is None:
            logger.warning("gmocoin_helper_unavailable", reason="pybotters extras not installed")
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="gmocoin-ws")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            self._connected.clear()

    async def _run(self) -> None:
        helper = GMOCoinHelper(self._client._client)  # type: ignore[attr-defined]
        try:
            async with helper.trade_ws(symbols=[self._symbol]) as ws:
                self._connected.set()
                async for _ in ws:
                    pass
        except Exception as exc:
            logger.error("gmocoin_ws_error", error=str(exc))
        finally:
            self._connected.clear()

