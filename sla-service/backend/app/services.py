"""业务操作：工单生命周期、时限裁决扫描、策略迁移。

所有写操作都在 BEGIN IMMEDIATE 事务内完成（见 db.tx），
后台扫描与用户状态操作因此按同一事务顺序裁决；
乐观锁 revision 保证过期提交返回冲突。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .db import tx
from .timeutil import iso, now_utc, parse
from .timing import PolicyVersionView, load_policy_version, public_timing, raw_timing


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


class Conflict(Exception):
    """revision 过期冲突。"""

    def __init__(self, current_revision: int):
        super().__init__("revision 已过期")
        self.current_revision = current_revision


# ---------------------------------------------------------------- 查询


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ticket WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise NotFound(f"工单 {ticket_id} 不存在")
    return row


def latest_policy_version_id(conn: sqlite3.Connection, policy_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM policy_version WHERE policy_id = ? ORDER BY version DESC LIMIT 1",
        (policy_id,),
    ).fetchone()
    if row is None:
        raise BadRequest(f"策略 {policy_id} 没有任何版本")
    return row["id"]


def ticket_detail(conn: sqlite3.Connection, ticket_id: int, now: datetime) -> dict:
    t = get_ticket(conn, ticket_id)
    pv = load_policy_version(conn, t["policy_version_id"])
    raw = raw_timing(conn, t, pv, now)
    adjudications = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "policy_version_id": r["policy_version_id"],
            "adjudicated_at": r["adjudicated_at"],
            "accumulated_minutes": r["accumulated_minutes"],
            "threshold_minutes": r["threshold_minutes"],
            "basis": json.loads(r["basis_json"]),
        }
        for r in conn.execute(
            "SELECT * FROM adjudication WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        ).fetchall()
    ]
    migrations = [
        {
            "id": r["id"],
            "from_policy_version_id": r["from_policy_version_id"],
            "to_policy_version_id": r["to_policy_version_id"],
            "migrated_at": r["migrated_at"],
            "actor": r["actor"],
            "diff": json.loads(r["diff_json"]),
        }
        for r in conn.execute(
            "SELECT * FROM policy_migration WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    ]
    return {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "revision": t["revision"],
        "created_at": t["created_at"],
        "resolved_at": t["resolved_at"],
        "policy": {
            "policy_id": pv.policy_id,
            "policy_version_id": pv.id,
            "name": pv.policy_name,
            "version": pv.version,
            "warn_minutes": pv.warn_minutes,
            "escalate_minutes": pv.escalate_minutes,
        },
        "timing": public_timing(raw),
        "adjudications": adjudications,
        "migrations": migrations,
        "handoffs": __import__(__package__ + ".handoff", fromlist=["history"]).history(
            conn, ticket_id
        ),
    }


def list_tickets(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    out = []
    for t in conn.execute("SELECT * FROM ticket ORDER BY id").fetchall():
        pv = load_policy_version(conn, t["policy_version_id"])
        raw = raw_timing(conn, t, pv, now)
        kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM adjudication WHERE ticket_id = ?", (t["id"],)
            ).fetchall()
        }
        out.append(
            {
                "id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "revision": t["revision"],
                "created_at": t["created_at"],
                "policy_version": f"{pv.policy_name} v{pv.version}",
                "accumulated_minutes": round(raw["accumulated_seconds"] / 60, 2),
                "escalate_deadline": public_timing(raw)["escalate_deadline"],
                "warned": "warning" in kinds,
                "escalated": "escalation" in kinds,
            }
        )
    return out


# ---------------------------------------------------------------- 工单生命周期


def create_ticket(
    conn: sqlite3.Connection, title: str, policy_id: int | None, now: datetime
) -> int:
    """创建工单并固定当前最新策略版本。"""
    if not title.strip():
        raise BadRequest("标题不能为空")
    with tx(conn):
        if policy_id is None:
            row = conn.execute("SELECT id FROM policy ORDER BY id LIMIT 1").fetchone()
            if row is None:
                raise BadRequest("尚未定义任何策略")
            policy_id = row["id"]
        pv_id = latest_policy_version_id(conn, policy_id)
        cur = conn.execute(
            "INSERT INTO ticket (title, status, policy_version_id, revision, created_at) VALUES (?, 'open', ?, 1, ?)",
            (title.strip(), pv_id, iso(now)),
        )
        ticket_id = cur.lastrowid
        conn.execute(
            "INSERT INTO ticket_segment (ticket_id, started_at) VALUES (?, ?)",
            (ticket_id, iso(now)),
        )
    return ticket_id


def set_status(
    conn: sqlite3.Connection,
    ticket_id: int,
    action: str,
    expected_revision: int,
    now: datetime,
) -> dict:
    """状态操作：wait / resume / resolve。乐观锁校验在同一事务内完成。"""
    with tx(conn):
        t = conn.execute("SELECT * FROM ticket WHERE id = ?", (ticket_id,)).fetchone()
        if t is None:
            raise NotFound(f"工单 {ticket_id} 不存在")
        if t["revision"] != expected_revision:
            raise Conflict(t["revision"])
        status = t["status"]

        if action == "wait":
            if status != "open":
                raise BadRequest(f"当前状态 {status} 不能转为等待客户")
            conn.execute(
                "UPDATE ticket_segment SET ended_at = ? WHERE ticket_id = ? AND ended_at IS NULL",
                (iso(now), ticket_id),
            )
            new_status, resolved_at = "waiting_customer", None
        elif action == "resume":
            if status != "waiting_customer":
                raise BadRequest(f"当前状态 {status} 不能恢复计时")
            conn.execute(
                "INSERT INTO ticket_segment (ticket_id, started_at) VALUES (?, ?)",
                (ticket_id, iso(now)),
            )
            new_status, resolved_at = "open", None
        elif action == "resolve":
            if status == "resolved":
                raise BadRequest("工单已解决")
            conn.execute(
                "UPDATE ticket_segment SET ended_at = ? WHERE ticket_id = ? AND ended_at IS NULL",
                (iso(now), ticket_id),
            )
            new_status, resolved_at = "resolved", iso(now)
        else:
            raise BadRequest(f"未知操作 {action}")

        cur = conn.execute(
            "UPDATE ticket SET status = ?, resolved_at = COALESCE(?, resolved_at), revision = revision + 1 "
            "WHERE id = ? AND revision = ?",
            (new_status, resolved_at, ticket_id, expected_revision),
        )
        if cur.rowcount == 0:  # 双重保险：并发下以条件更新为准
            raise Conflict(expected_revision + 1)
    return ticket_detail(conn, ticket_id, now)


# ---------------------------------------------------------------- 时限裁决


def _basis(
    raw: dict, pv: PolicyVersionView, kind: str, threshold: int, now: datetime
) -> dict:
    return {
        "rule": f"accumulated_minutes >= {kind}_threshold",
        "as_of": iso(now),
        "accumulated_minutes": round(raw["accumulated_seconds"] / 60, 2),
        "threshold_minutes": threshold,
        "policy": {
            "name": pv.policy_name,
            "version": pv.version,
            "policy_version_id": pv.id,
        },
        "counted_intervals": [[iso(s), iso(e)] for s, e in raw["counted"]],
        "counted_parts": raw["counted_parts"],
        "paused_intervals": [[iso(s), iso(e) if e else None] for s, e in raw["paused"]],
    }


def adjudicate_ticket(
    conn: sqlite3.Connection, ticket_id: int, now: datetime
) -> list[str]:
    """单个工单的扫描裁决。与用户操作走同一事务模型；唯一键保证只登记一次。"""
    fired: list[str] = []
    with tx(conn):
        t = conn.execute("SELECT * FROM ticket WHERE id = ?", (ticket_id,)).fetchone()
        if t is None:
            raise NotFound(f"工单 {ticket_id} 不存在")
        if t["status"] == "resolved":
            return []  # 已解决：不再裁决
        pv = load_policy_version(conn, t["policy_version_id"])
        raw = raw_timing(conn, t, pv, now)
        acc_min = raw["accumulated_seconds"] / 60
        from .handoff import has_handoffs

        # A queue handoff carries delivered outcomes with the ticket; a warning
        # or escalation that existed before the handoff must not fire again.
        already_delivered = (
            {
                r["kind"]
                for r in conn.execute(
                    "SELECT kind FROM adjudication WHERE ticket_id=?", (ticket_id,)
                ).fetchall()
            }
            if has_handoffs(conn, ticket_id)
            else set()
        )
        for kind, threshold in (
            ("warning", pv.warn_minutes),
            ("escalation", pv.escalate_minutes),
        ):
            if acc_min >= threshold and kind not in already_delivered:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO adjudication "
                    "(ticket_id, kind, policy_version_id, adjudicated_at, accumulated_minutes, threshold_minutes, basis_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket_id,
                        kind,
                        pv.id,
                        iso(now),
                        round(acc_min, 2),
                        threshold,
                        json.dumps(_basis(raw, pv, kind, threshold, now)),
                    ),
                )
                if cur.rowcount:
                    fired.append(kind)
    return fired


def scan_all(conn: sqlite3.Connection, now: datetime) -> dict:
    """后台计时器扫描：逐工单在独立事务中裁决，可安全重复执行。"""
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM ticket WHERE status != 'resolved' ORDER BY id"
        ).fetchall()
    ]
    fired: dict[int, list[str]] = {}
    for tid in ids:
        result = adjudicate_ticket(conn, tid, now)
        if result:
            fired[tid] = result
    return {"scanned": len(ids), "fired": fired}


# ---------------------------------------------------------------- 策略与迁移


def create_policy_version(
    conn: sqlite3.Connection,
    policy_id: int,
    warn_minutes: int,
    escalate_minutes: int,
    work_intervals: list[tuple[str, str]],
    holiday_intervals: list[tuple[str, str]],
    now: datetime,
) -> int:
    """新增策略版本。旧工单固定在原版本，时限不受任何影响。"""
    if warn_minutes <= 0 or escalate_minutes <= warn_minutes:
        raise BadRequest("需满足 0 < warn_minutes < escalate_minutes")
    if not work_intervals:
        raise BadRequest("工作区间不能为空")
    with tx(conn):
        if (
            conn.execute("SELECT id FROM policy WHERE id = ?", (policy_id,)).fetchone()
            is None
        ):
            raise NotFound(f"策略 {policy_id} 不存在")
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM policy_version WHERE policy_id = ?",
            (policy_id,),
        ).fetchone()["v"]
        cur = conn.execute(
            "INSERT INTO policy_version (policy_id, version, warn_minutes, escalate_minutes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (policy_id, version, warn_minutes, escalate_minutes, iso(now)),
        )
        pv_id = cur.lastrowid
        for kind, intervals in (
            ("work", work_intervals),
            ("holiday", holiday_intervals),
        ):
            for s, e in intervals:
                if parse(e) <= parse(s):
                    raise BadRequest(f"区间结束必须大于开始: {s} ~ {e}")
                conn.execute(
                    "INSERT INTO calendar_interval (policy_version_id, kind, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                    (pv_id, kind, s, e),
                )
    return pv_id


def _snapshot(
    conn: sqlite3.Connection, ticket: sqlite3.Row, pv: PolicyVersionView, now: datetime
) -> dict:
    raw = raw_timing(conn, ticket, pv, now)
    pub = public_timing(raw)
    return {
        "policy_version_id": pv.id,
        "version": pv.version,
        "warn_minutes": pv.warn_minutes,
        "escalate_minutes": pv.escalate_minutes,
        "accumulated_minutes": pub["accumulated_minutes"],
        "warn_deadline": pub["warn_deadline"] or pub["projected_warn_deadline"],
        "escalate_deadline": pub["escalate_deadline"]
        or pub["projected_escalate_deadline"],
    }


def _shift_seconds(old: str | None, new: str | None) -> float | None:
    if old is None or new is None:
        return None
    return (parse(new) - parse(old)).total_seconds()


def migration_preview(
    conn: sqlite3.Connection, ticket_id: int, to_version_id: int, now: datetime
) -> dict:
    """计算迁移差异（不写库）：旧/新累计分钟、截止时刻及位移。"""
    t = get_ticket(conn, ticket_id)
    from .handoff import has_handoffs

    if has_handoffs(conn, ticket_id):
        raise BadRequest("工单已经发生队列转派，不能再用策略迁移改写逐段计时")
    old_pv = load_policy_version(conn, t["policy_version_id"])
    new_pv = load_policy_version(conn, to_version_id)
    if new_pv.policy_id != old_pv.policy_id:
        raise BadRequest("只能迁移到同一策略的另一版本")
    old = _snapshot(conn, t, old_pv, now)
    new = _snapshot(conn, t, new_pv, now)
    return {
        "ticket_id": ticket_id,
        "as_of": iso(now),
        "from": old,
        "to": new,
        "delta": {
            "warn_minutes": new["warn_minutes"] - old["warn_minutes"],
            "escalate_minutes": new["escalate_minutes"] - old["escalate_minutes"],
            "accumulated_minutes": round(
                new["accumulated_minutes"] - old["accumulated_minutes"], 2
            ),
            "warn_deadline_shift_seconds": _shift_seconds(
                old["warn_deadline"], new["warn_deadline"]
            ),
            "escalate_deadline_shift_seconds": _shift_seconds(
                old["escalate_deadline"], new["escalate_deadline"]
            ),
        },
    }


def migrate_policy(
    conn: sqlite3.Connection,
    ticket_id: int,
    to_version_id: int,
    expected_revision: int,
    actor: str | None,
    now: datetime,
) -> dict:
    """显式迁移：保存旧/新计时差异证据后，在同一事务内切换固定版本。"""
    with tx(conn):
        t = conn.execute("SELECT * FROM ticket WHERE id = ?", (ticket_id,)).fetchone()
        if t is None:
            raise NotFound(f"工单 {ticket_id} 不存在")
        if t["revision"] != expected_revision:
            raise Conflict(t["revision"])
        diff = migration_preview(conn, ticket_id, to_version_id, now)
        diff = {**diff, "migrated_at": iso(now), "actor": actor}
        conn.execute(
            "INSERT INTO policy_migration (ticket_id, from_policy_version_id, to_policy_version_id, migrated_at, actor, diff_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ticket_id,
                diff["from"]["policy_version_id"],
                to_version_id,
                iso(now),
                actor,
                json.dumps(diff),
            ),
        )
        cur = conn.execute(
            "UPDATE ticket SET policy_version_id = ?, revision = revision + 1 WHERE id = ? AND revision = ?",
            (to_version_id, ticket_id, expected_revision),
        )
        if cur.rowcount == 0:
            raise Conflict(expected_revision + 1)
    return ticket_detail(conn, ticket_id, now)
