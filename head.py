"""Head wrapper (implementation.md sec 2). The one terminal Garv talks to.

Feels like a chat: a line Garv types (or a line/event the coordinator injects on
his behalf) becomes one `claude -p --resume <head_session> --mcp-config
harness_tools.json` turn, whose reply is printed here. One continuous session,
mediated by this script.

Run:  python head.py [--config project_config.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

from worker import _STDOUT_LINE_LIMIT, _send, spawn_argv


# --------------------------------------------------------------------------- pure helpers (unit-tested)

def build_head_cmd(instructions: str, session_id: str | None, mcp_config: str) -> list[str]:
    argv = ["claude"]
    if session_id:
        argv += ["--resume", session_id]
    argv += ["-p", instructions,
             "--mcp-config", mcp_config,
             "--permission-mode", "bypassPermissions",
             # Head delegates through tools/workers; it does not edit code itself.
             "--disallowedTools", "Edit", "Write", "NotebookEdit",
             "--output-format", "stream-json", "--verbose"]
    return argv


def event_to_prompt(frame: dict) -> str:
    ev = frame.get("event")
    role, task_id = frame.get("role"), frame.get("task_id")
    if ev == "worker_task_done":  # only fires when no Reviewer is running
        return (f"[harness event] {role} finished task {task_id} (outcome "
                f"{frame.get('outcome')}). Decide the next step: dispatch a follow-up "
                f"sub-task, or if the work is complete call flag_ready_to_commit with a "
                f"short summary.")
    if ev == "worker_task_reviewed":
        return (f"[harness event] {role} task {task_id} PASSED review. Decide the next "
                f"step: dispatch more sub-tasks, or call flag_ready_to_commit with a "
                f"short summary.")
    if ev == "review_stuck":
        return (f"[harness event] {role} task {task_id} FAILED review repeatedly and is "
                f"stuck. Reviewer feedback:\n{frame.get('feedback')}\nDecide: redirect the "
                f"worker with clearer instructions, cut the scope, or tell Garv.")
    if ev == "review_error":
        return (f"[harness event] the review of {role} task {task_id} could not run "
                f"({frame.get('detail')}) and the router fallback did not resolve it. "
                f"Tell Garv.")
    if ev == "docs_updated":
        src = " (via router fallback)" if frame.get("source") == "router" else ""
        return (f"[harness event] Docs updated {frame.get('files')}{src} for task "
                f"{task_id}. Decide the next step, or call flag_ready_to_commit.")
    if ev == "docs_error":
        return (f"[harness event] Docs could not update for task {task_id} "
                f"({frame.get('detail')}). Tell Garv.")
    return f"[harness event] {json.dumps(frame, default=str)}"


def head_reply_text(lines: list[dict]) -> str:
    """Head's assistant text plus a one-liner per tool call, from stream-json objects."""
    out: list[str] = []
    for obj in lines:
        if obj.get("type") != "assistant":
            continue
        for b in obj.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(b["text"].rstrip())
            elif b.get("type") == "tool_use":
                inp = b.get("input", {}) or {}
                hint = inp.get("role") or inp.get("summary") or inp.get("task_id") or ""
                out.append(f"  [Head -> {b.get('name', '?')} {hint}]".rstrip())
    return "\n".join(out)


# --------------------------------------------------------------------------- runtime

async def run_head_turn(state: dict, ws, text: str) -> None:
    await _send(ws, {"type": "status", "status": "working"})
    argv = spawn_argv(build_head_cmd(text, state["session_id"], state["mcp_config"]))
    print(f"\n[head] >>> {text}\n", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=state["cwd"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STDOUT_LINE_LIMIT,
    )
    lines: list[dict] = []
    result_obj: dict | None = None
    try:
        async for raw in proc.stdout:
            s = raw.decode("utf-8", "replace").rstrip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except ValueError:
                continue
            lines.append(obj)
            if obj.get("type") == "result":
                result_obj = obj
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
    print(head_reply_text(lines) or "(no text reply)", flush=True)
    if stderr_text.strip():
        print(f"[head] stderr:\n{stderr_text}", flush=True)

    cost = result_obj.get("total_cost_usd") if result_obj else None
    if result_obj:
        if result_obj.get("session_id"):
            state["session_id"] = result_obj["session_id"]
        await _send(ws, {"type": "result", "role": "head", "call_type": "head_turn",
                         "session_id": state["session_id"],
                         "total_cost_usd": cost,
                         "duration_ms": result_obj.get("duration_ms")})
    await _send(ws, {"type": "status", "status": "idle"})
    print(f"\n[head] <<< turn done (session={state['session_id']}, cost=${cost})\n", flush=True)


async def _stdin_producer(queue: asyncio.Queue) -> None:
    # Proactor loop (Windows) can't connect_read_pipe on console stdin -> read in a thread.
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            print("[head] stdin closed; driven by coordinator only", flush=True)
            return
        text = line.strip()
        if text:
            await queue.put(text)


async def _ws_producer(queue: asyncio.Queue, ws) -> None:
    async for raw in ws:
        frame = json.loads(raw)
        if frame.get("type") == "say":
            await queue.put(frame["text"])
        elif frame.get("type") == "event":
            await queue.put(event_to_prompt(frame))


async def _consumer(queue: asyncio.Queue, state: dict, ws) -> None:
    while True:
        text = await queue.get()
        try:
            await run_head_turn(state, ws, text)
        except Exception as exc:  # noqa: BLE001 - report, keep the loop alive
            print(f"[head] turn error: {exc!r}", flush=True)
        finally:
            queue.task_done()


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="project_config.json")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    rc = cfg["roles"]["head"]
    mcp_config = str((cfg_path.parent / rc["mcp_config"]).resolve())
    cwd = str((cfg_path.parent / cfg.get("repo_path", ".")).resolve())
    co = cfg.get("coordinator", {})
    uri = f"ws://{co.get('host', '127.0.0.1')}:{co.get('port', 8765)}/ws/head"

    state = {"session_id": None, "mcp_config": mcp_config, "cwd": cwd}
    print(f"[head] mcp_config={mcp_config}\n[head] cwd={cwd}\n[head] connecting {uri}", flush=True)

    async with websockets.connect(uri, max_size=_STDOUT_LINE_LIMIT) as ws:
        await _send(ws, {"type": "status", "status": "idle"})
        print("[head] connected. Type a line + enter to talk to Head.", flush=True)
        queue: asyncio.Queue = asyncio.Queue()
        await asyncio.gather(
            _stdin_producer(queue),
            _ws_producer(queue, ws),
            _consumer(queue, state, ws),
        )


if __name__ == "__main__":
    asyncio.run(main())
