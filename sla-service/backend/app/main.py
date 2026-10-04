"""FastAPI 入口：REST API + 后台扫描计时器 + 前端静态资源。"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import services
from .db import connect, init_schema
from .seed import seed_if_empty
from .timeutil import now_utc, parse

DEFAULT_DB = str(Path(__file__).resolve().parents[1] / "sla.db")


# ---------------------------------------------------------------- 请求模型


class TicketCreate(BaseModel):
    title: str
    policy_id: int | None = None


class StatusChange(BaseModel):
    action: str  # wait | resume | resolve
    expected_revision: int


class PolicyVersionCreate(BaseModel):
    warn_minutes: int
    escalate_minutes: int
    work_intervals: list[tuple[str, str]]
    holiday_intervals: list[tuple[str, str]] = []


class MigrateRequest(BaseModel):
    to_version_id: int
    expected_revision: int
    actor: str | None = None


# ---------------------------------------------------------------- 应用工厂


def create_app(
    db_path: str | None = None,
    scan_interval: float = 15.0,
    enable_scanner: bool = True,
    seed: bool = True,
) -> FastAPI:
    db_path = db_path or os.environ.get("SLA_DB", DEFAULT_DB)

    conn = connect(db_path)
    init_schema(conn)
    if seed:
        seed_if_empty(conn)
    conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if enable_scanner:
            task = asyncio.create_task(_scanner_loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="工单时限计时服务", lifespan=lifespan)
    app.state.db_path = db_path
    app.state.scan_interval = scan_interval
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    async def _scanner_loop():
        while True:
            await asyncio.sleep(app.state.scan_interval)
            try:
                await asyncio.to_thread(_scan_once)
            except Exception as e:  # 扫描器永不退出
                print(f"[scanner] {e}")

    def _scan_once():
        c = connect(db_path)
        try:
            services.scan_all(c, now_utc())
        finally:
            c.close()

    # ---------------- 依赖 ----------------

    def get_conn():
        c = connect(db_path)
        try:
            yield c
        finally:
            c.close()

    def get_now(now: str | None = Query(None)) -> datetime:
        return parse(now) if now else now_utc()

    # ---------------- 异常 ----------------

    @app.exception_handler(services.NotFound)
    async def _(req: Request, e: services.NotFound):
        return JSONResponse({"error": str(e)}, status_code=404)

    @app.exception_handler(services.BadRequest)
    async def _(req: Request, e: services.BadRequest):
        return JSONResponse({"error": str(e)}, status_code=400)

    @app.exception_handler(services.Conflict)
    async def _(req: Request, e: services.Conflict):
        return JSONResponse(
            {
                "error": "revision 已过期，请刷新后重试",
                "current_revision": e.current_revision,
            },
            status_code=409,
        )

    # ---------------- 路由 ----------------

    @app.get("/api/meta")
    def meta():
        return {"server_time": now_utc().isoformat(), "scan_interval": scan_interval}

    @app.get("/api/tickets")
    def list_tickets(conn=Depends(get_conn), now=Depends(get_now)):
        return services.list_tickets(conn, now)

    @app.post("/api/tickets", status_code=201)
    def create_ticket(body: TicketCreate, conn=Depends(get_conn), now=Depends(get_now)):
        tid = services.create_ticket(conn, body.title, body.policy_id, now)
        return services.ticket_detail(conn, tid, now)

    @app.post("/api/tickets/{ticket_id}/handoff")
    def handoff(
        ticket_id: int,
        body: MigrateRequest,
        conn=Depends(get_conn),
        now=Depends(get_now),
    ):
        from .handoff import transfer

        return transfer(
            conn, ticket_id, body.to_version_id, body.expected_revision, now
        )

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: int, conn=Depends(get_conn), now=Depends(get_now)):
        return services.ticket_detail(conn, ticket_id, now)

    @app.post("/api/tickets/{ticket_id}/status")
    def change_status(
        ticket_id: int, body: StatusChange, conn=Depends(get_conn), now=Depends(get_now)
    ):
        return services.set_status(
            conn, ticket_id, body.action, body.expected_revision, now
        )

    @app.get("/api/policies")
    def list_policies(conn=Depends(get_conn)):
        out = []
        for p in conn.execute("SELECT * FROM policy ORDER BY id").fetchall():
            versions = []
            for v in conn.execute(
                "SELECT * FROM policy_version WHERE policy_id = ? ORDER BY version",
                (p["id"],),
            ).fetchall():
                ivs = conn.execute(
                    "SELECT kind, start_utc, end_utc FROM calendar_interval WHERE policy_version_id = ? ORDER BY start_utc",
                    (v["id"],),
                ).fetchall()
                versions.append(
                    {
                        "policy_version_id": v["id"],
                        "version": v["version"],
                        "warn_minutes": v["warn_minutes"],
                        "escalate_minutes": v["escalate_minutes"],
                        "created_at": v["created_at"],
                        "work_intervals": [
                            [i["start_utc"], i["end_utc"]]
                            for i in ivs
                            if i["kind"] == "work"
                        ],
                        "holiday_intervals": [
                            [i["start_utc"], i["end_utc"]]
                            for i in ivs
                            if i["kind"] == "holiday"
                        ],
                    }
                )
            out.append({"policy_id": p["id"], "name": p["name"], "versions": versions})
        return out

    @app.post("/api/policies/{policy_id}/versions", status_code=201)
    def add_policy_version(
        policy_id: int,
        body: PolicyVersionCreate,
        conn=Depends(get_conn),
        now=Depends(get_now),
    ):
        pv_id = services.create_policy_version(
            conn,
            policy_id,
            body.warn_minutes,
            body.escalate_minutes,
            body.work_intervals,
            body.holiday_intervals,
            now,
        )
        return {"policy_version_id": pv_id}

    @app.get("/api/tickets/{ticket_id}/migration-preview")
    def preview_migration(
        ticket_id: int,
        to_version_id: int = Query(...),
        conn=Depends(get_conn),
        now=Depends(get_now),
    ):
        return services.migration_preview(conn, ticket_id, to_version_id, now)

    @app.post("/api/tickets/{ticket_id}/migrate")
    def migrate(
        ticket_id: int,
        body: MigrateRequest,
        conn=Depends(get_conn),
        now=Depends(get_now),
    ):
        return services.migrate_policy(
            conn, ticket_id, body.to_version_id, body.expected_revision, body.actor, now
        )

    @app.post("/api/scan")
    def scan(conn=Depends(get_conn), now=Depends(get_now)):
        return services.scan_all(conn, now)

    # ---------------- 前端静态资源 ----------------

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
