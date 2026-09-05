import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docs import build_docs_cmd, parse_copilot_stream  # noqa: E402

RESULT_OK = {"type": "result", "sessionId": "s1", "exitCode": 0,
             "usage": {"premiumRequests": 1, "totalApiDurationMs": 2742,
                       "sessionDurationMs": 7681,
                       "codeChanges": {"filesModified": ["README.md"]}}}
USAGE_JSON = {"totalPremiumRequestCost": 1, "totalApiDurationMs": 2742,
              "codeChanges": {"filesModified": ["README.md"]}}


class BuildDocsCmd(unittest.TestCase):
    def test_with_model(self):
        self.assertEqual(
            build_docs_cmd("copilot", "update readme", "auto", "/u.json"),
            ["copilot", "-p", "update readme", "--no-ask-user", "--allow-all-tools",
             "--output-format", "json", "--usage-output-file", "/u.json", "--model", "auto"],
        )

    def test_without_model(self):
        self.assertNotIn("--model", build_docs_cmd("copilot", "x", None, "/u.json"))

    def test_wrong_engine(self):
        with self.assertRaises(ValueError):
            build_docs_cmd("claude", "x", None, "/u.json")


class ParseCopilotStream(unittest.TestCase):
    def test_done(self):
        objs = [
            {"type": "assistant.message", "data": {"content": "Updated README with subtract."}},
            RESULT_OK,
        ]
        r = parse_copilot_stream(objs, 0, "", USAGE_JSON)
        self.assertEqual(r["outcome"], "done")
        self.assertIn("subtract", r["reply"])
        self.assertEqual(r["files_modified"], ["README.md"])
        self.assertEqual(r["premium_requests"], 1)
        self.assertFalse(r["rate_limited"])

    def test_nonzero_exit_is_error(self):
        r = parse_copilot_stream([{**RESULT_OK, "exitCode": 1}], 1, "", None)
        self.assertEqual(r["outcome"], "error")

    def test_missing_result_is_error(self):
        r = parse_copilot_stream([{"type": "assistant.message", "data": {"content": "hi"}}], 0, "", None)
        self.assertEqual(r["outcome"], "error")

    def test_rate_limit_flagged(self):
        objs = [{"type": "session.error", "data": {"message": "429 rate limit exceeded"}}, RESULT_OK]
        r = parse_copilot_stream(objs, 0, "", USAGE_JSON)
        self.assertTrue(r["rate_limited"])
        self.assertEqual(r["outcome"], "error")


if __name__ == "__main__":
    unittest.main()
