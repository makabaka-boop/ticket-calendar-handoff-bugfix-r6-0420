"""暂停跨界：等待客户暂停计时，恢复后在既有工作分钟上继续累积；
暂停区间本身跨越夜晚/周末/假日时不计入任何时间。"""

from app.services import create_ticket, get_ticket, set_status
from app.timeutil import parse
from app.timing import load_policy_version, raw_timing

HOLIDAY_WED = ("2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z")


def _timing(conn, tid, now):
    t = get_ticket(conn, tid)
    pv = load_policy_version(conn, t["policy_version_id"])
    return raw_timing(conn, t, pv, parse(now))


def test_pause_spans_weekend(conn, make_policy):
    make_policy(warn=120, esc=240)
    tid = create_ticket(
        conn, "跨周末暂停", None, parse("2026-10-09T16:00:00Z")
    )  # 周五 16:00
    set_status(conn, tid, "wait", 1, parse("2026-10-09T16:30:00Z"))  # 周五 16:30 暂停
    set_status(conn, tid, "resume", 2, parse("2026-10-12T09:30:00Z"))  # 周一 09:30 恢复

    raw = _timing(conn, tid, "2026-10-12T10:30:00Z")
    # 周五 30 分钟 + 周一 60 分钟；周末与暂停期间不计时
    assert raw["accumulated_seconds"] == 90 * 60
    assert raw["paused"] == [
        (parse("2026-10-09T16:30:00Z"), parse("2026-10-12T09:30:00Z"))
    ]


def test_pause_spans_holiday_cutout(conn, make_policy):
    make_policy(warn=120, esc=240, holidays=[HOLIDAY_WED])
    tid = create_ticket(
        conn, "跨假日暂停", None, parse("2026-09-29T16:00:00Z")
    )  # 周二 16:00
    set_status(conn, tid, "wait", 1, parse("2026-09-29T17:00:00Z"))  # 周二下班时暂停
    set_status(conn, tid, "resume", 2, parse("2026-10-01T10:00:00Z"))  # 周四 10:00 恢复

    raw = _timing(conn, tid, "2026-10-01T11:00:00Z")
    # 周二 16-17 计 60 分钟；周三假日 + 暂停均不计；周四 10-11 计 60 分钟
    assert raw["accumulated_seconds"] == 120 * 60
    assert raw["paused"] == [
        (parse("2026-09-29T17:00:00Z"), parse("2026-10-01T10:00:00Z"))
    ]
    # 计入区间不触碰周三假日
    assert all(
        e <= parse("2026-09-30T00:00:00Z") or s >= parse("2026-10-01T00:00:00Z")
        for s, e in raw["counted"]
    )


def test_paused_clock_frozen_and_projected(conn, make_policy):
    make_policy(warn=120, esc=240)
    tid = create_ticket(
        conn, "暂停中", None, parse("2026-10-05T09:00:00Z")
    )  # 周一 09:00
    set_status(
        conn, tid, "wait", 1, parse("2026-10-05T11:00:00Z")
    )  # 计了 120 分钟后暂停

    later = _timing(conn, tid, "2026-10-08T15:00:00Z")  # 几天后看：累计不变
    assert later["accumulated_seconds"] == 120 * 60
    assert later["escalate_deadline"] is None  # 暂停中无真实截止
    # “若现在恢复”的预计截止：还需 120 分钟 → 周四 17:00
    assert later["projected_escalate_deadline"] == parse("2026-10-08T17:00:00Z")
    # 末尾是开口暂停区间
    assert later["paused"][-1][1] is None
