"""Docs wrapper (idea.md sec Docs, implementation.md sec 1). Repo-local markdown
updater on the GitHub Copilot CLI. Head-dispatched only -- it never triggers the
Reviewer (it edits docs, not code).

Run:  python docs.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

import websockets

from worker import _STDOUT_LINE_LIMIT, _send, spawn_argv

_RATE_RE = re.compile(r"rate.?limit|quota|billing|insufficient|\b402\b|\b429\b|exhausted", re.I)


# --------------------------------------------------------------------------- pure helpers (unit-tested)

def build_docs_cmd(engine: str, instructions: str, model: str | None, usage_file: str) -> list[str]:
    if engine != "copilot":
        raise ValueError(f"docs engine {engine!r} not supported (expected 'copilot')")
    argv = ["copilot", "-p", instructions, "--no-ask-user", "--allow-all-tools",
            "--output-format", "json", "--usage-output-file", usage_file]
    if model:
        argv += ["--model", model]
    return argv


def parse_copilot_stream(objs: list[dict], returncode: int, stderr_text: str,
                         usage_json: dict | None) -> dict:
    """Fold copilot's JSONL + its --usage-output-file summary into one result."""
    result_obj = next((o for o in reversed(objs) if o.get("type") == "result"), None)
    texts = [o.get("data", {}).get("content", "") for o in objs
             if o.get("type") == "assistant.message" and o.get("data", {}).get("content")]
    reply = "\n".join(t for t in texts if t)

    usage = usage_json or {}
    ru = (result_obj or {}).get("usage", {})
    cc = usage.get("codeChanges") or ru.get("codeChanges") or {}
    files_modified = cc.get("filesModified") or []
    premium = usage.get("totalPremiumRequestCost", ru.get("premiumRequests"))
    duration_ms = usage.get("totalApiDurationMs") or ru.get("sessionDurationMs")

    haystack = reply + "\n" + stderr_text + "\n" + json.dumps(
        [o for o in objs if "error" in o.get("type", "")], default=str)
    rate_limited = bool(_RATE_RE.search(haystack))

    exit_code = (result_obj or {}).get("exitCode", returncode)
    if result_obj is None or exit_code != 0 or rate_limited:
        detail = (reply[-500:] or stderr_text.strip()[-500:]
                  or f"exit {exit_code}, no copilot result line")
        return {"outcome": "error", "reply": reply, "files_modified": files_modified,
                "premium_requests": premium, "duration_ms": duration_ms,
                "rate_limited": rate_limited, "detail": detail,
                "session_id": (result_obj or {}).get("sessionId")}

    return {"outcome": "done", "reply": reply, "files_modified": files_modified,
            "premium_requests": premium, "duration_ms": duration_ms,
            "rate_limited": False, "detail": None,
            "session_id": result_obj.get("sessionId")}


# --------------------------------------------------------------------------- runtime

async def run_docs(ws, frame: dict) -> None:
    task_id = frame["task_id"]
    wd = frame["working_dir"]
    usage_file = str(Path(tempfile.gettempdir()) / f"copilot_usage_{task_id}.json")
    argv = spawn_argv(build_docs_cmd(frame.get("engine", "copilot"),
                                     frame["instructions"], frame.get("model"), usage_file))

    await _send(ws, {"type": "status", "status": "working", "task_id": task_id})
    print(f"\n[docs] task {task_id} in {wd}\n[docs] $ copilot -p <instructions> "
          f"--model {frame.get('model')}\n", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=wd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STDOUT_LINE_LIMIT,
    )
    objs: list[dict] = []
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            print(line, flush=True)  # Garv watches
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            objs.append(obj)
            # copilot's JSONL is very chatty; only forward turn-level milestones
            # so the coordinator's event_log stays readable.
            if obj.get("type") in ("assistant.message", "model.call_start"):
                await _send(ws, {"type": "progress", "task_id": task_id, "event": "assistant"})
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
    if stderr_text.strip():
        print(f"[docs] stderr:\n{stderr_text}", flush=True)

    usage_json = None
    try:
        usage_json = json.loads(Path(usage_file).read_text(encoding="utf-8"))
        Path(usage_file).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass

    parsed = parse_copilot_stream(objs, proc.returncode, stderr_text, usage_json)
    await _send(ws, {"type": "result", "role": "docs", "call_type": "docs", "task_id": task_id,
                     "outcome": parsed["outcome"], "session_id": parsed["session_id"],
                     "premium_requests": parsed["premium_requests"],
                     "duration_ms": parsed["duration_ms"],
                     "files_modified": parsed["files_modified"],
                     "rate_limited": parsed["rate_limited"], "detail": parsed["detail"]})
    await _send(ws, {"type": "status", "status": "idle", "task_id": task_id})
    print(f"\n[docs] {task_id}: {parsed['outcome']} "
          f"(files={parsed['files_modified']}, credits={parsed['premium_requests']})\n", flush=True)


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="project_config.json")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).resolve().read_text(encoding="utf-8"))
    co = cfg.get("coordinator", {})
    uri = f"ws://{co.get('host', '127.0.0.1')}:{co.get('port', 8765)}/ws/docs"
    print(f"[docs] connecting {uri}", flush=True)

    async with websockets.connect(uri, max_size=_STDOUT_LINE_LIMIT) as ws:
        await _send(ws, {"type": "status", "status": "idle"})
        print("[docs] connected, idle", flush=True)
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") in ("dispatch", "followup"):
                try:
                    await run_docs(ws, frame)
                except Exception as exc:  # noqa: BLE001
                    await _send(ws, {"type": "result", "role": "docs", "call_type": "docs",
                                     "task_id": frame.get("task_id"), "outcome": "error",
                                     "detail": repr(exc), "rate_limited": False,
                                     "files_modified": [], "premium_requests": None,
                                     "duration_ms": None, "session_id": None})
                    await _send(ws, {"type": "status", "status": "idle"})
                    print(f"[docs] ERROR: {exc!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
