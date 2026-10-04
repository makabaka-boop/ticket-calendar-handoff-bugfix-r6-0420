"""Queue handoff preserves elapsed work under the calendar that supplied it.

转派（队列接力）语义：
- `handoff` 表把工单时间轴切成若干“指派区间”：每个区间内的工作分钟按当时
  所属队列（策略版本）的日历计入，历史投入不因转派而消失或被重新增加；
- 最后一个开口区间跟随工单当前固定的策略版本——后续工作服从新队列规则；
- 已送达的警告/升级在同一转派链内不重复送达；策略迁移（migrate）会重置
  裁决基线（见 services.adjudicate_ticket 与 chain_versions）。
"""

import json

from .db import tx
from .timeutil import iso, parse
from .timing import load_policy_version, public_timing, raw_timing

SCHEMA = """CREATE TABLE IF NOT EXISTS handoff (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES ticket(id),
 from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
 at TEXT NOT NULL, evidence TEXT NOT NULL);"""


def history(conn, ticket_id):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM handoff WHERE ticket_id=? ORDER BY id", (ticket_id,)
        )
    ]


def version_timeline(conn, ticket, current):
    """把工单时间轴切成 (策略版本视图, 起点, 终点|None) 的指派区间。

    转派边界之前的区间按当时所属版本计时；最后的开口区间跟随
    `current`——调用方给定的当前版本（迁移预览可传入目标版本，
    转派前证据可传入旧版本）。
    """
    hs = history(conn, ticket["id"])
    created = parse(ticket["created_at"])
    if not hs:
        return [(current, created, None)]
    cache = {current.id: current}

    def view(version_id):
        if version_id not in cache:
            cache[version_id] = load_policy_version(conn, version_id)
        return cache[version_id]

    spans = []
    start, version_id = created, hs[0]["from_version"]
    for h in hs:
        at = parse(h["at"])
        spans.append((view(version_id), start, at))
        start, version_id = at, h["to_version"]
    spans.append((current, start, None))
    return spans


def calendar_parts(conn, ticket, current, start, end):
    """[start, end) 与各指派区间的交集；每段携带当时所属版本的日历。"""
    parts = []
    for view, lo, hi in version_timeline(conn, ticket, current):
        s = max(start, lo)
        e = end if hi is None else min(end, hi)
        if s < e:
            parts.append((view, s, e))
    return parts


def chain_versions(conn, ticket):
    """当前转派链上的策略版本集合。

    从当前固定版本沿转派记录向前回溯；若最近一次变更不是转派
    （迁移造成断档），裁决基线已被迁移重置，回溯停止。
    """
    tip = ticket["policy_version_id"]
    versions = {tip}
    for h in reversed(history(conn, ticket["id"])):
        if h["to_version"] != tip:
            break
        versions.add(h["from_version"])
        tip = h["from_version"]
    return versions


def delivered_kinds(conn, ticket):
    """当前转派链内已送达（已登记）的裁决种类，转派后不重复送达。"""
    versions = sorted(chain_versions(conn, ticket))
    marks = ",".join("?" for _ in versions)
    rows = conn.execute(
        "SELECT DISTINCT kind FROM adjudication "
        f"WHERE ticket_id = ? AND policy_version_id IN ({marks})",
        (ticket["id"], *versions),
    ).fetchall()
    return {r["kind"] for r in rows}


def transfer(conn, ticket_id, to_version, revision, now):
    """转派到另一队列：历史分钟保留在原日历下，后续服从新版本。

    全部前置校验（过期修订、已解决、目标版本无效、时间倒挂）先于任何
    写入，且登记证据与切换版本在同一事务内——失败整体回滚，不留下
    半次转派的记录。
    """
    from .services import BadRequest, Conflict, NotFound, get_ticket, ticket_detail

    with tx(conn):
        ticket = get_ticket(conn, ticket_id)
        if ticket["revision"] != revision:
            raise Conflict(ticket["revision"])
        if ticket["status"] == "resolved":
            raise BadRequest("resolved tickets cannot be handed off")
        try:
            target = load_policy_version(conn, to_version)
        except KeyError:
            raise NotFound(f"policy_version {to_version} 不存在") from None
        if target.id == ticket["policy_version_id"]:
            raise BadRequest("already assigned to this policy version")
        previous = history(conn, ticket_id)
        if now < parse(ticket["created_at"]) or (
            previous and now < parse(previous[-1]["at"])
        ):
            raise BadRequest("handoff time precedes ticket history")
        # 转派前证据：历史分钟按当时所属队列日历计入后的快照
        before = public_timing(
            raw_timing(
                conn,
                ticket,
                load_policy_version(conn, ticket["policy_version_id"]),
                now,
            )
        )
        conn.execute(
            "INSERT INTO handoff(ticket_id,from_version,to_version,at,evidence) VALUES(?,?,?,?,?)",
            (
                ticket_id,
                ticket["policy_version_id"],
                to_version,
                iso(now),
                json.dumps(before),
            ),
        )
        conn.execute(
            "UPDATE ticket SET policy_version_id=?,revision=revision+1 WHERE id=? AND revision=?",
            (to_version, ticket_id, revision),
        )
    return ticket_detail(conn, ticket_id, now)
