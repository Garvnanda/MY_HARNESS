"""Head's MCP tool server (implementation.md sec 2). Local stdio server loaded by
`claude --mcp-config harness_tools.json`. Every tool is a thin HTTP call to the
coordinator's REST layer.

Env: HARNESS_COORDINATOR  (default http://127.0.0.1:8765)
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("HARNESS_COORDINATOR", "http://127.0.0.1:8765").rstrip("/")
mcp = FastMCP("harness")


def _get(path: str):
    r = httpx.get(f"{BASE}{path}", timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict):
    r = httpx.post(f"{BASE}{path}", json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def _safe(fn):
    try:
        return fn()
    except httpx.HTTPStatusError as e:
        return f"coordinator error {e.response.status_code}: {e.response.text[:300]}"
    except httpx.RequestError as e:
        return f"cannot reach coordinator at {BASE}: {e!r}"


@mcp.tool()
def dispatch_task(role: str, instructions: str) -> str:
    """Send a sub-task to a worker terminal (e.g. role="backend"). Returns the task id."""
    def go():
        out = _post("/dispatch", {"role": role, "instructions": instructions})
        return f"dispatched to {role}: task_id={out['task_id']}"
    return _safe(go)


@mcp.tool()
def check_worker_status(role: str) -> str:
    """Current status of a worker terminal plus its most recent task summary."""
    def go():
        st = _get("/state")
        term = next((t for t in st["terminals"] if t["role"] == role), None)
        tasks = [t for t in st["tasks"] if t["role"] == role]
        latest = tasks[0] if tasks else None
        summary = {
            "role": role,
            "status": term["status"] if term else "not connected",
            "current_session_id": term["current_session_id"] if term else None,
            "latest_task": None if latest is None else {
                "id": latest["id"], "status": latest["status"],
                "instruction": latest["instruction_text"][:200],
                "review_verdict": latest["review_verdict"],
            },
        }
        return json.dumps(summary, indent=2)
    return _safe(go)


@mcp.tool()
def get_reviewer_feedback(task_id: str) -> str:
    """Latest reviewer verdict + feedback for a task."""
    def go():
        t = _get(f"/tasks/{task_id}")
        v = t.get("review_verdict")
        if not v:
            return f"task {task_id}: no reviewer verdict yet. task status={t['status']}"
        return json.dumps({
            "task_id": task_id, "status": t["status"], "review_verdict": v,
            "review_cycles": t.get("review_cycles"),
            "review_feedback": t.get("review_feedback"),
        }, indent=2)
    return _safe(go)


@mcp.tool()
def flag_ready_to_commit(summary: str) -> str:
    """Mark the run ready for Garv to commit by hand. Sets the dashboard flag and
    is also surfaced in this chat."""
    def go():
        _post("/flag_ready", {"summary": summary})
        return f"flagged ready to commit: {summary}"
    return _safe(go)


@mcp.tool()
def set_active_project(config_path: str) -> str:
    """Point the harness at a different project_config.json."""
    def go():
        out = _post("/active_project", {"config_path": config_path})
        return f"{out.get('note', out)}"
    return _safe(go)


if __name__ == "__main__":
    mcp.run()
