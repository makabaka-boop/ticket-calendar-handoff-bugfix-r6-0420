"""到期与解决竞争：后台扫描裁决与用户解决操作按同一事务顺序裁决。

- 解决先提交 → 扫描不再登记任何裁决；
- 扫描先提交 → 裁决已登记，解决照常生效，历史保留；
- 并发交错下两种序列化顺序都合法，但结果必须自洽且不重复。
"""

import threading

from app.db import connect, init_schema
from app.services import (
    adjudicate_ticket,
    create_ticket,
    get_ticket,
    scan_all,
    set_status,
)
from app.timeutil import parse

NOW = parse("2026-10-05T13:00:00Z")  # 周一 13:00，工单 09:00 创建已累积 240 分钟


def _make_expired_ticket(conn, make_policy):
    make_policy(warn=120, esc=240)
    return create_ticket(conn, "临界工单", None, parse("2026-10-05T09:00:00Z"))


def _adjudications(conn, tid):
    return conn.execute(
        "SELECT * FROM adjudication WHERE ticket_id = ?", (tid,)
    ).fetchall()


def test_resolve_then_scan_registers_nothing(conn, make_policy):
    tid = _make_expired_ticket(conn, make_policy)
    set_status(conn, tid, "resolve", 1, NOW)
    assert scan_all(conn, NOW)["fired"] == {}
    assert _adjudications(conn, tid) == []


def test_scan_then_resolve_keeps_adjudication(conn, make_policy):
    tid = _make_expired_ticket(conn, make_policy)
    fired = adjudicate_ticket(conn, tid, NOW)
    assert fired == ["warning", "escalation"]
    set_status(conn, tid, "resolve", 1, NOW)
    t = get_ticket(conn, tid)
    assert t["status"] == "resolved"
    assert len(_adjudications(conn, tid)) == 2  # 历史裁决保留


def test_concurrent_expiry_and_resolve(db_path, make_policy):
    conn = connect(db_path)
    init_schema(conn)
    rounds = 30
    for _ in range(rounds):
        tid = _make_expired_ticket(conn, make_policy)
        barrier = threading.Barrier(2)
        errors = []

        def do_resolve():
            c = connect(db_path)
            try:
                barrier.wait()
                set_status(c, tid, "resolve", 1, NOW)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                c.close()

        def do_scan():
            c = connect(db_path)
            try:
                barrier.wait()
                adjudicate_ticket(c, tid, NOW)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                c.close()

        threads = [
            threading.Thread(target=do_resolve),
            threading.Thread(target=do_scan),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors, f"并发操作出错: {errors}"
        t = get_ticket(conn, tid)
        assert t["status"] == "resolved"  # 解决必然生效
        rows = _adjudications(conn, tid)
        # 两种序列化顺序：解决先 → 0 条；扫描先 → 警告+升级各一条
        assert len(rows) in (0, 2)
        if rows:
            assert {r["kind"] for r in rows} == {"warning", "escalation"}
            assert all(r["adjudicated_at"] <= t["resolved_at"] for r in rows)
        # 重复扫描不会补登记或重复登记
        scan_all(conn, NOW)
        assert len(_adjudications(conn, tid)) == len(rows)
    conn.close()


def test_repeated_scans_are_idempotent_across_reopen(db_path, make_policy):
    """重启（新连接）与重复扫描后，警告/升级只登记一次。"""
    conn = connect(db_path)
    init_schema(conn)
    tid = _make_expired_ticket(conn, make_policy)
    scan_all(conn, NOW)
    scan_all(conn, NOW)
    conn.close()

    conn2 = connect(db_path)  # 模拟重启后再次扫描
    scan_all(conn2, NOW)
    rows = _adjudications(conn2, tid)
    assert len(rows) == 2
    assert {r["kind"] for r in rows} == {"warning", "escalation"}
    conn2.close()
