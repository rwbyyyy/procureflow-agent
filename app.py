"""仅监听本机的 Demo API；演示身份、模型配置和生产限制见 README。"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from agent import AgentEngine
from domain import Database, Decision

ROOT = Path(__file__).resolve().parent
ACCOUNTS = {
    "lin": {"name": "林言", "role": "employee", "tenant": "atlas", "label": "研发申请人"},
    "chen": {"name": "陈知远", "role": "approver", "tenant": "atlas", "label": "采购审批人"},
    "audit": {"name": "周宁", "role": "auditor", "tenant": "atlas", "label": "审计观察员"},
    "nova": {"name": "沈青", "role": "employee", "tenant": "nova", "label": "另一组织申请人"},
}


class Login(BaseModel):
    account: Literal["lin", "chen", "audit", "nova"]
    password: str


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=8, max_length=2000)
    mode: Literal["live", "offline"] = "offline"


def create_app(data_dir: Path | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.getenv("AGENT_DATA_DIR", ROOT / "data"))
    store = Database(data_dir / "business.sqlite")
    engine = AgentEngine(store, data_dir / "checkpoints.sqlite")
    secret_path = data_dir / ".session-secret"
    if not secret_path.exists():
        secret_path.write_bytes(secrets.token_bytes(32))
    signing_key = secret_path.read_bytes()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
    busy: set[str] = set()
    lock = threading.Lock()

    # 崩溃后没有线程在运行，UI 必须提供明确的手动恢复动作。
    with store.connect() as db:
        db.execute("UPDATE runs SET status='paused' WHERE status='running'")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        pool.shutdown(wait=True)
        engine.connection.close()

    api = FastAPI(title="ProcureFlow Agent Demo", version="0.1.0", lifespan=lifespan)
    api.state.store, api.state.engine = store, engine

    @api.middleware("http")
    async def browser_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in ("POST", "PUT", "DELETE") and origin:
            if origin not in ("http://127.0.0.1:8765", "http://localhost:8765"):
                return Response("Cross-origin request denied", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def current_user(request: Request) -> dict:
        token = request.cookies.get("pf_session", "")
        try:
            body, signature = token.rsplit(".", 1)
            expected = hmac.new(signing_key, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError("invalid signature")
            session = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if session["expires"] < time.time():
                raise ValueError("expired")
            account = session["account"]
            return {"id": account, **ACCOUNTS[account]}
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, "请先选择演示身份登录") from None

    def accessible(run_id: str, user: dict) -> dict:
        try:
            run = store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        if run["tenant"] != user["tenant"] or (
            user["role"] == "employee" and run["owner"] != user["id"]
        ):
            raise HTTPException(404, "任务不存在")
        return run

    def schedule(run_id: str, initial: dict | None = None, decision: dict | None = None) -> None:
        with lock:
            if run_id in busy:
                raise HTTPException(409, "该任务正在执行，请勿重复操作")
            busy.add(run_id)

        def work():
            try:
                engine.execute(run_id, initial, decision)
            finally:
                with lock:
                    busy.discard(run_id)

        pool.submit(work)

    @api.get("/api/config")
    def config():
        return {
            "accounts": [{"id": k, **v} for k, v in ACCOUNTS.items()],
            "live_available": bool(os.getenv("DEEPSEEK_API_KEY")),
            "model": os.getenv("AGENT_MODEL", "deepseek-chat"),
            "demo": True,
        }

    @api.post("/api/login")
    def login(body: Login, response: Response):
        if not secrets.compare_digest(body.password, os.getenv("DEMO_PASSWORD", "demo-2026")):
            raise HTTPException(401, "演示口令错误")
        payload = {"account": body.account, "expires": int(time.time() + 8 * 3600)}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signature = hmac.new(signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        response.set_cookie(
            "pf_session",
            encoded + "." + signature,
            httponly=True,
            samesite="strict",
            max_age=8 * 3600,
        )
        return {"id": body.account, **ACCOUNTS[body.account]}

    @api.get("/api/me")
    def me(user=Depends(current_user)):
        return {**user, "budget": store.budget(user["tenant"])}

    @api.get("/api/runs")
    def list_runs(user=Depends(current_user)):
        with store.connect() as db:
            query, args = "SELECT * FROM runs WHERE tenant=?", [user["tenant"]]
            if user["role"] == "employee":
                query += " AND owner=?"
                args.append(user["id"])
            return [
                dict(r)
                for r in db.execute(query + " ORDER BY created DESC,rowid DESC LIMIT 80", args)
            ]

    @api.post("/api/runs", status_code=202)
    def create_run(body: RunRequest, user=Depends(current_user)):
        if user["role"] != "employee":
            raise HTTPException(403, "当前身份无申请权限")
        if body.mode == "live" and not os.getenv("DEEPSEEK_API_KEY"):
            raise HTTPException(400, "尚未配置模型 API，不能使用真实模式")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) FROM runs WHERE tenant=? AND status='running'", (user["tenant"],)
            ).fetchone()[0]
            if count >= 3:
                raise HTTPException(429, "同一组织最多同时运行三个任务")
            run_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO runs(id,tenant,owner,request,mode,status) "
                "VALUES (?,?,?,?,?,'running')",
                (run_id, user["tenant"], user["id"], body.request, body.mode),
            )
        store.event(run_id, "created", "采购需求已受理", {"mode": body.mode, "actor": user["name"]})
        initial = {
            "run_id": run_id,
            "tenant": user["tenant"],
            "mode": body.mode,
            "request": body.request,
            "messages": [{"role": "user", "content": body.request}],
            "rounds": 0,
        }
        schedule(run_id, initial)
        return {"id": run_id}

    @api.get("/api/runs/{run_id}")
    def get_run(run_id: str, user=Depends(current_user)):
        run = accessible(run_id, user)
        snapshot = engine.snapshot(run_id)
        with store.connect() as db:
            events = [
                dict(r)
                for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))
            ]
            checkpoints = len(list(engine.graph.get_state_history(engine.config(run_id))))
        for event in events:
            event["payload"] = json.loads(event["payload"])
        values = snapshot["values"]
        answer = next(
            (
                message.get("content")
                for message in reversed(values.get("messages", []))
                if message.get("role") == "assistant" and message.get("content")
            ),
            None,
        )
        return {
            **run,
            "answer": answer,
            "events": events,
            "proposal": values.get("proposal"),
            "order": values.get("order"),
            "decision": values.get("decision"),
            "next": snapshot["next"],
            "checkpoints": checkpoints,
            "checkpoint": snapshot["checkpoint"],
            "policies": store.search("采购 预算 价格 审计"),
        }

    @api.post("/api/runs/{run_id}/decision", status_code=202)
    def decide(run_id: str, body: Decision, user=Depends(current_user)):
        run = accessible(run_id, user)
        if user["role"] != "approver" or user["id"] == run["owner"]:
            raise HTTPException(403, "必须由独立采购审批人处理")
        if run["status"] != "awaiting_approval":
            raise HTTPException(409, "该任务不处于待审批状态")
        decision = {**body.model_dump(), "actor": user["id"]}
        with store.connect() as db:
            try:
                db.execute(
                    "INSERT INTO decisions(run_id,actor,action,comment) VALUES (?,?,?,?)",
                    (run_id, user["id"], body.action, body.comment),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "审批决定已经提交") from None
        store.event(run_id, "decision", "审批决定已记录", {**decision, "actor_name": user["name"]})
        schedule(run_id, decision=decision)
        return {"status": "accepted"}

    @api.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, user=Depends(current_user)):
        run = accessible(run_id, user)
        if user["id"] != run["owner"]:
            raise HTTPException(403, "只有申请人可恢复执行")
        if run["status"] not in ("failed", "paused"):
            raise HTTPException(409, "仅失败或服务重启暂停的任务可恢复")
        snapshot = engine.snapshot(run_id)
        with store.connect() as db:
            previous = db.execute("SELECT * FROM decisions WHERE run_id=?", (run_id,)).fetchone()
        decision = dict(previous) if previous and "approval" in snapshot["next"] else None
        if snapshot["values"]:
            initial = None
        else:
            initial = {
                "run_id": run_id,
                "tenant": run["tenant"],
                "mode": run["mode"],
                "request": run["request"],
                "messages": [{"role": "user", "content": run["request"]}],
                "rounds": 0,
            }
        store.event(
            run_id, "resume", "从已保存检查点恢复执行", {"checkpoint": snapshot["checkpoint"]}
        )
        schedule(run_id, initial, decision)
        return {"status": "accepted"}

    @api.get("/api/runs/{run_id}/events")
    async def event_stream(run_id: str, request: Request, user=Depends(current_user)):
        accessible(run_id, user)

        async def stream():
            after = 0
            while not await request.is_disconnected():
                with store.connect() as db:
                    rows = db.execute(
                        "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after)
                    ).fetchall()
                for row in rows:
                    item = dict(row)
                    after = item["id"]
                    yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                if store.get_run(run_id)["status"] not in ("running",):
                    break
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.get("/api/runs/{run_id}/report")
    def report(run_id: str, user=Depends(current_user)):
        run = get_run(run_id, user)
        proposal = run["proposal"]
        lines = [
            "# ProcureFlow 采购申请报告",
            "",
            "演示数据；订单仅本地登记，不产生真实交易。",
            "",
            f"任务：{run_id}",
            f"模式：{run['mode']}",
            f"状态：{run['status']}",
            f"需求：{run['request']}",
            "",
        ]
        if proposal:
            lines += [
                f"商品：{proposal['item']['name']} / {proposal['sku']}",
                f"数量：{proposal['quantity']}",
                f"单价：{proposal['item']['price']} 元",
                f"总额：{proposal['total']} 元",
                f"选型理由：{proposal['rationale']}",
                "",
                "## 制度依据",
            ]
            lines += [f"- [{x['id']}] {x['title']}：{x['content']}" for x in run["policies"]]
        lines += ["", "## 审计事件", *[f"- {e['created']} {e['title']}" for e in run["events"]]]
        return Response(
            "\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="procurement-{run_id[:8]}.md"'},
        )

    api.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @api.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @api.get("/lab", include_in_schema=False)
    def lab():
        return FileResponse(ROOT / "static" / "lab.html")

    @api.get("/api/lab/reports", include_in_schema=False)
    def lab_reports():
        reports = {}
        for mode, directory in (("offline", "lab"), ("live", "lab-live")):
            path = ROOT / "artifacts" / directory / "summary.json"
            if path.is_file():
                reports[mode] = json.loads(path.read_text(encoding="utf-8"))
        policy_path = ROOT / "artifacts" / "lab" / "policy-learning.json"
        if "offline" in reports and policy_path.is_file():
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            reports["offline"]["policy_learning"] = policy["holdout"]
        return reports

    @api.get("/api/lab/trajectories", include_in_schema=False)
    def lab_trajectories():
        path = ROOT / "artifacts" / "lab" / "trajectories.jsonl"
        if not path.is_file():
            raise HTTPException(404, "请先执行 run_lab.py 生成实验轨迹")
        return FileResponse(
            path, filename="procureflow-lab-trajectories.jsonl", media_type="application/x-ndjson"
        )

    return api


app = create_app()
