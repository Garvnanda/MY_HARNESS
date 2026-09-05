"""Offline stand-in for `claude -p --output-format stream-json`. Emits a handful
of stream-json lines so the coordinator/worker path can be exercised in tests
without spending Claude quota.

Usage: python tools/fake_engine.py [--resume <session_id>] "<instructions>"
"""
import json
import sys
import uuid

args = sys.argv[1:]
session_id = None
if "--resume" in args:
    i = args.index("--resume")
    session_id = args[i + 1]
    del args[i:i + 2]
instructions = args[0] if args else ""
session_id = session_id or f"fake-{uuid.uuid4().hex[:8]}"


def emit(obj):
    print(json.dumps(obj), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session_id})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Edit", "id": "tu1", "input": {"instr": instructions}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "tu1", "is_error": False, "content": "edited"}]}})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Bash", "id": "tu2", "input": {"command": "python -m unittest"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "tu2", "is_error": False, "content": "Ran 1 test\n\nOK"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
      "session_id": session_id, "total_cost_usd": 0.0123, "duration_ms": 1234,
      "permission_denials": []})
