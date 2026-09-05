import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import coordinator  # noqa: E402


class CoordinatorHeadEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(coordinator.app)

    def test_flag_ready_then_state(self):
        r = self.client.post("/flag_ready", json={"summary": "all green, 3 tests pass"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        st = self.client.get("/state").json()
        self.assertIn("head_connected", st)
        self.assertFalse(st["head_connected"])  # no head WS in this test
        self.assertIsNotNone(st["ready_to_commit"])
        self.assertEqual(st["ready_to_commit"]["summary"], "all green, 3 tests pass")

    def test_task_detail_404(self):
        self.assertEqual(self.client.get("/tasks/does-not-exist").status_code, 404)

    def test_head_say_without_head_is_503(self):
        self.assertEqual(self.client.post("/head_say", json={"text": "hi"}).status_code, 503)

    def test_active_project_missing_file_recorded_not_applied(self):
        r = self.client.post("/active_project", json={"config_path": "nope_not_here.json"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["applied"])


if __name__ == "__main__":
    unittest.main()
