"""UTC 区间运算。

工作日历 = 工作区间集合 减去 假日切口集合。
所有区间均为半开区间 [start, end)，元素为 tz-aware datetime 二元组。
"""

from __future__ import annotations

from datetime import datetime, timedelta

Interval = tuple[datetime, datetime]


def merge(intervals: list[Interval]) -> list[Interval]:
    """排序并合并相互重叠的区间（相邻但不重叠的保持分开）。"""
    out: list[Interval] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s < out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def subtract(base: list[Interval], cuts: list[Interval]) -> list[Interval]:
    """base 减去 cuts（假日切口），返回排序后的不重叠区间。"""
    cuts_m = merge(cuts)
    out: list[Interval] = []
    for bs, be in merge(base):
        cur = bs
        for cs, ce in cuts_m:
            if ce <= cur or cs >= be:
                continue
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
            if cur >= be:
                break
        if cur < be:
            out.append((cur, be))
    return out


def clip(intervals: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    """区间集合与 [start, end) 的交集。"""
    out: list[Interval] = []
    for s, e in intervals:
        if e <= start:
            continue
        if s >= end:
            break
        out.append((max(s, start), min(e, end)))
    return out


def overlap_seconds(intervals: list[Interval], start: datetime, end: datetime) -> float:
    """[start, end) 与区间集合重叠的总秒数。"""
    return sum((e - s).total_seconds() for s, e in clip(intervals, start, end))


def advance(
    intervals: list[Interval], start: datetime, seconds: float
) -> datetime | None:
    """从 start 起累积 seconds 秒有效区间时间后的时刻；区间耗尽则返回 None。"""
    remaining = seconds
    for s, e in intervals:
        if e <= start:
            continue
        seg_start = max(s, start)
        avail = (e - seg_start).total_seconds()
        if remaining <= avail:
            return seg_start + timedelta(seconds=remaining)
        remaining -= avail
    return None
