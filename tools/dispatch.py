"""Dev helper: POST a task to the coordinator. Stands in for Head until phase 3.

    python tools/dispatch.py backend "add multiply(a,b) to calc.py, add a test, run python -m unittest"
    python tools/dispatch.py backend "address the review feedback" <existing_task_id>   # follow-up
"""
import json
import sys
from pathlib import Path

import httpx

if len(sys.argv) < 3:
    sys.exit(__doc__)

role, instructions = sys.argv[1], sys.argv[2]
task_id = sys.argv[3] if len(sys.argv) > 3 else None

cfg = json.loads((Path(__file__).resolve().parent.parent / "project_config.json").read_text("utf-8"))
co = cfg.get("coordinator", {})
url = f"http://{co.get('host', '127.0.0.1')}:{co.get('port', 8765)}/dispatch"

payload = {"role": role, "instructions": instructions}
if task_id:
    payload["task_id"] = task_id

r = httpx.post(url, json=payload, timeout=15)
print(r.status_code, r.text)
