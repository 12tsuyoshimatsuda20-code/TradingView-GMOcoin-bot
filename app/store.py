"""SQLite based idempotency store."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Optional

from .models import EventRecord, SignalType


class EventStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        await asyncio.to_thread(self._ensure_schema)

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size TEXT,
                    ts INTEGER NOT NULL,
                    received_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    async def record_event(
        self,
        *,
        event_id: str,
        event_type: SignalType,
        symbol: str,
        side: str,
        size: Optional[str],
        ts: int,
        received_at: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._record_event_sync,
            event_id,
            event_type,
            symbol,
            side,
            size,
            ts,
            received_at,
        )

    def _record_event_sync(
        self,
        event_id: str,
        event_type: SignalType,
        symbol: str,
        side: str,
        size: Optional[str],
        ts: int,
        received_at: int,
    ) -> bool:
        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO events (id, type, symbol, side, size, ts, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, event_type, symbol, side, size, ts, received_at),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    async def get_last_event(self) -> Optional[EventRecord]:
        return await asyncio.to_thread(self._get_last_event_sync)

    def _get_last_event_sync(self) -> Optional[EventRecord]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, type, symbol, side, size, ts, received_at FROM events ORDER BY received_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return EventRecord(
                id=row["id"],
                type=row["type"],
                symbol=row["symbol"],
                side=row["side"],
                size=row["size"],
                ts=row["ts"],
                received_at=row["received_at"],
            )
        finally:
            conn.close()

    async def update_size(self, event_id: str, size: Optional[str]) -> None:
        await asyncio.to_thread(self._update_size_sync, event_id, size)

    def _update_size_sync(self, event_id: str, size: Optional[str]) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE events SET size = ? WHERE id = ?",
                (size, event_id),
            )
            conn.commit()
        finally:
            conn.close()
