from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from datetime import datetime, timezone


class IdempotencyStore:
    def __init__(self, db_path: Path, expiry_seconds: int = 600) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._expiry_seconds = expiry_seconds
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_ts TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    async def register_event(self, event_id: str, event_ts: str) -> bool:
        async with self._lock:
            self._cleanup_locked()
            cur = self._conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            )
            if cur.fetchone():
                return False
            now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
            self._conn.execute(
                "INSERT INTO events(event_id, event_ts, created_at) VALUES(?,?,?)",
                (event_id, event_ts, now_epoch),
            )
            self._conn.commit()
            return True

    async def remove_event(self, event_id: str) -> None:
        async with self._lock:
            self._conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
            self._conn.commit()

    async def is_duplicate(self, event_id: str) -> bool:
        async with self._lock:
            self._cleanup_locked()
            cur = self._conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            )
            return cur.fetchone() is not None

    def _cleanup_locked(self) -> None:
        now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
        threshold = now_epoch - self._expiry_seconds
        self._conn.execute("DELETE FROM events WHERE created_at < ?", (threshold,))
        self._conn.commit()


@dataclass
class StatusSnapshot:
    position_qty: float = 0.0
    position_side: str = "FLAT"
    last_event_id: Optional[str] = None
    last_event_ts: Optional[str] = None
    retry_stats: Dict[str, int] = field(default_factory=dict)
    ws_connected: bool = False


class StatusStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = StatusSnapshot(retry_stats={"rest": 0, "ws": 0})

    async def set_position(self, qty: float, side: str) -> None:
        async with self._lock:
            self._state.position_qty = qty
            self._state.position_side = side

    async def set_last_event(self, event_id: str, event_ts: str) -> None:
        async with self._lock:
            self._state.last_event_id = event_id
            self._state.last_event_ts = event_ts

    async def incr_retry(self, key: str) -> None:
        async with self._lock:
            if key not in self._state.retry_stats:
                self._state.retry_stats[key] = 0
            self._state.retry_stats[key] += 1

    async def set_ws_connected(self, connected: bool) -> None:
        async with self._lock:
            self._state.ws_connected = connected

    async def snapshot(self) -> StatusSnapshot:
        async with self._lock:
            return StatusSnapshot(
                position_qty=self._state.position_qty,
                position_side=self._state.position_side,
                last_event_id=self._state.last_event_id,
                last_event_ts=self._state.last_event_ts,
                retry_stats=dict(self._state.retry_stats),
                ws_connected=self._state.ws_connected,
            )
