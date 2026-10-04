"""Queue handoff preserves elapsed work under the calendar that supplied it."""

import json
from bisect import bisect_right
from datetime import datetime

from .db import tx
from .timeutil import iso, parse
from .timing import PolicyVersionView, load_policy_version, public_timing, raw_timing

SCHEMA = """CREATE TABLE IF NOT EXISTS handoff (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES ticket(id),
 from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
 at TEXT NOT NULL, evidence TEXT NOT NULL);"""


def history(conn, ticket_id):
    """Return handoff evidence in chronological order."""
    out = []
    for r in conn.execute(
        "SELECT * FROM handoff WHERE ticket_id=? ORDER BY id", (ticket_id,)
    ).fetchall():
        item = dict(r)
        item["evidence"] = json.loads(item["evidence"]) if item["evidence"] else {}
        out.append(item)
    return out


def events(conn, ticket_id) -> list[tuple[datetime, int, int]]:
    """Return (timestamp, from_version, to_version) boundary tuples."""
    return [
        (parse(r["at"]), r["from_version"], r["to_version"])
        for r in conn.execute(
            "SELECT at, from_version, to_version FROM handoff WHERE ticket_id=? ORDER BY id",
            (ticket_id,),
        ).fetchall()
    ]


def has_handoffs(conn, ticket_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM handoff WHERE ticket_id=? LIMIT 1", (ticket_id,))
        .fetchone()
        is not None
    )


def _policy_snapshot(pv: PolicyVersionView) -> dict:
    return {
        "policy_id": pv.policy_id,
        "policy_version_id": pv.id,
        "name": pv.policy_name,
        "version": pv.version,
        "warn_minutes": pv.warn_minutes,
        "escalate_minutes": pv.escalate_minutes,
    }


def calendar_parts(
    conn,
    ticket,
    current: PolicyVersionView,
    start,
    end,
    cache: dict[int, PolicyVersionView] | None = None,
):
    """Split a clock-running interval at each queue handoff boundary.

    Every piece is evaluated with the policy version that owned the ticket for
    that exact time. ``current`` is the owner of a point with no later handoff.
    """
    cache = cache if cache is not None else {}
    cuts = events(conn, ticket["id"])
    if not cuts:
        return [(current, start, end)]

    times = [h[0] for h in cuts]
    idx = bisect_right(times, start)
    if idx == 0:
        # 段开始于第一次转派之前：该部分属于转出队列（handoff 记录里的
        # from_version），即使传入的 current 已经是最后接手的队列版本。
        pv_id = cuts[0][1]
    elif idx == len(cuts):
        pv_id = current.id
    else:
        pv_id = cuts[idx][1]
    if pv_id != current.id:
        pv = cache.setdefault(pv_id, load_policy_version(conn, pv_id))
    else:
        pv = current

    parts = []
    lo = start
    for at, _from_version, to_version in cuts[idx:]:
        if at >= end:
            break
        if at > lo:
            parts.append((pv, lo, at))
        lo = at
        pv_id = to_version
        pv = current if pv_id == current.id else cache.setdefault(
            pv_id, load_policy_version(conn, pv_id)
        )
    if lo < end:
        parts.append((pv, lo, end))
    return parts


def transfer(conn, ticket_id, to_version, revision, now):
    from .services import BadRequest, Conflict, get_ticket, ticket_detail

    with tx(conn):
        ticket = get_ticket(conn, ticket_id)
        if ticket["revision"] != revision:
            raise Conflict(ticket["revision"])
        if ticket["status"] == "resolved":
            raise BadRequest("resolved tickets cannot be handed off")
        try:
            target = load_policy_version(conn, to_version)
        except KeyError as exc:
            raise BadRequest(str(exc)) from exc
        if target.id == ticket["policy_version_id"]:
            raise BadRequest("already assigned to this policy version")

        previous = events(conn, ticket_id)
        if now < parse(ticket["created_at"]) or (
            previous and now < previous[-1][0]
        ):
            raise BadRequest("handoff time precedes ticket history")

        source = load_policy_version(conn, ticket["policy_version_id"])
        before = public_timing(raw_timing(conn, ticket, source, now))
        cur = conn.execute(
            "INSERT INTO handoff(ticket_id,from_version,to_version,at,evidence) "
            "VALUES(?,?,?,?,?)",
            (ticket_id, source.id, target.id, iso(now), json.dumps({})),
        )
        handoff_id = cur.lastrowid
        updated = conn.execute(
            "UPDATE ticket SET policy_version_id=?,revision=revision+1 "
            "WHERE id=? AND revision=?",
            (target.id, ticket_id, revision),
        )
        if updated.rowcount == 0:
            raise Conflict(revision + 1)

        # Re-read after the assignment; waiting stays waiting and no running
        # segment is opened or closed, so the pause interval remains continuous.
        ticket = conn.execute(
            "SELECT * FROM ticket WHERE id=?", (ticket_id,)
        ).fetchone()
        after = public_timing(raw_timing(conn, ticket, target, now))
        evidence = {
            "as_of": iso(now),
            "status": ticket["status"],
            "from": {
                "policy": _policy_snapshot(source),
                "timing": before,
            },
            "to": {
                "policy": _policy_snapshot(target),
                "timing": after,
            },
        }
        conn.execute(
            "UPDATE handoff SET evidence=? WHERE id=?",
            (json.dumps(evidence), handoff_id),
        )

    return ticket_detail(conn, ticket_id, now)
