from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite
from loguru import logger

from utils import ensure_aware, utcnow


@dataclass
class StatusSnapshot:
    position_side: Optional[str] = None
    position_qty: float = 0.0
    ws_connected: bool = False
    last_event_id: Optional[str] = None
    last_event_ts: Optional[str] = None
    retry_stats: Dict[str, int] = field(default_factory=lambda: {"entry": 0, "close": 0, "rest": 0})


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("opening sqlite database", path=str(self.db_path))
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

    async def init(self) -> None:
        if not self._conn:
            raise RuntimeError("storage not connected")
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                error_detail TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                position_side TEXT,
                position_qty REAL DEFAULT 0,
                ws_connected INTEGER DEFAULT 0,
                last_event_id TEXT,
                last_event_ts TEXT,
                retry_stats TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            INSERT INTO status(id, updated_at)
            SELECT 1, datetime('now')
            WHERE NOT EXISTS (SELECT 1 FROM status WHERE id = 1);
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def begin_event(self, event_id: str, ts: datetime, mode: str, ttl_seconds: int) -> bool:
        if not self._conn:
            raise RuntimeError("storage not connected")
        cutoff = utcnow() - timedelta(seconds=ttl_seconds)
        async with self._conn.execute(
            "SELECT event_id, created_at FROM events WHERE event_id = ?",
            (event_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                created_at = ensure_aware(datetime.fromisoformat(row["created_at"]))
                if created_at >= cutoff:
                    return False
        await self._conn.execute(
            "REPLACE INTO events(event_id, ts, mode, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                ensure_aware(ts).isoformat(),
                mode,
                "processing",
                utcnow().isoformat(),
            ),
        )
        await self._conn.commit()
        return True

    async def finalize_event(
        self,
        event_id: str,
        *,
        status: str,
        error_code: Optional[str] = None,
        error_detail: Optional[str] = None,
    ) -> None:
        if not self._conn:
            raise RuntimeError("storage not connected")
        await self._conn.execute(
            "UPDATE events SET status = ?, error_code = ?, error_detail = ? WHERE event_id = ?",
            (status, error_code, error_detail, event_id),
        )
        await self._conn.commit()

    async def update_status(self, **fields: Any) -> None:
        if not self._conn:
            raise RuntimeError("storage not connected")
        current = await self.get_status()
        data = {**current.__dict__}
        data.update(fields)
        retry_stats = data.get("retry_stats")
        if isinstance(retry_stats, dict):
            retry_dump = json.dumps(retry_stats)
        else:
            retry_dump = retry_stats if isinstance(retry_stats, str) else json.dumps({})
        await self._conn.execute(
            """
            UPDATE status
            SET position_side = ?,
                position_qty = ?,
                ws_connected = ?,
                last_event_id = ?,
                last_event_ts = ?,
                retry_stats = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                data.get("position_side"),
                data.get("position_qty", 0.0),
                1 if data.get("ws_connected") else 0,
                data.get("last_event_id"),
                data.get("last_event_ts"),
                retry_dump,
                utcnow().isoformat(),
            ),
        )
        await self._conn.commit()

    async def increment_retry(self, category: str) -> None:
        status = await self.get_status()
        retry_stats = status.retry_stats
        retry_stats[category] = retry_stats.get(category, 0) + 1
        await self.update_status(retry_stats=retry_stats)

    async def get_status(self) -> StatusSnapshot:
        if not self._conn:
            raise RuntimeError("storage not connected")
        async with self._conn.execute("SELECT * FROM status WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if not row:
            return StatusSnapshot()
        retry_stats_raw = row["retry_stats"] or "{}"
        try:
            retry_stats = json.loads(retry_stats_raw)
        except json.JSONDecodeError:
            retry_stats = {"entry": 0, "close": 0, "rest": 0}
        return StatusSnapshot(
            position_side=row["position_side"],
            position_qty=row["position_qty"] or 0.0,
            ws_connected=bool(row["ws_connected"]),
            last_event_id=row["last_event_id"],
            last_event_ts=row["last_event_ts"],
            retry_stats=retry_stats,
        )
