"""Worker wrapper (implementation.md sec 1). Owns one engine CLI as a subprocess,
driven by the coordinator over a WebSocket. Garv watches this terminal scroll;
he never types into it.

Run:  python worker.py backend [--config project_config.json]

Engine invocation is the phase-1 spike's proven form:
    claude -p "<instr>" --output-format stream-json --verbose --permission-mode bypassPermissions
    claude --resume <session_id> -p "<instr>" ...        (follow-ups)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

import websockets

FAKE_ENGINE = Path(__file__).with_name("tools") / "fake_engine.py"
_RATE_LIMIT_RE = re.compile(r"rate.?limit|usage limit|quota exceeded|overloaded|\b429\b", re.I)
_STDOUT_LINE_LIMIT = 8 * 1024 * 1024  # stream-json result lines can be large


# --------------------------------------------------------------------------- pure helpers (unit-tested)

def build_engine_cmd(engine: str, instructions: str, session_id: str | None = None) -> list[str]:
    if engine == "claude":
        argv = ["claude"]
        if session_id:
            argv += ["--resume", session_id]
        argv += ["-p", instructions, "--output-format", "stream-json",
                 "--verbose", "--permission-mode", "bypassPermissions"]
        return argv
    if engine == "fake":
        argv = [sys.executable, str(FAKE_ENGINE)]
        if session_id:
            argv += ["--resume", session_id]
        argv += [instructions]
        return argv
    raise ValueError(f"unknown engine: {engine!r}")


def spawn_argv(argv: list[str]) -> list[str]:
    """Resolve argv[0] to its full path on PATH. On Windows, Python's subprocess
    runs a resolved .CMD/.EXE directly; do NOT wrap .cmd in `cmd.exe /c` -- that
    re-breaks on paths with spaces."""
    return [shutil.which(argv[0]) or argv[0], *argv[1:]]


def parse_result_line(result_obj: dict | None, returncode: int, stderr_text: str) -> dict:
    """Turn the engine's final `result` event into an outcome the coordinator can trust.

    Spike finding: a top-level subtype=="success" is NOT proof the task ran.
    permission_denials / is_error / a missing result line all mean it did not.
    """
    rate_limited = bool(stderr_text and _RATE_LIMIT_RE.search(stderr_text))
    if result_obj is None:
        return {"outcome": "error", "session_id": None, "subtype": None, "is_error": True,
                "num_turns": None, "total_cost_usd": None, "duration_ms": None,
                "permission_denials": [], "rate_limited": rate_limited,
                "error_detail": (stderr_text or "").strip()[-500:] or f"exit {returncode}, no result line"}

    if _RATE_LIMIT_RE.search(json.dumps(result_obj, default=str)):
        rate_limited = True
    denials = result_obj.get("permission_denials") or []
    is_error = bool(result_obj.get("is_error"))
    subtype = result_obj.get("subtype")

    if denials or is_error:
        outcome = "blocked"
    elif rate_limited:
        outcome = "blocked"
    elif returncode != 0 or subtype != "success":
        outcome = "error"
    else:
        outcome = "done"

    return {"outcome": outcome, "session_id": result_obj.get("session_id"), "subtype": subtype,
            "is_error": is_error, "num_turns": result_obj.get("num_turns"),
            "total_cost_usd": result_obj.get("total_cost_usd"),
            "duration_ms": result_obj.get("duration_ms"),
            "permission_denials": denials, "rate_limited": rate_limited}


def _progress_frames(obj: dict, task_id: str):
    """Compact progress events from a stream-json line. Names + error flags only —
    raw tool i/o stays in this terminal's scroll, not on the wire."""
    kind = obj.get("type")
    if kind == "assistant":
        for b in obj.get("message", {}).get("content", []):
            if b.get("type") == "tool_use":
                yield {"type": "progress", "task_id": task_id, "event": "tool_use", "name": b.get("name")}
    elif kind == "user":
        for b in obj.get("message", {}).get("content", []):
            if b.get("type") == "tool_result":
                yield {"type": "progress", "task_id": task_id, "event": "tool_result",
                       "is_error": bool(b.get("is_error"))}


# --------------------------------------------------------------------------- runtime

async def _send(ws, obj: dict) -> None:
    await ws.send(json.dumps(obj))


async def run_task(ws, role: str, engine: str, working_dir: str, frame: dict) -> None:
    task_id = frame["task_id"]
    call_type = "followup" if frame["type"] == "followup" else "dispatch"
    session_id = frame.get("session_id") if call_type == "followup" else None

    await _send(ws, {"type": "status", "status": "working", "task_id": task_id})
    argv = spawn_argv(build_engine_cmd(engine, frame["instructions"], session_id))
    print(f"\n[worker:{role}] $ {' '.join(argv)}\n  cwd={working_dir}\n", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=working_dir,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STDOUT_LINE_LIMIT,
    )

    result_obj: dict | None = None
    try:
        async for raw in proc.stdout:
            text = raw.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            print(text, flush=True)  # Garv watches the live scroll (idea.md sec 1)
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if obj.get("type") == "result":
                result_obj = obj
            else:
                for pf in _progress_frames(obj, task_id):
                    await _send(ws, pf)
        await proc.wait()
    finally:
        if proc.returncode is None:  # crash mid-stream -> don't orphan the engine
            proc.kill()
            await proc.wait()

    stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
    if stderr_text.strip():
        print(f"[worker:{role}] stderr:\n{stderr_text}", flush=True)

    parsed = parse_result_line(result_obj, proc.returncode, stderr_text)
    await _send(ws, {"type": "result", "task_id": task_id, "role": role,
                     "call_type": call_type, **parsed})

    if parsed["rate_limited"]:
        status = "paused-quota"
    elif parsed["outcome"] == "done":
        status = "idle"
    else:
        status = "blocked"
    await _send(ws, {"type": "status", "status": status, "task_id": task_id})
    print(f"\n[worker:{role}] task {task_id}: {parsed['outcome']} "
          f"(session={parsed['session_id']}, cost=${parsed['total_cost_usd']})\n", flush=True)


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows console is cp1252; engine output is UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("role")
    ap.add_argument("--config", default="project_config.json")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    rc = cfg["roles"][args.role]
    engine = rc.get("engine", "claude")
    base = (cfg_path.parent / cfg.get("repo_path", ".")).resolve()
    working_dir = str((base / rc.get("working_dir", ".")).resolve())
    Path(working_dir).mkdir(parents=True, exist_ok=True)

    co = cfg.get("coordinator", {})
    uri = f"ws://{co.get('host', '127.0.0.1')}:{co.get('port', 8765)}/ws/{args.role}"
    print(f"[worker:{args.role}] engine={engine} cwd={working_dir}\n[worker:{args.role}] connecting {uri}", flush=True)

    async with websockets.connect(uri, max_size=_STDOUT_LINE_LIMIT) as ws:
        await _send(ws, {"type": "status", "status": "idle"})
        print(f"[worker:{args.role}] connected, idle", flush=True)
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") in ("dispatch", "followup"):
                try:
                    await run_task(ws, args.role, engine, working_dir, frame)
                except Exception as exc:  # noqa: BLE001 - report, don't die
                    await _send(ws, {"type": "result", "task_id": frame.get("task_id"),
                                     "role": args.role, "call_type": frame["type"],
                                     "outcome": "error", "error_detail": repr(exc),
                                     "permission_denials": [], "rate_limited": False})
                    await _send(ws, {"type": "status", "status": "blocked",
                                     "task_id": frame.get("task_id")})
                    print(f"[worker:{args.role}] ERROR: {exc!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
