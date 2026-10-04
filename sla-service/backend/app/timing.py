"""工单计时引擎：有效工作分钟、截止时刻、计入/暂停区间。

模型：
- 工单生命周期由若干“运行段”(ticket_segment) 组成；等待客户时段是两段之间的空隙。
- 有效工作分钟 = 运行段 ∩ (工作区间 − 假日切口) 的总时长；转派边界把运行段
  切开，每段按当时所属队列的日历计入（见 handoff.calendar_parts）。
- 截止时刻 = 从 now 起再累积剩余有效分钟的日历时刻（advance），
  剩余分钟始终按当前固定版本的日历与阈值计算。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .intervals import Interval, advance, clip, subtract
from .timeutil import iso, parse


@dataclass
class PolicyVersionView:
    id: int
    policy_id: int
    policy_name: str
    version: int
    warn_minutes: int
    escalate_minutes: int
    work: list[Interval]
    holidays: list[Interval]

    def effective(self) -> list[Interval]:
        """工作区间减去假日切口后的有效日历。"""
        return subtract(self.work, self.holidays)


def load_policy_version(
    conn: sqlite3.Connection, policy_version_id: int
) -> PolicyVersionView:
    row = conn.execute(
        """
        SELECT pv.id, pv.policy_id, p.name AS policy_name, pv.version,
               pv.warn_minutes, pv.escalate_minutes
        FROM policy_version pv JOIN policy p ON p.id = pv.policy_id
        WHERE pv.id = ?
        """,
        (policy_version_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"policy_version {policy_version_id} 不存在")
    iv_rows = conn.execute(
        "SELECT kind, start_utc, end_utc FROM calendar_interval WHERE policy_version_id = ? ORDER BY start_utc",
        (policy_version_id,),
    ).fetchall()
    work = [
        (parse(r["start_utc"]), parse(r["end_utc"]))
        for r in iv_rows
        if r["kind"] == "work"
    ]
    holidays = [
        (parse(r["start_utc"]), parse(r["end_utc"]))
        for r in iv_rows
        if r["kind"] == "holiday"
    ]
    return PolicyVersionView(
        id=row["id"],
        policy_id=row["policy_id"],
        policy_name=row["policy_name"],
        version=row["version"],
        warn_minutes=row["warn_minutes"],
        escalate_minutes=row["escalate_minutes"],
        work=work,
        holidays=holidays,
    )


def load_segments(
    conn: sqlite3.Connection, ticket_id: int
) -> list[tuple[datetime, datetime | None]]:
    rows = conn.execute(
        "SELECT started_at, ended_at FROM ticket_segment WHERE ticket_id = ? ORDER BY started_at, id",
        (ticket_id,),
    ).fetchall()
    return [
        (parse(r["started_at"]), parse(r["ended_at"]) if r["ended_at"] else None)
        for r in rows
    ]


def paused_intervals(
    segments: list[tuple[datetime, datetime | None]], status: str
) -> list[tuple[datetime, datetime | None]]:
    """运行段之间的空隙即暂停区间；当前处于等待客户时末尾为开口区间。"""
    out: list[tuple[datetime, datetime | None]] = []
    for prev, nxt in zip(segments, segments[1:]):
        if prev[1] is not None:
            out.append((prev[1], nxt[0]))
    if status == "waiting_customer" and segments and segments[-1][1] is not None:
        out.append((segments[-1][1], None))
    return out


def raw_timing(
    conn: sqlite3.Connection, ticket: sqlite3.Row, pv: PolicyVersionView, now: datetime
) -> dict:
    """核心计算，返回 datetime 原值，便于迁移差异等二次计算。"""
    from .handoff import calendar_parts

    eff = pv.effective()
    segments = load_segments(conn, ticket["id"])
    counted: list[Interval] = []
    acc_seconds = 0.0
    for s, e in segments:
        seg_end = min(e, now) if e is not None else now
        if seg_end <= s:
            continue
        # 转派边界把运行段切开：每段按当时所属队列的日历计入
        for calendar, lo, hi in calendar_parts(conn, ticket, pv, s, seg_end):
            for cs, ce in clip(calendar.effective(), lo, hi):
                counted.append((cs, ce))
                acc_seconds += (ce - cs).total_seconds()

    status = ticket["status"]
    running = status == "open"
    warn_rem = pv.warn_minutes * 60 - acc_seconds
    esc_rem = pv.escalate_minutes * 60 - acc_seconds

    def deadline(remaining: float) -> datetime | None:
        if remaining <= 0:
            return now  # 已超过阈值：截止时刻就是现在（已到期）
        return advance(eff, now, remaining)

    warn_deadline = esc_deadline = proj_warn = proj_esc = None
    if running:
        warn_deadline, esc_deadline = deadline(warn_rem), deadline(esc_rem)
    elif status == "waiting_customer":
        # 暂停期间时钟冻结，给出“若现在恢复”的预计截止时刻
        proj_warn, proj_esc = deadline(warn_rem), deadline(esc_rem)

    return {
        "as_of": now,
        "running": running,
        "accumulated_seconds": acc_seconds,
        "counted": counted,
        "paused": paused_intervals(segments, status),
        "warn_deadline": warn_deadline,
        "escalate_deadline": esc_deadline,
        "projected_warn_deadline": proj_warn,
        "projected_escalate_deadline": proj_esc,
    }


def public_timing(raw: dict) -> dict:
    """转为 API JSON 形态。"""

    def dt(x):
        return iso(x) if x is not None else None

    return {
        "as_of": iso(raw["as_of"]),
        "running": raw["running"],
        "accumulated_minutes": round(raw["accumulated_seconds"] / 60, 2),
        "warn_deadline": dt(raw["warn_deadline"]),
        "escalate_deadline": dt(raw["escalate_deadline"]),
        "projected_warn_deadline": dt(raw["projected_warn_deadline"]),
        "projected_escalate_deadline": dt(raw["projected_escalate_deadline"]),
        "counted_intervals": [[iso(s), iso(e)] for s, e in raw["counted"]],
        "paused_intervals": [[iso(s), iso(e) if e else None] for s, e in raw["paused"]],
    }
