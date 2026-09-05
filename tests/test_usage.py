import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coordinator import init_db, usage_by_pool, usage_summary  # noqa: E402

NOW = 1_000_000.0
LIMITS = {"calls_per_5h": 45, "duration_hours_per_7d": 40}


def seed(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.executemany(
        "INSERT INTO usage_ledger(terminal_role,ts,cost_usd,duration_ms,call_type) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


class UsageSummary(unittest.TestCase):
    def test_windows_exclude_old_rows(self):
        conn = seed([
            ("backend", NOW - 60, 0.10, 1000, "dispatch"),          # in 5h + 7d
            ("backend", NOW - 4 * 3600, 0.20, 2000, "dispatch"),    # in 5h + 7d
            ("backend", NOW - 6 * 3600, 0.30, 4000, "dispatch"),    # in 7d only
            ("backend", NOW - 8 * 86400, 0.99, 9999, "dispatch"),   # outside both
        ])
        s = usage_summary(conn, LIMITS, now=NOW)
        self.assertEqual(s["calls_5h"], 2)
        self.assertAlmostEqual(s["cost_usd_5h"], 0.30)
        self.assertEqual(s["duration_7d_ms"], 1000 + 2000 + 4000)
        self.assertFalse(s["near_limit"])

    def test_near_limit_on_call_count(self):
        # 36 == 0.8 * 45 -> at the threshold
        conn = seed([("backend", NOW - 10, 0.0, 0, "dispatch")] * 36)
        self.assertTrue(usage_summary(conn, LIMITS, now=NOW)["near_limit"])

    def test_near_limit_on_duration(self):
        # one 33h call, threshold is 0.8 * 40 = 32h
        conn = seed([("backend", NOW - 10, 0.0, 33 * 3_600_000, "dispatch")])
        self.assertTrue(usage_summary(conn, LIMITS, now=NOW)["near_limit"])

    def test_empty_ledger(self):
        s = usage_summary(seed([]), LIMITS, now=NOW)
        self.assertEqual(s["calls_5h"], 0)
        self.assertEqual(s["duration_7d_ms"], 0)
        self.assertFalse(s["near_limit"])


def seed_full(rows):
    """rows: (role, ts, cost_usd, duration_ms, call_type, units, unit_kind)"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.executemany(
        "INSERT INTO usage_ledger(terminal_role,ts,cost_usd,duration_ms,call_type,units,unit_kind) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


class UsageByPool(unittest.TestCase):
    def test_pools_aggregate_by_role(self):
        conn = seed_full([
            ("head", NOW, 0.10, 1000, "head_turn", None, None),
            ("backend", NOW, 0.20, 2000, "dispatch", None, None),
            ("reviewer", NOW, None, 5000, "review", 89000, "tokens"),
            ("reviewer", NOW, None, 4000, "review", 11000, "tokens"),
            ("docs", NOW, None, 20000, "docs", 1, "credits"),
            ("router", NOW, 0.05, 3000, "router_review", 4200, "tokens"),
        ])
        p = usage_by_pool(conn, now=NOW)
        self.assertAlmostEqual(p["claude"]["cost_usd"], 0.30)
        self.assertEqual(p["claude"]["calls"], 2)
        self.assertEqual(p["gemini"]["units"], 100000)
        self.assertEqual(p["gemini"]["unit_kind"], "tokens")
        self.assertEqual(p["copilot"]["units"], 1)
        self.assertEqual(p["copilot"]["unit_kind"], "credits")
        self.assertEqual(p["router"]["units"], 4200)
        self.assertAlmostEqual(p["router"]["cost_usd"], 0.05)

    def test_empty(self):
        p = usage_by_pool(seed_full([]), now=NOW)
        for pool in ("claude", "gemini", "copilot", "router"):
            self.assertEqual(p[pool]["calls"], 0)
            self.assertIsNone(p[pool]["units"])
            self.assertIsNone(p[pool]["cost_usd"])


if __name__ == "__main__":
    unittest.main()
