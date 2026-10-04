"""假日切口：日历 = 工作区间 − 假日切口，截止时刻必须跳过假日。"""

from datetime import datetime, timezone

from app.intervals import advance, overlap_seconds, subtract
from app.services import create_ticket, get_ticket
from app.timeutil import parse
from app.timing import load_policy_version, raw_timing

UTC = timezone.utc
HOLIDAY_WED = ("2026-09-30T00:00:00Z", "2026-10-01T00:00:00Z")  # 周三全天切口


def test_subtract_removes_holiday():
    work = [
        (datetime(2026, 9, 28, 9, tzinfo=UTC), datetime(2026, 10, 3, 17, tzinfo=UTC))
    ]
    eff = subtract(work, [(parse(HOLIDAY_WED[0]), parse(HOLIDAY_WED[1]))])
    assert (
        overlap_seconds(
            eff, parse("2026-09-30T00:00:00Z"), parse("2026-10-01T00:00:00Z")
        )
        == 0
    )
    # 切口前后仍在
    assert (
        overlap_seconds(
            eff, parse("2026-09-29T00:00:00Z"), parse("2026-09-30T00:00:00Z")
        )
        > 0
    )
    assert (
        overlap_seconds(
            eff, parse("2026-10-01T00:00:00Z"), parse("2026-10-02T00:00:00Z")
        )
        > 0
    )


def test_advance_skips_holiday():
    # 每天 09:00-17:00 三个工作日，周三被假日切口移除
    work = [
        (datetime(2026, 9, 29, 9, tzinfo=UTC), datetime(2026, 9, 29, 17, tzinfo=UTC)),
        (datetime(2026, 9, 30, 9, tzinfo=UTC), datetime(2026, 9, 30, 17, tzinfo=UTC)),
        (datetime(2026, 10, 1, 9, tzinfo=UTC), datetime(2026, 10, 1, 17, tzinfo=UTC)),
    ]
    eff = subtract(work, [(parse(HOLIDAY_WED[0]), parse(HOLIDAY_WED[1]))])
    # 周二 16:00 起 120 分钟：周二 1h + 周三 0（假日）+ 周四 1h → 周四 10:00
    assert advance(eff, parse("2026-09-29T16:00:00Z"), 120 * 60) == parse(
        "2026-10-01T10:00:00Z"
    )


def test_ticket_deadline_crosses_holiday_cutout(conn, make_policy):
    pv_id = make_policy(warn=60, esc=120, holidays=[HOLIDAY_WED])
    # 周二 16:00 创建，阈值 120 分钟有效工作时间
    tid = create_ticket(conn, "跨假日工单", None, parse("2026-09-29T16:00:00Z"))
    # 强制固定到我们刚建的版本（create_ticket 取最新版本，这里只有一个策略）
    conn.execute("UPDATE ticket SET policy_version_id = ? WHERE id = ?", (pv_id, tid))

    t = get_ticket(conn, tid)
    pv = load_policy_version(conn, pv_id)
    now = parse("2026-09-29T17:30:00Z")  # 周二下班后
    raw = raw_timing(conn, t, pv, now)

    assert raw["accumulated_seconds"] == 60 * 60  # 周二 16:00-17:00 计入 60 分钟
    # 周三全天是假日切口 → 截止时刻顺延到周四 10:00
    assert raw["escalate_deadline"] == parse("2026-10-01T10:00:00Z")
    # 计入区间不包含周三
    assert all(
        not (s < parse("2026-10-01T00:00:00Z") and e > parse("2026-09-30T00:00:00Z"))
        for s, e in raw["counted"]
    )
