"""SQLite 持久化。

所有多语句写操作使用 BEGIN IMMEDIATE，并由进程内锁串行化，保证村级人员并发接单时
数据库层面只有一个事务能把计划从 pending 改成 assigned。升级截止时间、责任链等
全部落库，进程重启后状态不依赖内存。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    deactivated_at TEXT
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    town TEXT,
    village TEXT,
    agency_id TEXT REFERENCES agencies(id),
    daily_capacity INTEGER NOT NULL DEFAULT 6,
    active INTEGER NOT NULL DEFAULT 1,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    deactivated_at TEXT
);
CREATE TABLE IF NOT EXISTS elders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    town TEXT NOT NULL,
    village TEXT NOT NULL,
    risk_band TEXT NOT NULL DEFAULT 'routine',
    residence_status TEXT NOT NULL DEFAULT 'home',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    elder_id TEXT NOT NULL REFERENCES elders(id),
    scope TEXT NOT NULL,
    granted INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (elder_id, scope)
);
-- 授权变更只允许追加，任何撤回都不删除既有行。
CREATE TABLE IF NOT EXISTS consent_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    elder_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    granted INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS holidays (
    day TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    make_up_day TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    elder_id TEXT NOT NULL REFERENCES elders(id),
    due_date TEXT NOT NULL,
    interval_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    assignee_id TEXT,
    agency_id TEXT,
    created_at TEXT NOT NULL,
    assigned_at TEXT,
    completed_at TEXT,
    missed_at TEXT,
    generation_event_id INTEGER
);
-- 每位老人同一天至多存在一条未结束计划，重分配走取消/改派，不重复上门。
CREATE UNIQUE INDEX IF NOT EXISTS ux_plans_live
    ON plans(elder_id, due_date) WHERE status IN ('pending', 'assigned');
CREATE INDEX IF NOT EXISTS ix_plans_status ON plans(status);
CREATE TABLE IF NOT EXISTS plan_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    elder_id TEXT,
    plan_id TEXT,
    reason TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS visit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    plan_id TEXT,
    elder_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    agency_id TEXT,
    outcome TEXT NOT NULL,
    note TEXT,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    merged_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risks (
    id TEXT PRIMARY KEY,
    elder_id TEXT NOT NULL,
    band TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    source_plan_id TEXT,
    source_visit_id INTEGER,
    closed_at TEXT,
    closed_by TEXT
);
CREATE TABLE IF NOT EXISTS escalations (
    id TEXT PRIMARY KEY,
    risk_id TEXT NOT NULL REFERENCES risks(id),
    level TEXT NOT NULL,
    state TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    owner_id TEXT,
    timed_out_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_esc_state ON escalations(state);
-- 责任链只追加：接手、转派、联系家属、关闭、超时升级均为不可变事件。
CREATE TABLE IF NOT EXISTS chain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_id TEXT NOT NULL,
    escalation_id TEXT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_owner_id TEXT,
    to_owner_id TEXT,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chain_risk ON chain_events(risk_id, id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    @property
    def path(self) -> str:
        return self._path

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def lock(self) -> threading.RLock:
        return self._lock

    def begin(self) -> "Transaction":
        return Transaction(self._conn, self._lock)

    def read(self) -> "ReadTransaction":
        return ReadTransaction(self._conn, self._lock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class ReadTransaction:
    """只读临界区：加进程锁但不开写事务，避免查询互相阻塞。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


class Transaction:
    """BEGIN IMMEDIATE 事务：第一个进入者立刻拿写锁，并发接单其余事务只能排队后看到已接单。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        self._active = False

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        self._active = True
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._active:
                if exc_type is None:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute("ROLLBACK")
        finally:
            self._active = False
            self._lock.release()

    def commit_keep_error(self) -> None:
        """提前提交后仍向外抛错：审计先落库，随后的异常不再触发回滚。

        用于“记录被拒绝但必须留存”的场景（授权撤回、人员离线期间失效）。
        """
        if not self._active:
            raise RuntimeError("事务不在进行中")
        self._conn.execute("COMMIT")
        self._active = False
