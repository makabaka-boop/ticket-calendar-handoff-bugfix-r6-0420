"""统一使用 UTC。DB 与 API 中一律为 ISO 8601 字符串（Z 结尾）。"""

from __future__ import annotations

from datetime import datetime, timezone


def parse(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
