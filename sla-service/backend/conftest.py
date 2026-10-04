import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from app.db import connect, init_schema  # noqa: E402
from app.services import create_policy_version  # noqa: E402

UTC = timezone.utc
CAL_START = datetime(2026, 9, 28, tzinfo=UTC)  # 周一
CAL_END = datetime(2026, 10, 16, tzinfo=UTC)


def weekdays(start=CAL_START, end=CAL_END, open_h=9, close_h=17):
    """生成工作日 09:00-17:00 UTC 区间。"""
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


_policy_seq = 0


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def make_policy(conn):
    """创建策略并发布一个版本，返回 policy_version_id。"""

    def factory(warn=120, esc=240, holidays=(), open_h=9, close_h=17, now=None):
        global _policy_seq
        _policy_seq += 1
        conn.execute("INSERT INTO policy (name) VALUES (?)", (f"策略{_policy_seq}",))
        pid = conn.execute(
            "SELECT id FROM policy WHERE name = ?", (f"策略{_policy_seq}",)
        ).fetchone()["id"]
        return create_policy_version(
            conn,
            pid,
            warn,
            esc,
            weekdays(open_h=open_h, close_h=close_h),
            list(holidays),
            now or datetime(2026, 9, 27, tzinfo=UTC),
        )

    return factory
