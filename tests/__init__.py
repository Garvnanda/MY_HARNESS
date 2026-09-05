"""Test isolation: point the coordinator at a throwaway config + DB *before* any
`tests.test_*` module imports `coordinator`, so the suite never writes to the
real repo-root `harness_state.db`.
"""
import json
import os
import tempfile
from pathlib import Path

_d = Path(tempfile.mkdtemp(prefix="harness_test_"))
(_d / "project_config.json").write_text(json.dumps({
    "repo_path": ".",
    "coordinator": {"host": "127.0.0.1", "port": 8765},
    "usage_limits": {"calls_per_5h": 45, "duration_hours_per_7d": 40},
    "test_cmd": "python -m unittest",
    "max_review_cycles": 3,
    "roles": {
        "head": {"engine": "claude", "mcp_config": "harness_tools.json"},
        "backend": {"engine": "claude", "working_dir": "demo"},
        "reviewer": {"engine": "agy", "model": "gemini-3.8-flash-low"},
        "docs": {"engine": "copilot", "working_dir": "demo", "model": "auto"},
    },
}), encoding="utf-8")
os.environ.setdefault("HARNESS_CONFIG", str(_d / "project_config.json"))
