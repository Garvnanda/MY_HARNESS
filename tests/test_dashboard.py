import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import coordinator  # noqa: E402


class DashboardEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator.app)

    def setUp(self):
        coordinator.DB.execute("DELETE FROM event_log WHERE terminal='dtest'")
        coordinator.DB.commit()

    def test_root_serves_dashboard_html(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("harness dashboard", r.text)

    def test_events_shape_and_limit(self):
        for i in range(6):
            coordinator.log_event("dtest", f"e{i}", {"i": i})
        rows = self.client.get("/events?role=dtest&limit=3").json()
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(rows[0]), {"id", "ts", "iso", "terminal", "event_type", "payload_short"})
        # default order is oldest-first within the returned window
        self.assertLess(rows[0]["id"], rows[-1]["id"])

    def test_events_after_is_incremental_ascending(self):
        coordinator.log_event("dtest", "first", {})
        base = self.client.get("/events?role=dtest").json()[-1]["id"]
        coordinator.log_event("dtest", "second", {})
        coordinator.log_event("dtest", "third", {})
        rows = self.client.get(f"/events?role=dtest&after={base}").json()
        self.assertEqual([r["event_type"] for r in rows], ["second", "third"])

    def test_usage_has_by_pool(self):
        u = self.client.get("/usage").json()
        self.assertIn("by_pool", u)
        self.assertEqual(set(u["by_pool"]), {"claude", "gemini", "copilot", "router"})
        self.assertIn("near_limit", u)  # rolling-window fields still present

    def test_record_usage_units_persist_and_surface(self):
        coordinator.DB.execute("DELETE FROM usage_ledger WHERE terminal_role='reviewer'")
        coordinator.DB.commit()
        coordinator.record_usage("reviewer", None, 4200, "review", units=777, unit_kind="tokens")
        pool = self.client.get("/usage").json()["by_pool"]["gemini"]
        self.assertEqual(pool["units"], 777)
        self.assertEqual(pool["unit_kind"], "tokens")
        self.assertEqual(pool["calls"], 1)


if __name__ == "__main__":
    unittest.main()
