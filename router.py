"""Router fallback (implementation.md sec 5). When the Reviewer's `agy` or Docs'
`copilot` hits a quota / rate-limit / billing wall, the coordinator re-routes
the same pending task here: a direct chat-completions HTTP call to the $550
agent-router pool, restricted to Opus 5 / GPT-5.6 sol (idea.md sec 1 -- never
DeepSeek / GLM). No CLI subprocess.

Config from a gitignored `.env` at repo root (see `.env.example`). If unset the
callers detect `RouterUnconfigured` and escalate to Head, unchanged from phase 4.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

_QUOTA_RE = re.compile(
    r"rate.?limit|quota|RESOURCE_EXHAUSTED|\b429\b|\b402\b|billing|insufficient|exhausted",
    re.I,
)


class RouterUnconfigured(RuntimeError):
    pass


def _cfg():
    return (
        os.environ.get("ROUTER_BASE_URL", "").strip().rstrip("/"),
        os.environ.get("ROUTER_API_KEY", "").strip(),
        os.environ.get("ROUTER_MODEL_REVIEW", "").strip(),
        os.environ.get("ROUTER_MODEL_DOCS", "").strip(),
    )


def configured() -> bool:
    base, key, _, _ = _cfg()
    return bool(base and key)


def is_quota_error(text: str | None) -> bool:
    return bool(text and _QUOTA_RE.search(text))


def router_chat(messages: list[dict], model: str, *, timeout: float = 120.0) -> dict:
    base, key, m_review, m_docs = _cfg()
    if not (base and key):
        raise RouterUnconfigured("ROUTER_BASE_URL / ROUTER_API_KEY not set in .env")
    allowed = {m for m in (m_review, m_docs) if m}
    if model not in allowed:
        raise ValueError(f"router model {model!r} not in allow-list {sorted(allowed)}")
    t0 = time.time()
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return {"text": data["choices"][0]["message"]["content"],
            "usage": data.get("usage", {}), "model": model,
            "duration_ms": int((time.time() - t0) * 1000)}


def _extract_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip().strip("`").strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def route_review(instructions: str, working_dir: str, test_cmd: str) -> dict:
    """Reviewer fallback -> {verdict, feedback, source:'router', tokens, duration_ms}."""
    from reviewer import review_prompt

    _, _, model, _ = _cfg()
    prompt = review_prompt(instructions, working_dir, test_cmd) + (
        "\n\nYou cannot run commands or read files here. Judge from the task text and "
        "general knowledge; if you cannot confirm the tests pass, say so in feedback "
        "and lean toward \"fail\"."
    )
    out = router_chat([{"role": "user", "content": prompt}], model)
    v = _extract_json(out["text"]) or {}
    verdict = v.get("verdict") if v.get("verdict") in ("pass", "fail") else "fail"
    return {"verdict": verdict, "feedback": v.get("feedback") or out["text"][:800],
            "source": "router", "tokens": out["usage"].get("total_tokens"),
            "cost": out["usage"].get("cost") or out["usage"].get("total_cost"),
            "duration_ms": out["duration_ms"]}


def route_docs(instructions: str, files: dict[str, str]) -> dict:
    """Docs fallback. `files` = {path: current_text}. -> {files: {path: new_text}, ...}."""
    _, _, _, model = _cfg()
    listing = "\n\n".join(f"### FILE: {p}\n```\n{t}\n```" for p, t in files.items())
    prompt = (
        "Update the markdown documentation file(s) below to reflect this change:\n\n"
        f"<task>\n{instructions}\n</task>\n\n{listing}\n\n"
        "Return JSON only: an object mapping each file path to its COMPLETE new "
        "contents. Include every file, even ones you did not change."
    )
    out = router_chat([{"role": "user", "content": prompt}], model)
    result = _extract_json(out["text"])
    if not isinstance(result, dict):
        raise ValueError(f"route_docs: could not parse file map: {out['text'][:300]}")
    return {"files": {k: v for k, v in result.items() if isinstance(v, str)},
            "duration_ms": out["duration_ms"], "tokens": out["usage"].get("total_tokens"),
            "cost": out["usage"].get("cost") or out["usage"].get("total_cost")}
