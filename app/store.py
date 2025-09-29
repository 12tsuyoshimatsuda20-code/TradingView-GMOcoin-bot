from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from loguru import logger


@dataclass
class StoredEventRecord:
    event_id: str
    received_at: str
    mode: str
    status: str
    action: Optional[str]
    request: Optional[str]
    response: Optional[str]
    error: Optional[str]


class EventStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                action TEXT,
                request TEXT,
                response TEXT,
                error TEXT
            )
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def register_event(
        self,
        event_id: str,
        mode: str,
        payload: dict,
        *,
        status: str = "received",
    ) -> bool:
        if self._conn is None:
            raise RuntimeError("EventStore not connected")

        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._conn.execute(
                """
                INSERT INTO events(event_id, received_at, mode, status, request)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, now, mode, status, json.dumps(payload, ensure_ascii=False)),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            await self._conn.rollback()
            logger.info("Duplicate event detected: %s", event_id)
            return False

    async def update_event(
        self,
        event_id: str,
        *,
        status: str,
        action: Optional[str] = None,
        response: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("EventStore not connected")

        await self._conn.execute(
            """
            UPDATE events
            SET status = ?, action = ?, response = ?, error = ?
            WHERE event_id = ?
            """,
            (
                status,
                action,
                json.dumps(response, ensure_ascii=False) if response else None,
                error,
                event_id,
            ),
        )
        await self._conn.commit()

    async def fetch_last_event(self) -> Optional[StoredEventRecord]:
        if self._conn is None:
            raise RuntimeError("EventStore not connected")

        async with self._conn.execute(
            """
            SELECT event_id, received_at, mode, status, action, request, response, error
            FROM events
            ORDER BY received_at DESC
            LIMIT 1
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return StoredEventRecord(*row)

    async def get_event(self, event_id: str) -> Optional[StoredEventRecord]:
        if self._conn is None:
            raise RuntimeError("EventStore not connected")
        async with self._conn.execute(
            """
            SELECT event_id, received_at, mode, status, action, request, response, error
            FROM events
            WHERE event_id = ?
            """,
            (event_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return StoredEventRecord(*row)
