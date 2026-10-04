"""演示数据：两个队列（策略），“标准支持”有两个版本（不同阈值与日历）、
六个处于不同状态的工单，其中一个是跨队列转派演示。

日历：2026-09-28 ~ 2026-10-30 的工作日；假日切口 2026-10-01/02。
所有演示工单固定 v1（先创建工单、后发布 v2，模拟真实时序）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .db import tx
from .handoff import transfer
from .services import adjudicate_ticket, create_policy_version, scan_all, set_status
from .timeutil import now_utc, parse

CAL_START = datetime(2026, 9, 28)  # 周一
CAL_END = datetime(2026, 10, 31)


def weekdays(
    start: datetime, end: datetime, open_h: int, close_h: int
) -> list[tuple[str, str]]:
    out = []
    d = start
    while d < end:
        if d.weekday() < 5:
            out.append(
                (
                    d.replace(hour=open_h, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    d.replace(hour=close_h, minute=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )
        d += timedelta(days=1)
    return out


def _create_ticket_at(
    conn: sqlite3.Connection, title: str, policy_version_id: int, created: str
) -> int:
    """种子专用：固定到指定策略版本创建工单。"""
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO ticket (title, status, policy_version_id, revision, created_at) VALUES (?, 'open', ?, 1, ?)",
            (title, policy_version_id, created),
        )
        tid = cur.lastrowid
        conn.execute(
            "INSERT INTO ticket_segment (ticket_id, started_at) VALUES (?, ?)",
            (tid, created),
        )
    return tid


def seed_if_empty(conn: sqlite3.Connection) -> bool:
    if conn.execute("SELECT COUNT(*) AS c FROM ticket").fetchone()["c"]:
        return False

    with tx(conn):
        conn.execute("INSERT INTO policy (name) VALUES ('标准支持')")
        policy_id = conn.execute(
            "SELECT id FROM policy WHERE name='标准支持'"
        ).fetchone()["id"]

    v1 = create_policy_version(
        conn,
        policy_id,
        warn_minutes=240,
        escalate_minutes=480,
        work_intervals=weekdays(CAL_START, CAL_END, 9, 17),
        holiday_intervals=[("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")],
        now=now_utc(),
    )

    # 演示工单（固定 v1）
    t1 = _create_ticket_at(
        conn, "无法登录控制台", v1, "2026-09-30T12:00:00Z"
    )  # 累计 300 → 警告
    _create_ticket_at(
        conn, "账单数据异常", v1, "2026-09-28T15:00:00Z"
    )  # 累计 1080 → 警告+升级
    t3 = _create_ticket_at(
        conn, "希望支持 CSV 导出", v1, "2026-09-30T10:00:00Z"
    )  # 等待客户，暂停跨假日
    set_status(conn, t3, "wait", 1, parse("2026-09-30T12:00:00Z"))
    t4 = _create_ticket_at(conn, "密码重置请求", v1, "2026-09-29T09:00:00Z")  # 已解决
    set_status(conn, t4, "resolve", 1, parse("2026-09-29T13:00:00Z"))
    t5 = _create_ticket_at(
        conn, "API 间歇返回 500", v1, "2026-09-29T10:00:00Z"
    )  # 暂停跨夜后恢复 → 升级
    set_status(conn, t5, "wait", 1, parse("2026-09-29T16:00:00Z"))
    set_status(conn, t5, "resume", 2, parse("2026-09-30T09:00:00Z"))

    # v2：阈值更紧、工作时段不同、假日只切 10-01 —— 用于演示迁移差异
    create_policy_version(
        conn,
        policy_id,
        warn_minutes=180,
        escalate_minutes=360,
        work_intervals=weekdays(CAL_START, CAL_END, 8, 16),
        holiday_intervals=[("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")],
        now=now_utc(),
    )

    # 第二个队列：VIP 支持（另一套工作日历与时限）
    with tx(conn):
        conn.execute("INSERT INTO policy (name) VALUES ('VIP 支持')")
        vip_id = conn.execute(
            "SELECT id FROM policy WHERE name='VIP 支持'"
        ).fetchone()["id"]
    vip_v1 = create_policy_version(
        conn,
        vip_id,
        warn_minutes=120,
        escalate_minutes=300,
        work_intervals=weekdays(CAL_START, CAL_END, 8, 16),
        holiday_intervals=[("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")],
        now=now_utc(),
    )

    # 转派演示：标准支持 v1 下累计 660 分钟、警告+升级已送达；
    # 09-30 12:00 转派 VIP 队列 —— 历史分钟保留，已送达裁决不重复
    t6 = _create_ticket_at(conn, "转派演示：报表偶发超时", v1, "2026-09-29T09:00:00Z")
    adjudicate_ticket(conn, t6, parse("2026-09-30T12:00:00Z"))
    transfer(conn, t6, vip_v1, 1, parse("2026-09-30T12:00:00Z"))

    scan_all(conn, now_utc())
    return True
