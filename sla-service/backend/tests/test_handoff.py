"""队列转派（handoff）：历史投入按当时所属队列的日历计入，后续工作服从
新队列规则；暂停状态延续；已送达的警告/升级不重复送达；过期修订与无效
转派整体回滚，不留下半次转派的记录。
"""

import threading

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.handoff import transfer
from app.services import (
    BadRequest,
    Conflict,
    NotFound,
    adjudicate_ticket,
    create_policy_version,
    create_ticket,
    get_ticket,
    migrate_policy,
    scan_all,
    set_status,
    ticket_detail,
)
from app.timeutil import parse
from conftest import weekdays

# 三个队列：不同工作时段与阈值
# A：工作日 09:00-17:00，警告 120 / 升级 240
# B：工作日 12:00-20:00，警告 10000 / 升级 10001（默认不触发）
# C：工作日 15:00-23:00，警告 10000 / 升级 10001


@pytest.fixture
def queues(conn, make_policy):
    a = make_policy(warn=120, esc=240, open_h=9, close_h=17)
    b = make_policy(warn=10000, esc=10001, open_h=12, close_h=20)
    c = make_policy(warn=10000, esc=10001, open_h=15, close_h=23)
    return a, b, c


def _handoffs(conn, tid):
    return conn.execute(
        "SELECT * FROM handoff WHERE ticket_id = ? ORDER BY id", (tid,)
    ).fetchall()


def _adjudications(conn, tid):
    return conn.execute(
        "SELECT * FROM adjudication WHERE ticket_id = ? ORDER BY id", (tid,)
    ).fetchall()


# ------------------------------------------------------------ 历史计入与后续规则


def test_elapsed_minutes_survive_handoff(conn, queues):
    """转派瞬间累计分钟不变；之前只在旧日历有效的时间段不被重算掉。"""
    vA, vB, _ = queues
    tid = create_ticket(conn, "转派保留", 1, parse("2026-09-28T09:00:00Z"))  # 周一
    before = ticket_detail(conn, tid, parse("2026-09-28T13:00:00Z"))
    assert before["timing"]["accumulated_minutes"] == 240  # A 日历 09-13

    detail = transfer(conn, tid, vB, 1, parse("2026-09-28T13:00:00Z"))
    # 转派后瞬间：累计保持 240（B 日历 12 点才开始，若全部重算只剩 60）
    assert detail["timing"]["accumulated_minutes"] == 240
    # 转派证据与转派后一致
    ev = detail["handoffs"][0]["evidence"]
    assert ev["accumulated_minutes"] == 240
    assert detail["handoffs"][0]["from_version"] == vA
    assert detail["handoffs"][0]["to_version"] == vB


def test_future_work_follows_new_calendar(conn, queues):
    """转派后只在 B 的工作时段内累积；截止时刻按 B 的日历推进。"""
    vA, vB, _ = queues
    tid = create_ticket(conn, "新日历", 1, parse("2026-09-28T09:00:00Z"))
    transfer(conn, tid, vB, 1, parse("2026-09-28T13:00:00Z"))  # 已计 240

    # 周一 13:00-14:00：B 工作时段（12-20）内 → +60；A 的 09-12 时段不重算
    detail = ticket_detail(conn, tid, parse("2026-09-28T14:00:00Z"))
    assert detail["timing"]["accumulated_minutes"] == 300
    # 计入区间不落入 B 日历之外（12 点前没有 B 的计入）
    assert all(
        s >= parse("2026-09-28T12:00:00Z") or e <= parse("2026-09-28T13:00:00Z")
        for s, e in (
            (parse(a), parse(b)) for a, b in detail["timing"]["counted_intervals"]
        )
    )


def test_multi_handoff_piecewise_timing(conn, queues):
    """A→B→C 接力：每段按当时队列日历计入，转派记录按时间有序。"""
    vA, vB, vC = queues
    tid = create_ticket(conn, "接力", 1, parse("2026-09-28T09:00:00Z"))
    # A 段 09:00-11:00 → 120 分钟
    d = transfer(conn, tid, vB, 1, parse("2026-09-28T11:00:00Z"))
    # B 段 11:00-14:00：B 日历 12 点开始 → 12:00-14:00 计 120
    d = transfer(conn, tid, vC, d["revision"], parse("2026-09-28T14:00:00Z"))
    # C 段 14:00-16:00：C 日历 15 点开始 → 15:00-16:00 计 60
    detail = ticket_detail(conn, tid, parse("2026-09-28T16:00:00Z"))
    assert detail["timing"]["accumulated_minutes"] == 120 + 120 + 60

    hs = _handoffs(conn, tid)
    assert [(h["from_version"], h["to_version"]) for h in hs] == [(vA, vB), (vB, vC)]
    assert hs[0]["at"] < hs[1]["at"]


# ------------------------------------------------------------ 暂停延续


def test_pause_survives_handoff_and_resume(conn, make_policy):
    """等待客户的工单转派后仍暂停、累计冻结；恢复后在新日历上继续累积。"""
    vA = make_policy(warn=120, esc=240, open_h=9, close_h=17)
    vB = make_policy(warn=590, esc=600, open_h=10, close_h=18)
    tid = create_ticket(conn, "暂停转派", 1, parse("2026-09-28T09:00:00Z"))
    set_status(conn, tid, "wait", 1, parse("2026-09-28T11:00:00Z"))  # 计 120 后暂停

    d = transfer(conn, tid, vB, 2, parse("2026-09-28T12:00:00Z"))  # 暂停中转派
    assert d["status"] == "waiting_customer"
    assert d["timing"]["accumulated_minutes"] == 120  # 与转派前证据一致
    assert d["handoffs"][0]["evidence"]["accumulated_minutes"] == 120
    assert d["timing"]["paused_intervals"][-1][1] is None  # 暂停延续（开口）
    # 暂停中预计截止按新日历：还需 600-120=480 分钟 → 周一 12:00 起 8 小时 → 20:00？
    # B 工作日 10-18（8 小时）→ 周一 12:00 + 480 = 周一 20:00 超出 → 周二 10:00+120 = 12:00
    assert d["timing"]["projected_escalate_deadline"] == "2026-09-29T12:00:00Z"

    # 周三 10:00 恢复（B 日历），到周三 12:00 再计 120
    d = set_status(conn, tid, "resume", d["revision"], parse("2026-09-30T10:00:00Z"))
    d = ticket_detail(conn, tid, parse("2026-09-30T12:00:00Z"))
    assert d["timing"]["accumulated_minutes"] == 240
    assert d["timing"]["paused_intervals"] == [
        ["2026-09-28T11:00:00Z", "2026-09-30T10:00:00Z"]
    ]
    # 运行中截止：还需 360 → 周三 12:00 + 360 = 18:00
    assert d["timing"]["escalate_deadline"] == "2026-09-30T18:00:00Z"


# ------------------------------------------------------------ 裁决不重复


def test_delivered_adjudications_not_repeated_after_handoff(conn, make_policy):
    """A 下已发出的警告/升级，转派到阈值更低的 B 后不重复送达。"""
    vA = make_policy(warn=120, esc=240, open_h=9, close_h=17)
    vB = make_policy(warn=60, esc=180, open_h=10, close_h=18)
    tid = create_ticket(conn, "不重复裁决", 1, parse("2026-09-28T09:00:00Z"))
    fired = adjudicate_ticket(conn, tid, parse("2026-09-28T13:00:00Z"))  # 累计 240
    assert fired == ["warning", "escalation"]

    transfer(conn, tid, vB, 1, parse("2026-09-28T13:00:00Z"))
    # B 的阈值（60/180）早已被超过，但已送达的不再送达
    assert adjudicate_ticket(conn, tid, parse("2026-09-28T14:00:00Z")) == []
    rows = _adjudications(conn, tid)
    assert [(r["kind"], r["policy_version_id"]) for r in rows] == [
        ("warning", vA),
        ("escalation", vA),
    ]


def test_new_kind_still_fires_under_new_version(conn, make_policy):
    """未送达过的种类在新版本阈值下仍会触发（且只一次）。"""
    vA = make_policy(warn=120, esc=10000, open_h=9, close_h=17)
    vB = make_policy(warn=100, esc=180, open_h=10, close_h=18)
    tid = create_ticket(conn, "新种类", 1, parse("2026-09-28T09:00:00Z"))
    adjudicate_ticket(conn, tid, parse("2026-09-28T11:00:00Z"))  # 累计 120 → 警告@A

    transfer(conn, tid, vB, 1, parse("2026-09-28T11:00:00Z"))
    # 累计 120 < B 升级 180：无新裁决；警告已送达不重复
    assert adjudicate_ticket(conn, tid, parse("2026-09-28T11:30:00Z")) == []
    # 到 12:00 累计 180 → 升级在 B 下登记一次；警告仍不重复
    assert adjudicate_ticket(conn, tid, parse("2026-09-28T12:00:00Z")) == ["escalation"]
    assert adjudicate_ticket(conn, tid, parse("2026-09-28T13:00:00Z")) == []
    rows = _adjudications(conn, tid)
    assert [(r["kind"], r["policy_version_id"]) for r in rows] == [
        ("warning", vA),
        ("escalation", vB),
    ]


def test_adjudication_idempotent_across_handoffs_and_restart(db_path, make_policy):
    """多队列接力 + 重复扫描 + 重启（新连接）：裁决不重复、不丢失。"""
    conn = connect(db_path)
    init_schema(conn)
    vA = make_policy(warn=120, esc=240, open_h=9, close_h=17)
    vB = make_policy(warn=60, esc=180, open_h=10, close_h=18)
    tid = create_ticket(conn, "接力幂等", 1, parse("2026-09-28T09:00:00Z"))
    scan_all(conn, parse("2026-09-28T13:00:00Z"))  # 警告+升级 @A
    transfer(conn, tid, vB, 1, parse("2026-09-28T13:00:00Z"))
    scan_all(conn, parse("2026-09-28T14:00:00Z"))
    scan_all(conn, parse("2026-09-28T15:00:00Z"))
    conn.close()

    conn2 = connect(db_path)  # 模拟重启后再扫描
    scan_all(conn2, parse("2026-09-28T16:00:00Z"))
    rows = _adjudications(conn2, tid)
    assert [(r["kind"], r["policy_version_id"]) for r in rows] == [
        ("warning", vA),
        ("escalation", vA),
    ]
    conn2.close()


def test_migration_after_handoff_resets_adjudication_baseline(conn, make_policy):
    """迁移（显式重定基线）后才允许按新版本重新裁决；纯转派不允许。"""
    vA = make_policy(warn=120, esc=240, open_h=9, close_h=17)
    vB = make_policy(warn=60, esc=180, open_h=10, close_h=18)
    # 与 B 同策略的新版本（迁移只能同策略），阈值不同
    pid_b = conn.execute(
        "SELECT policy_id FROM policy_version WHERE id=?", (vB,)
    ).fetchone()["policy_id"]
    vB2 = create_policy_version(
        conn,
        pid_b,
        90,
        200,
        weekdays(open_h=10, close_h=18),
        [],
        parse("2026-09-27T00:00:00Z"),
    )
    tid = create_ticket(conn, "迁移重置", 1, parse("2026-09-28T09:00:00Z"))
    adjudicate_ticket(conn, tid, parse("2026-09-28T13:00:00Z"))  # 警告+升级 @A

    d = transfer(conn, tid, vB, 1, parse("2026-09-28T13:00:00Z"))
    assert adjudicate_ticket(conn, tid, parse("2026-09-28T14:00:00Z")) == []  # 转派不重置

    # 显式迁移到同策略新版本 → 裁决基线重置，按 vB2 重新登记
    d = migrate_policy(conn, tid, vB2, d["revision"], actor=None, now=parse("2026-09-28T14:00:00Z"))
    fired = adjudicate_ticket(conn, tid, parse("2026-09-28T14:00:00Z"))
    assert fired == ["warning", "escalation"]
    kinds = {(r["kind"], r["policy_version_id"]) for r in _adjudications(conn, tid)}
    assert ("warning", vB2) in kinds and ("escalation", vB2) in kinds


# ------------------------------------------------------------ 失败整体回滚


def test_stale_revision_rejects_whole_handoff(conn, queues):
    vA, vB, _ = queues
    tid = create_ticket(conn, "过期修订", 1, parse("2026-09-28T09:00:00Z"))
    with pytest.raises(Conflict):
        transfer(conn, tid, vB, 99, parse("2026-09-28T10:00:00Z"))
    assert _handoffs(conn, tid) == []  # 不留半次转派
    assert get_ticket(conn, tid)["policy_version_id"] == vA
    assert get_ticket(conn, tid)["revision"] == 1


def test_invalid_handoffs_leave_no_record(conn, queues):
    vA, vB, _ = queues
    tid = create_ticket(conn, "无效转派", 1, parse("2026-09-28T09:00:00Z"))

    with pytest.raises(NotFound):  # 目标版本不存在
        transfer(conn, tid, 99999, 1, parse("2026-09-28T10:00:00Z"))
    with pytest.raises(BadRequest):  # 已是该版本
        transfer(conn, tid, vA, 1, parse("2026-09-28T10:00:00Z"))
    with pytest.raises(BadRequest):  # 转派时间早于工单创建
        transfer(conn, tid, vB, 1, parse("2026-09-27T10:00:00Z"))

    assert _handoffs(conn, tid) == []
    assert get_ticket(conn, tid)["revision"] == 1

    # 时间倒挂：第二次转派早于第一次
    d = transfer(conn, tid, vB, 1, parse("2026-09-28T12:00:00Z"))
    with pytest.raises(BadRequest):
        transfer(conn, tid, vA, d["revision"], parse("2026-09-28T11:00:00Z"))
    assert len(_handoffs(conn, tid)) == 1  # 只有第一次

    # 已解决的工单不能转派
    d = set_status(conn, tid, "resolve", d["revision"], parse("2026-09-28T13:00:00Z"))
    with pytest.raises(BadRequest):
        transfer(conn, tid, vA, d["revision"], parse("2026-09-28T14:00:00Z"))
    assert len(_handoffs(conn, tid)) == 1


def test_concurrent_handoffs_only_one_wins(db_path, make_policy):
    """并发的两个转派按事务串行化：一个成功，一个 409，只留一条记录。"""
    conn = connect(db_path)
    init_schema(conn)
    vA = make_policy(warn=120, esc=240, open_h=9, close_h=17)
    vB = make_policy(warn=60, esc=180, open_h=10, close_h=18)
    tid = create_ticket(conn, "并发转派", 1, parse("2026-09-28T09:00:00Z"))
    barrier = threading.Barrier(2)
    results, errors = [], []

    def do_handoff():
        c = connect(db_path)
        try:
            barrier.wait()
            results.append(transfer(c, tid, vB, 1, parse("2026-09-28T10:00:00Z")))
        except Conflict as e:
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=do_handoff) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(results) == 1 and len(errors) == 1
    assert len(_handoffs(conn, tid)) == 1
    assert get_ticket(conn, tid)["revision"] == 2
    conn.close()


# ------------------------------------------------------------ API 层


def _api_client(tmp_path):
    db_path = str(tmp_path / "handoff-api.db")
    conn = connect(db_path)
    init_schema(conn)
    conn.execute("INSERT INTO policy (name) VALUES ('队列A')")
    conn.execute("INSERT INTO policy (name) VALUES ('队列B')")
    vA = create_policy_version(
        conn, 1, 120, 240, weekdays(open_h=9, close_h=17), [],
        parse("2026-09-27T00:00:00Z"),
    )
    vB = create_policy_version(
        conn, 2, 60, 180, weekdays(open_h=10, close_h=18), [],
        parse("2026-09-27T00:00:00Z"),
    )
    conn.close()
    app = create_app(db_path=db_path, enable_scanner=False, seed=False)
    return TestClient(app), db_path, vA, vB


def _create(client, title="API 转派"):
    return client.post(
        "/api/tickets", json={"title": title}, params={"now": "2026-09-28T09:00:00Z"}
    ).json()


def test_handoff_api_happy_path_and_idempotent_scan(tmp_path):
    client, db_path, vA, vB = _api_client(tmp_path)
    t = _create(client)
    client.post("/api/scan", params={"now": "2026-09-28T13:00:00Z"})  # 警告+升级 @A

    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": vB, "expected_revision": 1},
        params={"now": "2026-09-28T13:00:00Z"},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["policy"]["policy_version_id"] == vB
    assert d["revision"] == 2
    assert d["timing"]["accumulated_minutes"] == 240  # 历史分钟保留
    assert len(d["handoffs"]) == 1
    h = d["handoffs"][0]
    assert (h["from_version"], h["to_version"]) == (vA, vB)
    assert h["evidence"]["accumulated_minutes"] == 240  # 证据与现状一致

    # 转派后重复扫描 + 重启后扫描：已送达的裁决不重复
    for _ in range(2):
        client.post("/api/scan", params={"now": "2026-09-28T15:00:00Z"})
    client2 = TestClient(create_app(db_path=db_path, enable_scanner=False, seed=False))
    client2.post("/api/scan", params={"now": "2026-09-28T16:00:00Z"})
    d = client2.get(f"/api/tickets/{t['id']}").json()
    assert [(a["kind"], a["policy_version_id"]) for a in d["adjudications"]] == [
        ("warning", vA),
        ("escalation", vA),
    ]


def test_handoff_api_rejections_leave_no_record(tmp_path):
    client, _, vA, vB = _api_client(tmp_path)

    # 过期修订 → 409
    t = _create(client, "过期修订")
    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": vB, "expected_revision": 99},
        params={"now": "2026-09-28T10:00:00Z"},
    )
    assert r.status_code == 409 and r.json()["current_revision"] == 1

    # 目标版本不存在 → 404
    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": 9999, "expected_revision": 1},
        params={"now": "2026-09-28T10:00:00Z"},
    )
    assert r.status_code == 404

    # 同一版本 → 400
    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": vA, "expected_revision": 1},
        params={"now": "2026-09-28T10:00:00Z"},
    )
    assert r.status_code == 400

    # 时间倒挂（早于创建）→ 400
    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": vB, "expected_revision": 1},
        params={"now": "2026-09-27T10:00:00Z"},
    )
    assert r.status_code == 400

    # 全部失败都不留半次转派：无记录、版本与修订不变
    d = client.get(f"/api/tickets/{t['id']}").json()
    assert d["handoffs"] == []
    assert d["revision"] == 1
    assert d["policy"]["policy_version_id"] == vA

    # 已解决工单 → 400，同样不留记录
    client.post(
        f"/api/tickets/{t['id']}/status",
        json={"action": "resolve", "expected_revision": 1},
        params={"now": "2026-09-28T11:00:00Z"},
    )
    r = client.post(
        f"/api/tickets/{t['id']}/handoff",
        json={"to_version_id": vB, "expected_revision": 2},
        params={"now": "2026-09-28T12:00:00Z"},
    )
    assert r.status_code == 400
    assert client.get(f"/api/tickets/{t['id']}").json()["handoffs"] == []
