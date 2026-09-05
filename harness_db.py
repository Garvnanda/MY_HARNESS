"""State schema + pure read helpers. No I/O at import so tests can use it freely.
Write helpers live in coordinator.py, bound to its connection + lock.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS terminals (
    role                TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    current_session_id  TEXT,
    current_task_id     TEXT,
    last_updated        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    role             TEXT NOT NULL,
    instruction_text TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       REAL NOT NULL,
    completed_at     REAL,
    review_verdict   TEXT,
    review_feedback  TEXT,
    review_cycles    INTEGER NOT NULL DEFAULT 0
);
-- ponytail: columns added via CREATE TABLE only; a pre-existing harness_state.db
-- must be deleted to pick them up. It is a gitignored scratch file.
CREATE TABLE IF NOT EXISTS usage_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    terminal_role TEXT NOT NULL,
    ts            REAL NOT NULL,
    cost_usd      REAL,
    duration_ms   INTEGER,
    call_type     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    terminal   TEXT,
    event_type TEXT NOT NULL,
    payload    TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def usage_summary(conn: sqlite3.Connection, limits: dict, now: float | None = None) -> dict:
    """Rolling 5h call count + 7d duration sum vs conservative limits (implementation.md sec 4).
    Read-only signal; enforcement/pausing is Head's job (phase 3)."""
    now = now or time.time()
    calls_per_5h = limits.get("calls_per_5h", 45)
    hours_per_7d = limits.get("duration_hours_per_7d", 40)
    calls_5h = conn.execute(
        "SELECT COUNT(*) FROM usage_ledger WHERE ts >= ?", (now - 5 * 3600,)
    ).fetchone()[0]
    dur_7d_ms = conn.execute(
        "SELECT COALESCE(SUM(duration_ms),0) FROM usage_ledger WHERE ts >= ?", (now - 7 * 86400,)
    ).fetchone()[0]
    cost_5h = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM usage_ledger WHERE ts >= ?", (now - 5 * 3600,)
    ).fetchone()[0]
    near_limit = (calls_5h >= 0.8 * calls_per_5h) or (dur_7d_ms >= 0.8 * hours_per_7d * 3_600_000)
    return {
        "calls_5h": calls_5h,
        "calls_per_5h": calls_per_5h,
        "cost_usd_5h": round(cost_5h, 6),
        "duration_7d_ms": dur_7d_ms,
        "duration_7d_hours": round(dur_7d_ms / 3_600_000, 3),
        "duration_hours_per_7d": hours_per_7d,
        "near_limit": bool(near_limit),
    }


def iso(ts) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None
