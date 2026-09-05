"""Reviewer wrapper (implementation.md sec 3, sec 5). Fires ONLY when the
coordinator sends a `review` frame -- which it does automatically every time a
worker marks a sub-task done (never a Head decision). Reads the change, runs the
test suite, returns a structured pass/fail verdict + feedback.

Engine: Antigravity CLI (`agy`), Gemini model.  Run:  python reviewer.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import websockets

from worker import _STDOUT_LINE_LIMIT, _send, spawn_argv

SCHEMA_PATH = str(Path(__file__).with_name("reviewer_schema.json"))
_RATE_RE = re.compile(r"rate.?limit|quota|RESOURCE_EXHAUSTED|\b429\b|billing|insufficient", re.I)


# --------------------------------------------------------------------------- pure helpers (unit-tested)

def build_review_cmd(engine: str, prompt: str, model: str | None, schema_path: str | None) -> list[str]:
    if engine != "agy":
        raise ValueError(f"reviewer engine {engine!r} not supported (expected 'agy')")
    argv = ["agy", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"]
    if model:
        argv += ["--model", model]
    if schema_path:
        argv += ["--json-schema", schema_path]
    return argv


def review_prompt(instructions: str, working_dir: str, test_cmd: str) -> str:
    return (
        "You are a strict code reviewer. A worker was asked to do the following "
        f"in {working_dir}:\n\n<task>\n{instructions}\n</task>\n\n"
        "Review the change:\n"
        f"1. If {working_dir} is a git repo, run `git diff HEAD` to see what changed; "
        "otherwise inspect the current files.\n"
        f"2. Run the test suite: `{test_cmd}` (cwd {working_dir}).\n"
        "3. Judge whether the change correctly and completely satisfies the task, "
        "with tests passing and no obvious bug or omission.\n\n"
        'Respond with JSON only: {"verdict": "pass" or "fail", '
        '"feedback": "<specifics; if fail, exactly what to fix>"}.'
    )


def _loads_lenient(s: str):
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
        s = s[4:].strip() if s.lower().startswith("json") else s.strip()
    try:
        return json.loads(s)
    except ValueError:
        return None


def parse_agy_result(stdout_text: str, returncode: int, stderr_text: str) -> dict:
    blob = stdout_text.strip()
    obj = _loads_lenient(blob)
    if obj is None:
        for line in reversed(blob.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                obj = _loads_lenient(line)
                if obj is not None:
                    break

    rate_limited = bool(_RATE_RE.search(stdout_text + "\n" + stderr_text))
    tokens = (obj or {}).get("usage", {}).get("total_tokens")
    duration_ms = int((obj or {}).get("duration_seconds", 0) * 1000)

    if obj is None or returncode != 0 or obj.get("status") != "SUCCESS":
        detail = ((obj or {}).get("response")
                  or stderr_text.strip()[-500:]
                  or blob[-500:]
                  or f"exit {returncode}, no parseable agy output")
        return {"verdict": "error", "feedback": None, "detail": detail,
                "rate_limited": rate_limited, "tokens": tokens, "duration_ms": duration_ms}

    # agy gives the schema-conforming result cleanly in `structured_output`; `response`
    # can be one or more concatenated JSON blobs, so only fall back to it line by line.
    verdict_obj = obj.get("structured_output")
    if not (isinstance(verdict_obj, dict) and verdict_obj.get("verdict") in ("pass", "fail")):
        verdict_obj = _loads_lenient(obj.get("response", ""))
        if not (isinstance(verdict_obj, dict) and verdict_obj.get("verdict") in ("pass", "fail")):
            verdict_obj = None
            for line in str(obj.get("response", "")).splitlines():
                cand = _loads_lenient(line)
                if isinstance(cand, dict) and cand.get("verdict") in ("pass", "fail"):
                    verdict_obj = cand
                    break

    if not (isinstance(verdict_obj, dict) and verdict_obj.get("verdict") in ("pass", "fail")):
        return {"verdict": "error", "feedback": None,
                "detail": f"could not parse verdict from agy output: {str(obj.get('response'))[:300]}",
                "rate_limited": rate_limited, "tokens": tokens, "duration_ms": duration_ms}

    return {"verdict": verdict_obj["verdict"], "feedback": verdict_obj.get("feedback", ""),
            "detail": None, "rate_limited": False, "tokens": tokens, "duration_ms": duration_ms}


# --------------------------------------------------------------------------- runtime

async def run_review(ws, frame: dict) -> None:
    task_id = frame["task_id"]
    wd = frame["working_dir"]
    prompt = review_prompt(frame["instructions"], wd, frame.get("test_cmd", "python -m unittest"))
    argv = spawn_argv(build_review_cmd(frame.get("engine", "agy"), prompt, frame.get("model"), SCHEMA_PATH))

    await _send(ws, {"type": "status", "status": "working", "task_id": task_id})
    print(f"\n[reviewer] reviewing {task_id} ({frame.get('worker_role')}) in {wd}\n"
          f"[reviewer] $ agy -p <prompt> --model {frame.get('model')}\n", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=wd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=_STDOUT_LINE_LIMIT,
    )
    chunks: list[str] = []
    try:
        async for raw in proc.stdout:
            s = raw.decode("utf-8", "replace")
            chunks.append(s)
            print(s.rstrip(), flush=True)  # Garv watches the live scroll
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    stderr_text = (await proc.stderr.read()).decode("utf-8", "replace")
    if stderr_text.strip():
        print(f"[reviewer] stderr:\n{stderr_text}", flush=True)

    parsed = parse_agy_result("".join(chunks), proc.returncode, stderr_text)
    await _send(ws, {"type": "result", "role": "reviewer", "call_type": "review",
                     "task_id": task_id, "verdict": parsed["verdict"],
                     "feedback": parsed["feedback"], "detail": parsed["detail"],
                     "rate_limited": parsed["rate_limited"], "total_cost_usd": None,
                     "duration_ms": parsed["duration_ms"], "tokens": parsed["tokens"]})
    await _send(ws, {"type": "status", "status": "idle", "task_id": task_id})
    print(f"\n[reviewer] {task_id}: {parsed['verdict']}"
          f"{' - ' + (parsed['feedback'] or parsed['detail'] or '') if parsed['verdict'] != 'pass' else ''}\n",
          flush=True)


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
    uri = f"ws://{co.get('host', '127.0.0.1')}:{co.get('port', 8765)}/ws/reviewer"
    print(f"[reviewer] connecting {uri}", flush=True)

    async with websockets.connect(uri, max_size=_STDOUT_LINE_LIMIT) as ws:
        await _send(ws, {"type": "status", "status": "idle"})
        print("[reviewer] connected, idle", flush=True)
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") == "review":
                try:
                    await run_review(ws, frame)
                except Exception as exc:  # noqa: BLE001 - report, keep loop alive
                    await _send(ws, {"type": "result", "role": "reviewer", "call_type": "review",
                                     "task_id": frame.get("task_id"), "verdict": "error",
                                     "feedback": None, "detail": repr(exc),
                                     "rate_limited": False, "total_cost_usd": None,
                                     "duration_ms": None, "tokens": None})
                    await _send(ws, {"type": "status", "status": "idle"})
                    print(f"[reviewer] ERROR: {exc!r}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
