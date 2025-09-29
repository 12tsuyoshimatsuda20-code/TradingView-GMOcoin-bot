from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional


@dataclass
class EventRecord:
    event_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    detail: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "detail": self.detail,
        }


class IdempotencyStore:
    def __init__(self, ttl: timedelta) -> None:
        self._ttl = ttl
        self._records: Dict[str, EventRecord] = {}
        self._lock = asyncio.Lock()

    async def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            key
            for key, value in self._records.items()
            if now - value.updated_at >= self._ttl
        ]
        for key in expired:
            self._records.pop(key, None)

    async def check_and_set(self, event_id: str, status: str, detail: str | None = None) -> tuple[bool, EventRecord]:
        async with self._lock:
            await self._purge_expired()
            record = self._records.get(event_id)
            if record:
                return False, record
            now = datetime.now(timezone.utc)
            record = EventRecord(
                event_id=event_id,
                status=status,
                created_at=now,
                updated_at=now,
                detail=detail,
            )
            self._records[event_id] = record
            return True, record

    async def update(self, event_id: str, status: str, detail: str | None = None) -> None:
        async with self._lock:
            record = self._records.get(event_id)
            if not record:
                now = datetime.now(timezone.utc)
                record = EventRecord(
                    event_id=event_id,
                    status=status,
                    created_at=now,
                    updated_at=now,
                    detail=detail,
                )
                self._records[event_id] = record
                return
            record.status = status
            record.updated_at = datetime.now(timezone.utc)
            record.detail = detail

    async def get(self, event_id: str) -> Optional[EventRecord]:
        async with self._lock:
            await self._purge_expired()
            return self._records.get(event_id)

    async def snapshot(self) -> list[dict]:
        async with self._lock:
            await self._purge_expired()
            return [record.as_dict() for record in self._records.values()]
