import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import router  # noqa: E402


class QuotaDetection(unittest.TestCase):
    def test_hits(self):
        for s in ["HTTP 429 Too Many Requests", "RESOURCE_EXHAUSTED", "quota exceeded",
                  "billing hard limit reached", "402 Payment Required"]:
            self.assertTrue(router.is_quota_error(s), s)

    def test_misses(self):
        self.assertFalse(router.is_quota_error("SyntaxError: bad token"))
        self.assertFalse(router.is_quota_error(None))


class ExtractJson(unittest.TestCase):
    def test_bare(self):
        self.assertEqual(router._extract_json('{"verdict": "pass"}'), {"verdict": "pass"})

    def test_fenced(self):
        self.assertEqual(router._extract_json('```json\n{"verdict": "fail"}\n```'), {"verdict": "fail"})

    def test_prose_wrapped(self):
        self.assertEqual(router._extract_json('Sure:\n{"verdict": "pass", "feedback": "ok"} done'),
                         {"verdict": "pass", "feedback": "ok"})


class Config(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("ROUTER_BASE_URL", "ROUTER_API_KEY", "ROUTER_MODEL_REVIEW", "ROUTER_MODEL_DOCS")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_unconfigured_raises(self):
        self.assertFalse(router.configured())
        with self.assertRaises(router.RouterUnconfigured):
            router.router_chat([{"role": "user", "content": "hi"}], "opus-5")

    def test_model_not_in_allowlist_raises(self):
        os.environ.update(ROUTER_BASE_URL="http://x", ROUTER_API_KEY="k",
                          ROUTER_MODEL_REVIEW="opus-5", ROUTER_MODEL_DOCS="gpt-5.6-sol")
        self.assertTrue(router.configured())
        with self.assertRaises(ValueError):
            router.router_chat([{"role": "user", "content": "hi"}], "deepseek-v4-flash")


class RouteReview(unittest.TestCase):
    def setUp(self):
        self._orig = router.router_chat
        os.environ.update(ROUTER_BASE_URL="http://x", ROUTER_API_KEY="k",
                          ROUTER_MODEL_REVIEW="opus-5", ROUTER_MODEL_DOCS="gpt-5.6-sol")

    def tearDown(self):
        router.router_chat = self._orig
        for k in ("ROUTER_BASE_URL", "ROUTER_API_KEY", "ROUTER_MODEL_REVIEW", "ROUTER_MODEL_DOCS"):
            os.environ.pop(k, None)

    def test_parses_verdict(self):
        router.router_chat = lambda msgs, model, **kw: {
            "text": '{"verdict": "pass", "feedback": "looks fine"}', "usage": {"total_tokens": 12},
            "model": model, "duration_ms": 9}
        r = router.route_review("add subtract", "/wd", "python -m unittest")
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual(r["feedback"], "looks fine")
        self.assertEqual(r["source"], "router")

    def test_unparseable_defaults_to_fail(self):
        router.router_chat = lambda msgs, model, **kw: {
            "text": "I think it's probably ok", "usage": {}, "model": model, "duration_ms": 3}
        r = router.route_review("x", "/wd", "pytest")
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("probably ok", r["feedback"])

    def test_route_docs_returns_file_map(self):
        router.router_chat = lambda msgs, model, **kw: {
            "text": '{"README.md": "# New\\ndocs here"}', "usage": {}, "model": model, "duration_ms": 4}
        r = router.route_docs("document subtract", {"README.md": "# Old"})
        self.assertEqual(r["files"], {"README.md": "# New\ndocs here"})


if __name__ == "__main__":
    unittest.main()
