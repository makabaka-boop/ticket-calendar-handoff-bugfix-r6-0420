"""API 层：过期修订提交返回 409；扫描幂等；页面所需字段齐全。"""

from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services import create_policy_version
from app.timeutil import parse
from conftest import weekdays


def _client(tmp_path):
    db_path = str(tmp_path / "api.db")
    conn = connect(db_path)
    init_schema(conn)
    conn.execute("INSERT INTO policy (name) VALUES ('标准支持')")
    create_policy_version(
        conn,
        1,
        60,
        120,
        weekdays(),
        [("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")],
        parse("2026-09-27T00:00:00Z"),
    )
    conn.close()
    app = create_app(db_path=db_path, enable_scanner=False, seed=False)
    return TestClient(app), db_path


def test_stale_revision_returns_conflict(tmp_path):
    client, _ = _client(tmp_path)
    t = client.post(
        "/api/tickets",
        json={"title": "冲突演示"},
        params={"now": "2026-10-05T09:00:00Z"},
    ).json()
    assert t["revision"] == 1

    r = client.post(
        f"/api/tickets/{t['id']}/status",
        json={"action": "wait", "expected_revision": 1},
        params={"now": "2026-10-05T09:30:00Z"},
    )
    assert r.status_code == 200 and r.json()["revision"] == 2

    # 用过期 revision 提交 → 409，并带回当前 revision
    r = client.post(
        f"/api/tickets/{t['id']}/status",
        json={"action": "resume", "expected_revision": 1},
        params={"now": "2026-10-05T10:00:00Z"},
    )
    assert r.status_code == 409
    assert r.json()["current_revision"] == 2
    # 状态未被改变
    assert client.get(f"/api/tickets/{t['id']}").json()["status"] == "waiting_customer"


def test_scan_idempotent_via_api(tmp_path):
    client, db_path = _client(tmp_path)
    t = client.post(
        "/api/tickets",
        json={"title": "幂等扫描"},
        params={"now": "2026-10-05T09:00:00Z"},
    ).json()

    for _ in range(3):
        client.post(
            "/api/scan", params={"now": "2026-10-05T11:00:00Z"}
        )  # 累计 120 分钟
    detail = client.get(f"/api/tickets/{t['id']}").json()
    assert len(detail["adjudications"]) == 2  # warning + escalation 各一次

    # 模拟重启：同一数据库文件新建应用再扫描
    app2 = create_app(db_path=db_path, enable_scanner=False, seed=False)
    client2 = TestClient(app2)
    client2.post("/api/scan", params={"now": "2026-10-05T12:00:00Z"})
    detail = client2.get(f"/api/tickets/{t['id']}").json()
    assert len(detail["adjudications"]) == 2


def test_detail_payload_has_page_fields(tmp_path):
    client, _ = _client(tmp_path)
    t = client.post(
        "/api/tickets",
        json={"title": "页面字段"},
        params={"now": "2026-10-05T09:00:00Z"},
    ).json()
    client.post(
        f"/api/tickets/{t['id']}/status",
        json={"action": "wait", "expected_revision": 1},
        params={"now": "2026-10-05T10:00:00Z"},
    )
    client.post(
        f"/api/tickets/{t['id']}/status",
        json={"action": "resume", "expected_revision": 2},
        params={"now": "2026-10-06T09:00:00Z"},
    )
    client.post("/api/scan", params={"now": "2026-10-06T11:00:00Z"})

    d = client.get(
        f"/api/tickets/{t['id']}", params={"now": "2026-10-06T11:00:00Z"}
    ).json()
    timing = d["timing"]
    assert timing["escalate_deadline"] is not None  # 截止时刻
    assert timing["counted_intervals"]  # 已计入区间
    assert timing["paused_intervals"]  # 暂停区间
    assert d["adjudications"]  # 升级记录
    basis = d["adjudications"][0]["basis"]  # 裁决依据
    assert basis["accumulated_minutes"] >= basis["threshold_minutes"]
    assert basis["counted_intervals"]
