import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer import build_review_cmd, parse_agy_result, review_prompt  # noqa: E402


def agy_json(**over):
    base = {"conversation_id": "c1", "status": "SUCCESS", "response": "",
            "duration_seconds": 2.0, "num_turns": 1,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    base.update(over)
    return json.dumps(base)


class BuildReviewCmd(unittest.TestCase):
    def test_full(self):
        self.assertEqual(
            build_review_cmd("agy", "P", "gemini-3.8-flash-low", "/s.json"),
            ["agy", "-p", "P", "--output-format", "json", "--dangerously-skip-permissions",
             "--model", "gemini-3.8-flash-low", "--json-schema", "/s.json"],
        )

    def test_minimal(self):
        cmd = build_review_cmd("agy", "P", None, None)
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--json-schema", cmd)

    def test_wrong_engine(self):
        with self.assertRaises(ValueError):
            build_review_cmd("claude", "P", None, None)


class ReviewPrompt(unittest.TestCase):
    def test_contains_task_and_cmd(self):
        p = review_prompt("add subtract", "/wd", "pytest -q")
        for s in ("add subtract", "/wd", "pytest -q", "JSON only", "git diff HEAD"):
            self.assertIn(s, p)


class ParseAgyResult(unittest.TestCase):
    def test_pass(self):
        out = agy_json(response=json.dumps({"verdict": "pass", "feedback": "ok"}))
        r = parse_agy_result(out, 0, "")
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual(r["feedback"], "ok")
        self.assertEqual(r["tokens"], 15)
        self.assertEqual(r["duration_ms"], 2000)
        self.assertFalse(r["rate_limited"])

    def test_fail(self):
        out = agy_json(response=json.dumps({"verdict": "fail", "feedback": "divide is wrong"}))
        r = parse_agy_result(out, 0, "")
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("divide", r["feedback"])

    def test_prefers_structured_output_over_messy_response(self):
        # real agy: `response` is two concatenated JSON blobs; `structured_output` is clean
        out = agy_json(
            response='{"verdict": "fail", "feedback": "x"}\n{"feedback":"x","toolAction":"done","verdict":"fail"}\n',
            structured_output={"verdict": "fail", "feedback": "calc.py not found"},
        )
        r = parse_agy_result(out, 0, "")
        self.assertEqual(r["verdict"], "fail")
        self.assertEqual(r["feedback"], "calc.py not found")

    def test_falls_back_to_first_valid_response_line(self):
        out = agy_json(response='{"verdict": "pass", "feedback": "ok"}\n{"noise": true}\n')
        r = parse_agy_result(out, 0, "")
        self.assertEqual(r["verdict"], "pass")

    def test_status_error(self):
        r = parse_agy_result(agy_json(status="ERROR", response="boom"), 0, "")
        self.assertEqual(r["verdict"], "error")

    def test_nonzero_exit(self):
        r = parse_agy_result(agy_json(response=json.dumps({"verdict": "pass", "feedback": ""})), 1, "")
        self.assertEqual(r["verdict"], "error")

    def test_rate_limited(self):
        r = parse_agy_result("garbage", 1, "google.api_core.exceptions.ResourceExhausted: 429 RESOURCE_EXHAUSTED")
        self.assertEqual(r["verdict"], "error")
        self.assertTrue(r["rate_limited"])

    def test_unparseable_verdict(self):
        r = parse_agy_result(agy_json(response="not json at all"), 0, "")
        self.assertEqual(r["verdict"], "error")
        self.assertIn("parse", r["detail"])


if __name__ == "__main__":
    unittest.main()
