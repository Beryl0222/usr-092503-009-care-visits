"""协调器状态的 SQLite 持久化。

所有写入都在协调器持有的同一把锁内完成,因此这里用单个连接、
``check_same_thread=False`` 并开启 WAL,重启进程后状态完整恢复。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS elders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    authorized INTEGER NOT NULL DEFAULT 1,
    suspended_until TEXT,
    scopes TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    site_id TEXT NOT NULL,
    township_id TEXT NOT NULL,
    health_notes TEXT NOT NULL DEFAULT '',
    family_contacts TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    site_id TEXT NOT NULL,
    township_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    on_leave INTEGER NOT NULL DEFAULT 0,
    capacity INTEGER NOT NULL DEFAULT 8
);
CREATE TABLE IF NOT EXISTS agencies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    elder_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    agency_id TEXT,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT,
    outcome TEXT,
    offline_token TEXT UNIQUE,
    occurred_at TEXT,
    merged_seq INTEGER,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    elder_id TEXT NOT NULL,
    task_id TEXT,
    band TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    deadline_at TEXT,
    acked_by TEXT,
    escalated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alert_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    from_handler TEXT,
    to_handler TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    """对 SQLite 的薄封装:负责建表与通用行读写。"""

    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def insert(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})",
            tuple(json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in row.values()),
        )

    def update(self, table: str, row_id: str, changes: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in changes)
        self.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?",
            tuple(changes.values()) + (row_id,),
        )
