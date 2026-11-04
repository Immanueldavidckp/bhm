"""Persistent store-and-forward queue (SQLite).

Every message is written here first and deleted only after the broker
acknowledges it (QoS 1 PUBACK), so readings survive network outages and
gateway reboots. Oldest rows are dropped once `max_rows` is reached.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time


class MessageBuffer:
    def __init__(self, path: str, max_rows: int = 200_000):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS queue ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " topic TEXT NOT NULL, payload BLOB NOT NULL,"
            " qos INTEGER NOT NULL, retain INTEGER NOT NULL, created REAL NOT NULL)")
        self.dropped = 0

    def put(self, topic: str, payload: bytes, qos: int = 1, retain: bool = False) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO queue (topic, payload, qos, retain, created) VALUES (?,?,?,?,?)",
                (topic, payload, qos, int(retain), time.time()))
            n = self._db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            if n > self.max_rows:
                excess = n - self.max_rows
                self._db.execute(
                    "DELETE FROM queue WHERE id IN (SELECT id FROM queue ORDER BY id LIMIT ?)",
                    (excess,))
                self.dropped += excess

    def peek(self, limit: int = 50) -> list[tuple[int, str, bytes, int, bool]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, topic, payload, qos, retain FROM queue ORDER BY id LIMIT ?",
                (limit,)).fetchall()
        return [(r[0], r[1], bytes(r[2]), r[3], bool(r[4])) for r in rows]

    def delete(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("DELETE FROM queue WHERE id = ?", [(i,) for i in ids])

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
