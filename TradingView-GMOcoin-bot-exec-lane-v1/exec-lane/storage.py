from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

DB_PATH = Path(__file__).resolve().parent / "runtime.db"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                event_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def prune_old_events(ttl_sec: int = 600) -> None:
    threshold = int(time.time()) - ttl_sec
    with _connect() as conn:
        conn.execute("DELETE FROM idempotency WHERE created_at < ?", (threshold,))
        conn.commit()


def put_event(event_id: str) -> bool:
    now = int(time.time())
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO idempotency (event_id, created_at) VALUES (?, ?)",
                (event_id, now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def exists_recent(event_id: str, ttl_sec: int = 600) -> bool:
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM idempotency WHERE event_id = ?", (event_id,)
        ).fetchone()
    if row is None:
        return False
    created_at = int(row[0])
    if now - created_at <= ttl_sec:
        return True
    delete_event(event_id)
    return False


def delete_event(event_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM idempotency WHERE event_id = ?", (event_id,))
        conn.commit()


def set_state(key: str, value: Any) -> None:
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value), now),
        )
        conn.commit()


def get_state(key: str, default: Optional[Any] = None) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row[0]


def export_state() -> Dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM state").fetchall()
    return {row[0]: row[1] for row in rows}


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()
