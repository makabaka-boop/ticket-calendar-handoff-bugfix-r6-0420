"""队列转派：历史按原队列日历计入，后续按新队列规则，裁决不重复通知。"""

import pytest

from app.db import tx
from app.handoff import transfer
from app.services import (
    BadRequest,
    Conflict,
    create_policy_version,
    create_ticket,
    get_ticket,
    migration_preview,
    scan_all,
    set_status,
    ticket_detail,
)
from app.timeutil import parse
from conftest import weekdays


def _queue(conn, name, warn, esc, open_h, close_h):
    with tx(conn):
        cur = conn.execute("INSERT INTO policy (name) VALUES (?)", (name,))
        policy_id = cur.lastrowid
    return create_policy_version(
        conn,
        policy_id,
        warn,
        esc,
        weekdays(open_h=open_h, close_h=close_h),
        [],
        parse("2026-09-27T00:00:00Z"),
    )


def _policy_id(conn, version_id):
    return conn.execute(
        "SELECT policy_id FROM policy_version WHERE id=?", (version_id,)
    ).fetchone()["policy_id"]


def _create(conn, title, version_id, at):
    """``at`` is already a tz-aware datetime in these tests."""
    return create_ticket(conn, title, _policy_id(conn, version_id), at)

def test_handoff_keeps_history_under_old_calendar_and_future_uses_new(conn):
    v1 = _queue(conn, "A 队列", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(conn, "B 队列", warn=60, esc=180, open_h=12, close_h=20)
    tid = _create(conn, "跨队列", v1, parse("2026-10-05T09:00:00Z"))

    # A 队列日历 09:00-10:00 计入 60 分钟，并已经发出一次警告。
    scan_all(conn, parse("2026-10-05T10:00:00Z"))
    detail = transfer(conn, tid, v2, 1, parse("2026-10-05T10:00:00Z"))
    assert detail["revision"] == 2
    assert detail["timing"]["accumulated_minutes"] == 60
    assert detail["timing"]["counted_parts"] == [
        {
            "policy_version_id": v1,
            "start": "2026-10-05T09:00:00Z",
            "end": "2026-10-05T10:00:00Z",
            "accumulated_minutes": 60.0,
        }
    ]

    # 10:00-12:00 不在 B 队列工作日历内，历史 60 分钟不得消失，也不得增加。
    detail = ticket_detail(conn, tid, parse("2026-10-05T11:00:00Z"))
    assert detail["timing"]["accumulated_minutes"] == 60

    # 12:00 以后服从 B 队列日历；A 的历史 60 分钟继续保留。
    detail = ticket_detail(conn, tid, parse("2026-10-05T13:00:00Z"))
    assert detail["timing"]["accumulated_minutes"] == 120
    assert detail["timing"]["counted_parts"] == [
        {
            "policy_version_id": v1,
            "start": "2026-10-05T09:00:00Z",
            "end": "2026-10-05T10:00:00Z",
            "accumulated_minutes": 60.0,
        },
        {
            "policy_version_id": v2,
            "start": "2026-10-05T10:00:00Z",
            "end": "2026-10-05T13:00:00Z",
            "accumulated_minutes": 60.0,
        },
    ]


def test_waiting_handoff_keeps_pause_continuous_then_new_calendar_counts(conn):
    v1 = _queue(conn, "等待前队列", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(conn, "等待后队列", warn=60, esc=180, open_h=12, close_h=20)
    tid = _create(conn, "等待中转派", v1, parse("2026-10-05T09:00:00Z"))
    set_status(conn, tid, "wait", 1, parse("2026-10-05T09:30:00Z"))

    detail = transfer(conn, tid, v2, 2, parse("2026-10-05T11:00:00Z"))
    assert detail["status"] == "waiting_customer"
    assert detail["revision"] == 3
    assert detail["timing"]["accumulated_minutes"] == 30
    assert detail["timing"]["projected_escalate_deadline"] == (
        "2026-10-05T14:30:00Z"
    )
    assert detail["timing"]["paused_intervals"] == [
        ["2026-10-05T09:30:00Z", None]
    ]

    set_status(conn, tid, "resume", 3, parse("2026-10-06T12:00:00Z"))
    detail = ticket_detail(conn, tid, parse("2026-10-06T13:00:00Z"))
    assert detail["status"] == "open"
    assert detail["timing"]["accumulated_minutes"] == 90
    assert [p["policy_version_id"] for p in detail["timing"]["counted_parts"]] == [
        v1,
        v2,
    ]
    assert detail["timing"]["paused_intervals"] == [
        ["2026-10-05T09:30:00Z", "2026-10-06T12:00:00Z"]
    ]


def test_multiple_handoffs_split_each_queue_chronologically(conn):
    v1 = _queue(conn, "第一队列", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(conn, "第二队列", warn=60, esc=180, open_h=12, close_h=20)
    v3 = _queue(conn, "第三队列", warn=60, esc=180, open_h=14, close_h=22)
    tid = _create(conn, "三队列接力", v1, parse("2026-10-05T09:00:00Z"))
    scan_all(conn, parse("2026-10-05T10:00:00Z"))

    transfer(conn, tid, v2, 1, parse("2026-10-05T10:00:00Z"))
    transfer(conn, tid, v3, 2, parse("2026-10-05T13:00:00Z"))
    scan_all(conn, parse("2026-10-05T13:00:00Z"))

    detail = ticket_detail(conn, tid, parse("2026-10-05T15:00:00Z"))
    assert [p["policy_version_id"] for p in detail["timing"]["counted_parts"]] == [
        v1,
        v2,
        v3,
    ]
    assert [p["accumulated_minutes"] for p in detail["timing"]["counted_parts"]] == [
        60.0,
        60.0,
        60.0,
    ]
    assert detail["timing"]["accumulated_minutes"] == 180
    assert len(detail["handoffs"]) == 2
    assert [
        (h["from_version"], h["to_version"]) for h in detail["handoffs"]
    ] == [(v1, v2), (v2, v3)]


def test_delivered_warning_is_not_emitted_again_after_handoff(conn):
    v1 = _queue(conn, "已警告队列", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(conn, "接手队列", warn=60, esc=180, open_h=12, close_h=20)
    tid = _create(conn, "不重复警告", v1, parse("2026-10-05T09:00:00Z"))
    assert scan_all(conn, parse("2026-10-05T10:00:00Z"))["fired"] == {
        tid: ["warning"]
    }

    transfer(conn, tid, v2, 1, parse("2026-10-05T10:00:00Z"))
    # 重复扫描、以及超过新队列升级阈值后，警告仍只通知一次；升级可首次通知。
    assert scan_all(conn, parse("2026-10-05T15:00:00Z"))["fired"] == {
        tid: ["escalation"]
    }
    assert scan_all(conn, parse("2026-10-05T16:00:00Z"))["fired"] == {}

    rows = conn.execute(
        "SELECT kind, policy_version_id FROM adjudication WHERE ticket_id=? ORDER BY id",
        (tid,),
    ).fetchall()
    assert [(r["kind"], r["policy_version_id"]) for r in rows] == [
        ("warning", v1),
        ("escalation", v2),
    ]


@pytest.mark.parametrize(
    "to_version,revision,at",
    [
        (None, 99, "2026-10-05T10:00:00Z"),  # 过期 revision
        (None, 1, "2026-10-05T10:00:00Z"),  # 目标就是当前队列
        (9999, 1, "2026-10-05T10:00:00Z"),  # 无效目标版本
        (None, 1, "2026-10-05T08:00:00Z"),  # 早于工单创建
    ],
)
def test_invalid_handoff_rolls_back_without_partial_record(
    conn, to_version, revision, at
):
    v1 = _queue(conn, "原队列", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(conn, "另一队列", warn=60, esc=180, open_h=12, close_h=20)
    tid = _create(conn, "无效转派", v1, parse("2026-10-05T09:00:00Z"))
    target = v1 if to_version is None else to_version

    with pytest.raises((BadRequest, Conflict)):
        transfer(conn, tid, target, revision, parse(at))

    ticket = get_ticket(conn, tid)
    assert ticket["policy_version_id"] == v1
    assert ticket["revision"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM handoff").fetchone()["c"] == 0


def test_policy_migration_cannot_rewrite_ticket_after_handoff(conn):
    v1 = _queue(conn, "迁移前", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(conn, "接手", warn=60, esc=180, open_h=12, close_h=20)
    tid = _create(conn, "迁移与转派", v1, parse("2026-10-05T09:00:00Z"))
    transfer(conn, tid, v2, 1, parse("2026-10-05T10:00:00Z"))

    with pytest.raises(BadRequest):
        migration_preview(conn, tid, v1, parse("2026-10-05T11:00:00Z"))


def test_handoff_api_conflict_and_evidence(tmp_path):
    """API 层：过期 revision 返回 409；成功转派返回逐段证据且不重复裁决。"""
    from fastapi.testclient import TestClient

    from app.db import connect, init_schema
    from app.main import create_app

    db_path = str(tmp_path / "handoff_api.db")
    c = connect(db_path)
    init_schema(c)
    v1 = _queue(c, "API 原队列", warn=60, esc=180, open_h=9, close_h=17)
    v2 = _queue(c, "API 接手队列", warn=60, esc=180, open_h=12, close_h=20)
    c.close()

    client = TestClient(create_app(db_path=db_path, enable_scanner=False, seed=False))
    t = client.post(
        "/api/tickets",
        json={"title": "API 转派", "policy_id": 1},
        params={"now": "2026-10-05T09:00:00Z"},
    ).json()
    client.post("/api/scan", params={"now": "2026-10-05T10:00:00Z"})

    # 过期 revision → 409，且不产生转派记录
    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": v2, "expected_revision": 99},
        params={"now": "2026-10-05T10:00:00Z"},
    )
    assert r.status_code == 409
    assert r.json()["current_revision"] == 1

    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": v2, "expected_revision": 1},
        params={"now": "2026-10-05T10:00:00Z"},
    )
    assert r.status_code == 200
    detail = r.json()
    assert detail["policy"]["policy_version_id"] == v2
    assert detail["revision"] == 2
    handoff = detail["handoffs"][0]
    assert handoff["from_version"] == v1
    assert handoff["to_version"] == v2
    assert handoff["evidence"]["to"]["timing"]["counted_parts"][0][
        "policy_version_id"
    ] == v1

    # 警告已在 v1 发出，新队列扫描不再重复发出
    client.post("/api/scan", params={"now": "2026-10-05T14:00:00Z"})
    client.post("/api/scan", params={"now": "2026-10-05T14:00:00Z"})
    kinds = [
        (a["kind"], a["policy_version_id"])
        for a in client.get(f"/api/tickets/{t['id']}").json()["adjudications"]
    ]
    assert ("warning", v1) in kinds
    assert sum(k == "warning" for k, _ in kinds) == 1
