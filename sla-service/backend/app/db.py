"""SQLite 访问层：schema、连接、事务助手。

并发模型：所有写路径（用户状态操作与后台扫描裁决）都走
`tx()` 的 BEGIN IMMEDIATE 事务，SQLite 会将并发写者串行化，
因此“到期裁决”与“解决工单”等竞争操作按事务提交顺序裁决。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS policy_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_id INTEGER NOT NULL REFERENCES policy(id),
    version INTEGER NOT NULL,
    warn_minutes INTEGER NOT NULL,
    escalate_minutes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (policy_id, version)
);

-- 工作日历由 UTC 区间给出：kind='work' 为工作区间，kind='holiday' 为假日切口
CREATE TABLE IF NOT EXISTS calendar_interval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_version_id INTEGER NOT NULL REFERENCES policy_version(id),
    kind TEXT NOT NULL CHECK (kind IN ('work', 'holiday')),
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calendar_pv ON calendar_interval(policy_version_id);

CREATE TABLE IF NOT EXISTS ticket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'waiting_customer', 'resolved')),
    policy_version_id INTEGER NOT NULL REFERENCES policy_version(id),  -- 创建时固定的策略版本
    revision INTEGER NOT NULL DEFAULT 1,                               -- 乐观锁
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- 计时运行段：ended_at 为 NULL 表示当前正在计时；段与段之间的空隙即暂停区间
CREATE TABLE IF NOT EXISTS ticket_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES ticket(id),
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_segment_ticket ON ticket_segment(ticket_id);

-- 时限裁决（警告/升级）登记，唯一键保证重启与重复扫描后只登记一次
CREATE TABLE IF NOT EXISTS adjudication (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES ticket(id),
    kind TEXT NOT NULL CHECK (kind IN ('warning', 'escalation')),
    policy_version_id INTEGER NOT NULL REFERENCES policy_version(id),
    adjudicated_at TEXT NOT NULL,
    accumulated_minutes REAL NOT NULL,
    threshold_minutes INTEGER NOT NULL,
    basis_json TEXT NOT NULL,           -- 裁决依据：累计分钟、计入/暂停区间等
    UNIQUE (ticket_id, kind, policy_version_id)
);

-- 策略迁移证据：保存旧/新计时差异
CREATE TABLE IF NOT EXISTS policy_migration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES ticket(id),
    from_policy_version_id INTEGER NOT NULL,
    to_policy_version_id INTEGER NOT NULL,
    migrated_at TEXT NOT NULL,
    actor TEXT,
    diff_json TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # 手动控制事务
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    from .handoff import SCHEMA as HANDOFF_SCHEMA

    conn.executescript(HANDOFF_SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：进入即取写锁，保证读-改-写串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
