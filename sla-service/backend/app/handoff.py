"""Queue handoff preserves elapsed work under the calendar that supplied it."""

import json
from .db import tx
from .timeutil import iso, parse
from .timing import load_policy_version, raw_timing, public_timing

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


def transfer(conn, ticket_id, to_version, revision, now):
    from .services import get_ticket, BadRequest, Conflict, ticket_detail

    with tx(conn):
        ticket = get_ticket(conn, ticket_id)
        if ticket["revision"] != revision:
            raise Conflict(ticket["revision"])
        if ticket["status"] == "resolved":
            raise BadRequest("resolved tickets cannot be handed off")
        target = load_policy_version(conn, to_version)
        if target.id == ticket["policy_version_id"]:
            raise BadRequest("already assigned to this policy version")
        previous = history(conn, ticket_id)
        if now < parse(ticket["created_at"]) or (
            previous and now < parse(previous[-1]["at"])
        ):
            raise BadRequest("handoff time precedes ticket history")
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
            "UPDATE ticket SET policy_version_id=?,revision=revision+1 WHERE id=?",
            (to_version, ticket_id),
        )
    return ticket_detail(conn, ticket_id, now)


def calendar_parts(conn, ticket, current, start, end):
    return [(current, start, end)]
