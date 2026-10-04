"""策略迁移：发布新版本不影响旧工单；显式迁移展示旧/新差异并保存证据。"""

import pytest

from app.services import (
    Conflict,
    create_policy_version,
    create_ticket,
    get_ticket,
    migrate_policy,
    migration_preview,
    scan_all,
    ticket_detail,
)
from app.timeutil import parse

NOW = parse("2026-09-28T10:00:00Z")  # 周一 10:00


def _v2(conn, make_policy):
    """同一策略的第二版本：阈值不同、工作时段 10:00-18:00、无假日切口。"""
    pid = conn.execute("SELECT policy_id FROM policy_version").fetchone()["policy_id"]
    from conftest import weekdays

    return create_policy_version(
        conn,
        pid,
        warn_minutes=90,
        escalate_minutes=200,
        work_intervals=weekdays(open_h=10, close_h=18),
        holiday_intervals=[],
        now=parse("2026-09-27T00:00:00Z"),
    )


def test_new_version_does_not_change_existing_tickets(conn, make_policy):
    make_policy(
        warn=120, esc=240, holidays=[("2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z")]
    )
    tid = create_ticket(conn, "老工单", None, parse("2026-09-28T09:00:00Z"))
    before = ticket_detail(conn, tid, NOW)

    _v2(conn, make_policy)  # 发布新版本

    after = ticket_detail(conn, tid, NOW)
    assert before["policy"]["policy_version_id"] == after["policy"]["policy_version_id"]
    assert before["timing"]["escalate_deadline"] == after["timing"]["escalate_deadline"]
    assert (
        before["timing"]["accumulated_minutes"]
        == after["timing"]["accumulated_minutes"]
    )


def test_migration_preview_shows_diff_and_migrate_saves_evidence(conn, make_policy):
    v1 = make_policy(
        warn=120, esc=240, holidays=[("2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z")]
    )
    tid = create_ticket(conn, "待迁移", None, parse("2026-09-28T09:00:00Z"))
    v2 = _v2(conn, make_policy)

    preview = migration_preview(conn, tid, v2, NOW)
    assert preview["from"]["policy_version_id"] == v1
    assert preview["to"]["policy_version_id"] == v2
    # v1 下 09:00-10:00 计 60 分钟；v2 工作日 10:00 才开始 → 累计 0
    assert preview["from"]["accumulated_minutes"] == 60
    assert preview["to"]["accumulated_minutes"] == 0
    assert preview["delta"]["accumulated_minutes"] == -60
    # v1 升级截止：还需 180 分钟 → 周一 13:00；v2：还需 200 分钟 → 周一 13:20
    assert preview["from"]["escalate_deadline"] == "2026-09-28T13:00:00Z"
    assert preview["to"]["escalate_deadline"] == "2026-09-28T13:20:00Z"
    assert preview["delta"]["escalate_deadline_shift_seconds"] == 1200

    # 预览不落库
    assert (
        conn.execute("SELECT COUNT(*) AS c FROM policy_migration").fetchone()["c"] == 0
    )

    detail = migrate_policy(conn, tid, v2, expected_revision=1, actor="admin", now=NOW)
    assert detail["policy"]["policy_version_id"] == v2
    assert detail["revision"] == 2
    assert len(detail["migrations"]) == 1
    evidence = detail["migrations"][0]
    assert evidence["actor"] == "admin"
    assert evidence["diff"]["from"]["escalate_deadline"] == "2026-09-28T13:00:00Z"
    assert evidence["diff"]["to"]["escalate_deadline"] == "2026-09-28T13:20:00Z"
    # 证据已持久化
    assert (
        conn.execute(
            "SELECT COUNT(*) AS c FROM policy_migration WHERE ticket_id = ?", (tid,)
        ).fetchone()["c"]
        == 1
    )


def test_migration_requires_current_revision(conn, make_policy):
    make_policy(warn=120, esc=240)
    tid = create_ticket(conn, "冲突迁移", None, parse("2026-09-28T09:00:00Z"))
    v2 = _v2(conn, make_policy)
    with pytest.raises(Conflict):
        migrate_policy(conn, tid, v2, expected_revision=99, actor=None, now=NOW)
    # 未迁移
    assert get_ticket(conn, tid)["policy_version_id"] != v2


def test_readjudication_after_migration_uses_new_version(conn, make_policy):
    """迁移后按新版本重新裁决（旧版本下的裁决记录保留）。"""
    v1 = make_policy(warn=120, esc=240)
    tid = create_ticket(conn, "迁移后再裁决", None, parse("2026-09-28T09:00:00Z"))
    v2 = _v2(conn, make_policy)  # warn 90 / esc 200，工作日 10:00 起

    later = parse("2026-09-28T15:00:00Z")  # v1 下累计 360 → 警告+升级
    scan_all(conn, later)
    kinds_v1 = {
        r["kind"]
        for r in conn.execute("SELECT kind FROM adjudication WHERE ticket_id=?", (tid,))
    }
    assert kinds_v1 == {"warning", "escalation"}

    migrate_policy(conn, tid, v2, expected_revision=1, actor=None, now=later)
    scan_all(conn, later)  # v2 下累计 300 ≥ 200 → 按 v2 再登记一轮
    rows = conn.execute(
        "SELECT kind, policy_version_id FROM adjudication WHERE ticket_id=?", (tid,)
    ).fetchall()
    assert {(r["kind"], r["policy_version_id"]) for r in rows} == {
        ("warning", v1),
        ("escalation", v1),
        ("warning", v2),
        ("escalation", v2),
    }
