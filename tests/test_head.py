import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from head import build_head_cmd, event_to_prompt, head_reply_text  # noqa: E402


class BuildHeadCmd(unittest.TestCase):
    def test_first_turn_no_resume(self):
        cmd = build_head_cmd("do x", None, "mc.json")
        self.assertEqual(cmd[:5], ["claude", "-p", "do x", "--mcp-config", "mc.json"])
        self.assertIn("bypassPermissions", cmd)
        self.assertIn("stream-json", cmd)
        self.assertNotIn("--resume", cmd)
        # Head must not edit code directly
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1:cmd.index("--disallowedTools") + 4],
                         ["Edit", "Write", "NotebookEdit"])

    def test_resume_prepended(self):
        cmd = build_head_cmd("next", "sid-9", "mc.json")
        self.assertEqual(cmd[:5], ["claude", "--resume", "sid-9", "-p", "next"])


class EventToPrompt(unittest.TestCase):
    def test_worker_done(self):
        p = event_to_prompt({"event": "worker_task_done", "role": "backend",
                             "task_id": "t-1", "outcome": "done"})
        self.assertIn("backend", p)
        self.assertIn("t-1", p)
        self.assertIn("flag_ready_to_commit", p)

    def test_unknown_event_falls_back(self):
        p = event_to_prompt({"event": "weird", "x": 1})
        self.assertIn("harness event", p)
        self.assertIn("weird", p)


class HeadReplyText(unittest.TestCase):
    def test_text_and_tool_use(self):
        lines = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Dispatching to backend."},
                {"type": "tool_use", "name": "dispatch_task", "input": {"role": "backend"}},
            ]}},
        ]
        txt = head_reply_text(lines)
        self.assertIn("Dispatching to backend.", txt)
        self.assertIn("[Head -> dispatch_task backend]", txt)

    def test_empty(self):
        self.assertEqual(head_reply_text([]), "")


if __name__ == "__main__":
    unittest.main()
