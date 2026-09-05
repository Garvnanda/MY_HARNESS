import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordinator  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


def task_row(tid):
    return coordinator.DB.execute(
        "SELECT status, review_verdict, review_cycles FROM tasks WHERE id=?", (tid,)).fetchone()


class ReviewRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        coordinator.CONNS.clear()
        coordinator.DB.execute("DELETE FROM tasks WHERE id LIKE 'rt-%'")
        coordinator.DB.commit()

    async def test_worker_done_auto_dispatches_review(self):
        coordinator.CONNS["reviewer"] = FakeWS()
        coordinator.create_task("rt-1", "backend", "do X")
        await coordinator.on_worker_done("rt-1", {"role": "backend", "session_id": "s1"})

        frame = coordinator.CONNS["reviewer"].sent[0]
        self.assertEqual(frame["type"], "review")
        self.assertEqual(frame["task_id"], "rt-1")
        self.assertEqual(frame["worker_role"], "backend")
        self.assertEqual(task_row("rt-1")["status"], "in_review")

    async def test_worker_done_without_reviewer_goes_to_head(self):
        coordinator.CONNS["head"] = FakeWS()
        coordinator.create_task("rt-2", "backend", "do X")
        await coordinator.on_worker_done("rt-2", {"role": "backend"})

        self.assertEqual(coordinator.CONNS["head"].sent[0]["event"], "worker_task_done")
        self.assertEqual(task_row("rt-2")["status"], "done")

    async def test_review_pass_marks_done_and_wakes_head(self):
        coordinator.CONNS["head"] = FakeWS()
        coordinator.create_task("rt-3", "backend", "do X")
        await coordinator.on_review_result({"task_id": "rt-3", "verdict": "pass",
                                            "feedback": "lgtm", "duration_ms": 100})

        row = task_row("rt-3")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["review_verdict"], "pass")
        self.assertEqual(coordinator.CONNS["head"].sent[0]["event"], "worker_task_reviewed")

    async def test_review_fail_routes_feedback_back_to_worker(self):
        coordinator.CONNS["backend"] = FakeWS()
        coordinator.upsert_terminal("backend", "idle", session_id="ws-1")
        coordinator.create_task("rt-4", "backend", "do X")
        await coordinator.on_review_result({"task_id": "rt-4", "verdict": "fail",
                                            "feedback": "divide returns a*b"})

        frame = coordinator.CONNS["backend"].sent[0]
        self.assertEqual(frame["type"], "followup")
        self.assertEqual(frame["session_id"], "ws-1")
        self.assertIn("divide returns a*b", frame["instructions"])
        row = task_row("rt-4")
        self.assertEqual(row["status"], "revising")
        self.assertEqual(row["review_cycles"], 1)

    async def test_review_fail_at_cap_stops_and_escalates(self):
        coordinator.CONNS["head"] = FakeWS()
        coordinator.CONNS["backend"] = FakeWS()
        coordinator.create_task("rt-5", "backend", "do X")
        coordinator.DB.execute("UPDATE tasks SET review_cycles=2 WHERE id='rt-5'")  # max is 3
        coordinator.DB.commit()
        await coordinator.on_review_result({"task_id": "rt-5", "verdict": "fail", "feedback": "nope"})

        self.assertEqual(task_row("rt-5")["status"], "review_stuck")
        self.assertEqual(coordinator.CONNS["head"].sent[0]["event"], "review_stuck")
        self.assertEqual(coordinator.CONNS["backend"].sent, [])  # no re-route at the cap

    async def test_review_error_escalates_to_head(self):
        coordinator.CONNS["head"] = FakeWS()
        coordinator.create_task("rt-6", "backend", "do X")
        await coordinator.on_review_result({"task_id": "rt-6", "verdict": "error",
                                            "detail": "agy 429", "rate_limited": True})

        self.assertEqual(task_row("rt-6")["status"], "review_error")
        evt = coordinator.CONNS["head"].sent[0]
        self.assertEqual(evt["event"], "review_error")
        self.assertTrue(evt["rate_limited"])


if __name__ == "__main__":
    unittest.main()
