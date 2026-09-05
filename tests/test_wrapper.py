import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import worker  # noqa: E402
from worker import build_engine_cmd, parse_result_line, _progress_frames  # noqa: E402

# Real-shaped `result` lines captured in the phase-1 spike (trimmed).
DONE = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6,
        "session_id": "c7d1867b-309d-43d1-93ba-c61893044073",
        "total_cost_usd": 0.0896838, "duration_ms": 16142, "permission_denials": []}
DENIED = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 9,
          "session_id": "7d51ceda-f878-4c00-ac41-08bcf9f29d10",
          "total_cost_usd": 0.116813, "duration_ms": 23638,
          "permission_denials": [{"tool_name": "Bash", "tool_use_id": "x",
                                  "tool_input": {"command": "python -m unittest"}}]}


class BuildEngineCmd(unittest.TestCase):
    def test_claude_plain(self):
        self.assertEqual(
            build_engine_cmd("claude", "do x"),
            ["claude", "-p", "do x", "--output-format", "stream-json",
             "--verbose", "--permission-mode", "bypassPermissions"],
        )

    def test_claude_resume_prepends_session(self):
        cmd = build_engine_cmd("claude", "next step", "sid123")
        self.assertEqual(cmd[:4], ["claude", "--resume", "sid123", "-p"])
        self.assertIn("bypassPermissions", cmd)

    def test_fake_uses_this_python(self):
        cmd = build_engine_cmd("fake", "do x")
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("fake_engine.py"))
        self.assertEqual(cmd[-1], "do x")

    def test_unknown_engine(self):
        with self.assertRaises(ValueError):
            build_engine_cmd("bogus", "x")


class ParseResultLine(unittest.TestCase):
    def test_clean_success_is_done(self):
        r = parse_result_line(DONE, 0, "")
        self.assertEqual(r["outcome"], "done")
        self.assertEqual(r["session_id"], DONE["session_id"])
        self.assertAlmostEqual(r["total_cost_usd"], 0.0896838)
        self.assertFalse(r["rate_limited"])

    def test_permission_denial_is_blocked(self):
        r = parse_result_line(DENIED, 0, "")
        self.assertEqual(r["outcome"], "blocked")
        self.assertTrue(r["permission_denials"])

    def test_missing_result_line_is_error(self):
        r = parse_result_line(None, 1, "boom")
        self.assertEqual(r["outcome"], "error")
        self.assertTrue(r["is_error"])
        self.assertIn("boom", r["error_detail"])

    def test_rate_limit_in_stderr_flags_and_blocks(self):
        r = parse_result_line(DONE, 0, "API Error: 429 rate limit exceeded, retry later")
        self.assertTrue(r["rate_limited"])
        self.assertEqual(r["outcome"], "blocked")

    def test_nonzero_exit_clean_result_is_error(self):
        r = parse_result_line(DONE, 3, "")
        self.assertEqual(r["outcome"], "error")


class ProgressFrames(unittest.TestCase):
    def test_tool_use_extracted(self):
        line = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "id": "t1", "input": {}}]}}
        frames = list(_progress_frames(line, "task-1"))
        self.assertEqual(frames, [{"type": "progress", "task_id": "task-1",
                                   "event": "tool_use", "name": "Edit"}])

    def test_tool_result_error_flag(self):
        line = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True}]}}
        frames = list(_progress_frames(line, "task-1"))
        self.assertEqual(frames[0]["event"], "tool_result")
        self.assertTrue(frames[0]["is_error"])


if __name__ == "__main__":
    unittest.main()
