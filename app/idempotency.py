from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass
class EventRecord:
    event_id: str
    mode: str
    symbol: str
    created_at: datetime


class IdempotencyCache:
    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._events: Dict[str, EventRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, event_id: str, mode: str, symbol: str) -> bool:
        async with self._lock:
            self._purge()
            if event_id in self._events:
                return False
            self._events[event_id] = EventRecord(
                event_id=event_id,
                mode=mode,
                symbol=symbol,
                created_at=datetime.now(timezone.utc),
            )
            return True

    async def exists(self, event_id: str) -> bool:
        async with self._lock:
            self._purge()
            return event_id in self._events

    async def get(self, event_id: str) -> Optional[EventRecord]:
        async with self._lock:
            self._purge()
            return self._events.get(event_id)

    async def snapshot(self) -> Dict[str, EventRecord]:
        async with self._lock:
            self._purge()
            return dict(self._events)

    def _purge(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [key for key, record in self._events.items() if now - record.created_at > self._ttl]
        for key in expired:
            self._events.pop(key, None)
