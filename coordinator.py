"""Coordinator: the local hub every wrapper talks to (implementation.md sec 3).

Phase 2 scope: one backend worker. FastAPI on 127.0.0.1, SQLite state, a WS per
role, three REST endpoints. No Head, no Reviewer, no dashboard HTML yet.

Run:  python coordinator.py
Env:  HARNESS_CONFIG=path/to/project_config.json  (default: ./project_config.json)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from harness_db import init_db, iso as _iso, usage_summary

# --------------------------------------------------------------------------- config

CONFIG_PATH = Path(os.environ.get("HARNESS_CONFIG", "project_config.json")).resolve()
CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
_BASE = (CONFIG_PATH.parent / CFG.get("repo_path", ".")).resolve()
DB_PATH = CONFIG_PATH.parent / "harness_state.db"


def role_cfg(role: str) -> dict:
    try:
        return CFG["roles"][role]
    except KeyError:
        raise HTTPException(404, f"role {role!r} not in config")


def resolve_working_dir(role: str) -> str:
    return str((_BASE / role_cfg(role).get("working_dir", ".")).resolve())


# --------------------------------------------------------------------------- db

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
LOCK = threading.Lock()  # ponytail: one global lock, fine for <10 wrappers
init_db(DB)


def log_event(terminal: str | None, event_type: str, payload: dict) -> None:
    with LOCK:
        DB.execute(
            "INSERT INTO event_log(ts, terminal, event_type, payload) VALUES (?,?,?,?)",
            (time.time(), terminal, event_type, json.dumps(payload, default=str)),
        )
        DB.commit()


def upsert_terminal(role, status, *, session_id=None, task_id=None) -> None:
    with LOCK:
        row = DB.execute("SELECT * FROM terminals WHERE role=?", (role,)).fetchone()
        sid = session_id if session_id is not None else (row["current_session_id"] if row else None)
        tid = task_id if task_id is not None else (row["current_task_id"] if row else None)
        DB.execute(
            "INSERT INTO terminals(role,status,current_session_id,current_task_id,last_updated) "
            "VALUES (?,?,?,?,?) ON CONFLICT(role) DO UPDATE SET "
            "status=excluded.status, current_session_id=excluded.current_session_id, "
            "current_task_id=excluded.current_task_id, last_updated=excluded.last_updated",
            (role, status, sid, tid, time.time()),
        )
        DB.commit()


def create_task(task_id, role, instructions) -> None:
    with LOCK:
        DB.execute(
            "INSERT INTO tasks(id,role,instruction_text,status,created_at) VALUES (?,?,?,?,?)",
            (task_id, role, instructions, "dispatched", time.time()),
        )
        DB.commit()


def set_task_status(task_id, status, *, completed=False) -> None:
    with LOCK:
        DB.execute(
            "UPDATE tasks SET status=?, completed_at=? WHERE id=?",
            (status, time.time() if completed else None, task_id),
        )
        DB.commit()


def record_usage(role, cost_usd, duration_ms, call_type) -> None:
    with LOCK:
        DB.execute(
            "INSERT INTO usage_ledger(terminal_role,ts,cost_usd,duration_ms,call_type) VALUES (?,?,?,?,?)",
            (role, time.time(), cost_usd, duration_ms, call_type),
        )
        DB.commit()


def meta_set(key: str, value) -> None:
    with LOCK:
        DB.execute(
            "INSERT INTO meta(key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, default=str), time.time()),
        )
        DB.commit()


def meta_get(key: str, default=None):
    row = DB.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


# --------------------------------------------------------------------------- app

app = FastAPI(title="harness-coordinator")
CONNS: dict[str, WebSocket] = {}


class DispatchBody(BaseModel):
    role: str
    instructions: str
    task_id: str | None = None


class SummaryBody(BaseModel):
    summary: str


class ConfigPathBody(BaseModel):
    config_path: str


class SayBody(BaseModel):
    text: str


async def push_to_head(obj: dict) -> bool:
    """Deliver an event/say frame to the Head wrapper, if one is connected."""
    ws = CONNS.get("head")
    if ws is None:
        return False
    await ws.send_json(obj)
    return True


async def _send_worker_frame(role: str, task_id: str, instructions: str, *, followup: bool) -> bool:
    """Build + send a dispatch/followup frame to a worker WS. Returns False (and
    marks the task blocked) if that worker is not connected."""
    ws = CONNS.get(role)
    if ws is None or role not in CFG.get("roles", {}):
        set_task_status(task_id, "blocked")
        log_event(role, "dispatch_failed", {"task_id": task_id, "reason": "worker not connected"})
        return False
    rc = CFG["roles"][role]
    frame = {"task_id": task_id, "instructions": instructions,
             "working_dir": resolve_working_dir(role), "engine": rc.get("engine", "claude")}
    if followup:
        row = DB.execute("SELECT current_session_id FROM terminals WHERE role=?", (role,)).fetchone()
        frame["type"] = "followup"
        frame["session_id"] = row["current_session_id"] if row else None
    else:
        frame["type"] = "dispatch"
    await ws.send_json(frame)
    log_event(role, "followup_dispatch" if followup else "dispatch", frame)
    return True


async def dispatch_review(task_id: str, worker_role: str, worker_session_id: str | None) -> None:
    """The hard rule (impl.md sec 3): every worker-done triggers a review. Not a
    Head decision."""
    ws = CONNS.get("reviewer")
    task = DB.execute("SELECT instruction_text FROM tasks WHERE id=?", (task_id,)).fetchone()
    rc = CFG.get("roles", {}).get("reviewer", {})
    frame = {"type": "review", "task_id": task_id, "worker_role": worker_role,
             "worker_session_id": worker_session_id,
             "instructions": task["instruction_text"] if task else "",
             "working_dir": resolve_working_dir(worker_role),
             "test_cmd": CFG.get("test_cmd", "python -m unittest"),
             "engine": rc.get("engine", "agy"), "model": rc.get("model")}
    await ws.send_json(frame)
    log_event("reviewer", "review_dispatch", frame)


def _reviewer_available() -> bool:
    return "reviewer" in CONNS and "reviewer" in CFG.get("roles", {})


async def on_worker_done(task_id: str | None, result: dict) -> None:
    """A worker finished. Auto-dispatch the Reviewer (impl.md sec 3); only fall
    through to Head directly when no reviewer is running."""
    role = result.get("role")
    log_event(role, "task_done", {"task_id": task_id, "session_id": result.get("session_id")})

    if task_id and role and role not in ("head", "reviewer") and _reviewer_available():
        set_task_status(task_id, "in_review")
        await dispatch_review(task_id, role, result.get("session_id"))
        return

    if task_id:
        set_task_status(task_id, "done", completed=True)
    if role and role != "head":
        await push_to_head({"type": "event", "event": "worker_task_done",
                            "task_id": task_id, "role": role, "outcome": "done"})


async def on_review_result(frame: dict) -> None:
    """Route a reviewer verdict (impl.md sec 3): pass -> done + wake Head;
    fail -> feedback straight back into the same worker session, loop until pass
    or the cycle cap; error -> tell Head (phase 5 adds the router re-route here)."""
    task_id = frame.get("task_id")
    verdict = frame.get("verdict")
    feedback = frame.get("feedback") or ""
    row = DB.execute("SELECT role, review_cycles FROM tasks WHERE id=?", (task_id,)).fetchone()
    worker_role = row["role"] if row else None
    cycles = row["review_cycles"] if row else 0

    with LOCK:
        DB.execute("UPDATE tasks SET review_verdict=?, review_feedback=? WHERE id=?",
                   (verdict, feedback or frame.get("detail"), task_id))
        DB.commit()
    log_event("reviewer", "review_result", {"task_id": task_id, "verdict": verdict})

    if verdict == "pass":
        set_task_status(task_id, "done", completed=True)
        await push_to_head({"type": "event", "event": "worker_task_reviewed",
                            "task_id": task_id, "role": worker_role, "verdict": "pass"})
        return

    if verdict == "fail":
        max_cycles = CFG.get("max_review_cycles", 3)
        with LOCK:
            DB.execute("UPDATE tasks SET review_cycles=review_cycles+1 WHERE id=?", (task_id,))
            DB.commit()
        if cycles + 1 >= max_cycles:
            set_task_status(task_id, "review_stuck")
            log_event("reviewer", "review_stuck", {"task_id": task_id, "cycles": cycles + 1})
            await push_to_head({"type": "event", "event": "review_stuck", "task_id": task_id,
                                "role": worker_role, "feedback": feedback})
        else:
            set_task_status(task_id, "revising")
            log_event("reviewer", "review_fail_routed", {"task_id": task_id, "cycle": cycles + 1})
            await _send_worker_frame(
                worker_role, task_id,
                "Reviewer did not pass this. Address the feedback below, then report done.\n\n" + feedback,
                followup=True)
        return

    # verdict == "error" (agy failed / unparseable / rate-limited)
    set_task_status(task_id, "review_error")
    # PHASE 5 SEAM: re-route this review to the $550 router (Opus 5 / GPT-5.6 sol) here.
    await push_to_head({"type": "event", "event": "review_error", "task_id": task_id,
                        "role": worker_role, "detail": frame.get("detail"),
                        "rate_limited": frame.get("rate_limited", False)})


async def handle_frame(role: str, frame: dict) -> None:
    kind = frame.get("type")
    if kind == "status":
        upsert_terminal(role, frame["status"], task_id=frame.get("task_id"))
        log_event(role, "status", frame)
    elif kind == "progress":
        log_event(role, "progress", frame)
    elif kind == "result":
        log_event(role, "result", frame)
        record_usage(role, frame.get("total_cost_usd"), frame.get("duration_ms"),
                     frame.get("call_type", "dispatch"))
        if role == "reviewer":
            await on_review_result(frame)
        else:
            if frame.get("session_id"):
                row = DB.execute("SELECT status FROM terminals WHERE role=?", (role,)).fetchone()
                upsert_terminal(role, row["status"] if row else "idle",
                                session_id=frame["session_id"], task_id=frame.get("task_id"))
            task_id, outcome = frame.get("task_id"), frame.get("outcome")
            if outcome == "done":
                await on_worker_done(task_id, frame)
            elif outcome and task_id:
                set_task_status(task_id, outcome, completed=True)
    else:
        log_event(role, "unknown_frame", frame)
    print(f"[coordinator] {role} -> {kind} {frame.get('status') or frame.get('outcome') or frame.get('event') or ''}",
          flush=True)


@app.websocket("/ws/{role}")
async def ws_endpoint(websocket: WebSocket, role: str):
    await websocket.accept()
    CONNS[role] = websocket
    log_event(role, "connected", {})
    print(f"[coordinator] {role} connected", flush=True)
    try:
        while True:
            frame = await websocket.receive_json()
            await handle_frame(role, frame)
    except WebSocketDisconnect:
        pass
    finally:
        if CONNS.get(role) is websocket:
            del CONNS[role]
        log_event(role, "disconnected", {})
        print(f"[coordinator] {role} disconnected", flush=True)


@app.post("/dispatch")
async def dispatch(body: DispatchBody):
    role = body.role
    if role not in CONNS:
        raise HTTPException(503, f"no worker connected for role {role!r}")

    if body.task_id:  # follow-up into the same session (--resume path)
        row = DB.execute("SELECT current_session_id FROM terminals WHERE role=?", (role,)).fetchone()
        if not (row and row["current_session_id"]):
            raise HTTPException(409, f"no stored session for role {role!r}; cannot follow up")
        set_task_status(body.task_id, "dispatched")
        await _send_worker_frame(role, body.task_id, body.instructions, followup=True)
        return {"task_id": body.task_id}

    task_id = f"t-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    create_task(task_id, role, body.instructions)
    await _send_worker_frame(role, task_id, body.instructions, followup=False)
    return {"task_id": task_id}


@app.get("/state")
def state():
    terms = [dict(r) for r in DB.execute("SELECT * FROM terminals").fetchall()]
    for t in terms:
        t["last_updated_iso"] = _iso(t["last_updated"])
    tasks = [dict(r) for r in DB.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 50").fetchall()]
    for t in tasks:
        t["created_at_iso"] = _iso(t["created_at"])
        t["completed_at_iso"] = _iso(t["completed_at"])
    return {"terminals": terms, "tasks": tasks, "workers_connected": sorted(CONNS),
            "head_connected": "head" in CONNS,
            "ready_to_commit": meta_get("ready_to_commit")}


@app.get("/usage")
def usage():
    return usage_summary(DB, CFG.get("usage_limits", {}))


@app.get("/tasks/{task_id}")
def task_detail(task_id: str):
    row = DB.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"task {task_id!r} not found")
    d = dict(row)
    d["created_at_iso"] = _iso(d["created_at"])
    d["completed_at_iso"] = _iso(d["completed_at"])
    return d


@app.post("/flag_ready")
def flag_ready(body: SummaryBody):
    entry = {"summary": body.summary, "ts": time.time()}
    meta_set("ready_to_commit", entry)
    log_event("head", "ready_to_commit", entry)
    print(f"[coordinator] READY TO COMMIT: {body.summary}", flush=True)
    return {"ok": True, **entry}


@app.post("/active_project")
def active_project(body: ConfigPathBody):
    global CFG, _BASE
    meta_set("active_project", body.config_path)
    p = Path(body.config_path).resolve()
    if not p.is_file():
        return {"applied": False, "note": f"recorded, but {p} is not a file; not reloaded"}
    with LOCK:
        CFG = json.loads(p.read_text(encoding="utf-8"))
        _BASE = (p.parent / CFG.get("repo_path", ".")).resolve()
    log_event("head", "active_project", {"config_path": str(p)})
    return {"applied": True, "note": "config reloaded in coordinator; connected workers apply on reconnect"}


@app.post("/head_say")
async def head_say(body: SayBody):
    if not await push_to_head({"type": "say", "text": body.text}):
        raise HTTPException(503, "no head connected")
    log_event("head", "say", {"text": body.text})
    return {"ok": True}


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):  # engine/frame text is UTF-8; Windows console is cp1252
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    host = CFG.get("coordinator", {}).get("host", "127.0.0.1")
    port = CFG.get("coordinator", {}).get("port", 8765)
    print(f"[coordinator] config={CONFIG_PATH}  db={DB_PATH}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
