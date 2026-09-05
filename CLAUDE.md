# Project Context

This repo is the **multi-agent coding harness itself** — a tool being built, not a project
it will manage. Read `docs/idea.md` and `docs/implementation.md` in full before doing
anything else. They contain the locked architecture and technical spec; don't deviate from
decisions in them without flagging it to Garv first and explaining why.

## Workflow rule (the one that matters most)

Build strictly in the phases listed in `implementation.md` §8:

1. Spike: confirm `--permission-mode acceptEdits`/`bypassPermissions` actually allows
   unattended multi-step Claude Code work (§1's flagged assumption)
2. Coordinator + one worker wrapper (backend), proven on a trivial task
3. Head, with its MCP tools
4. Reviewer + the auto-trigger-on-done rule
5. Docs + the router fallback logic
6. Dashboard

**Stop after each phase and wait for Garv's review before starting the next one.** Don't
chain straight through multiple phases in one session even if the path forward seems
obvious — that's the point of building it this way. Use Plan Mode to show the plan for a
phase before touching any files, not just for the first one.

## Platform

Native Windows (PowerShell/CMD). No WSL2 — don't assume Unix sockets, tmux, or POSIX-only
tooling are available. Coordination between processes goes over local loopback
HTTP/WebSocket, which works the same on this platform as anywhere else.

## Open items to resolve during the relevant phase, not before

- Whether Antigravity CLI (`agy`) or Copilot CLI (`copilot`) expose their own remaining-quota
  numbers in their output — check when building Reviewer/Docs (phase 4-5), don't assume
  either way going in.
- Exact `--mcp-config` JSON schema Claude Code expects — verify against current docs when
  building Head's MCP server (phase 3), not from memory.

## Commands

Python 3.11, deps already global (`fastapi`, `uvicorn`, `websockets`, `httpx`); `requirements.txt`
lists them. Run everything from the repo root.

- **Offline tests** (no network, no Claude quota): `python -m unittest discover -s tests -t .`
- **Coordinator** (terminal 1): `python coordinator.py` — FastAPI on `127.0.0.1:8765`, creates
  `harness_state.db`. Config from `HARNESS_CONFIG` env or `./project_config.json`.
- **Backend worker** (terminal 2): `python worker.py backend` — connects to the coordinator,
  runs its engine (`claude` by default; set `roles.backend.engine` to `fake` in
  `project_config.json` for an offline dry run against `tools/fake_engine.py`).
- **Reviewer** (terminal 3): `python reviewer.py` — engine `agy` (Gemini,
  `roles.reviewer.model` in config). Fires automatically every time a worker marks a
  sub-task `done` (hard coordinator rule, not a Head decision). Reads the change, runs
  `test_cmd`, returns a `{verdict, feedback}` verdict via `agy --json-schema`. On `fail`
  the coordinator routes feedback straight back into the same worker session (`--resume`)
  and loops up to `max_review_cycles` (default 3), then escalates `review_stuck` to Head.
  On `pass` it wakes Head with `worker_task_reviewed`.
- **Docs** (terminal 4): `python docs.py` — engine `copilot` (`roles.docs.model`).
  Head-dispatched only (`dispatch_task("docs", …)`); updates repo markdown. Does NOT
  trigger the Reviewer. Reports `docs_updated` / `docs_error` to Head.
- **Router fallback** (`router.py`, no terminal): when `agy` or `copilot` hits a
  quota / rate-limit / billing wall, the coordinator re-routes that one pending task
  to a direct chat-completions call (Opus 5 / GPT-5.6 sol only). Config in a
  gitignored `.env` (copy `.env.example`, fill `ROUTER_*`). Blank `.env` → the
  fallback logs `router_unconfigured` and escalates to Head, no regression.
- **Head** (terminal 5): `python head.py` — the terminal Garv talks to. Type a line + enter;
  each line (or a coordinator-injected line/event) becomes one
  `claude -p --resume --mcp-config harness_tools.json` turn. MCP tools (`dispatch_task`,
  `check_worker_status`, `get_reviewer_feedback`, `flag_ready_to_commit`,
  `set_active_project`) live in `harness_tools.py`; `harness_tools.json` is the
  `--mcp-config` file. Run Head from repo root so the relative path resolves.
- **Talk to Head without the terminal** (e.g. from a script): `POST /head_say {"text": "..."}`.
- **Low-level dispatch** (bypasses Head): `python tools/dispatch.py backend "<instructions>"`;
  follow-up into the same session: `python tools/dispatch.py backend "<instructions>" <task_id>`.
- **Dashboard**: open `http://127.0.0.1:8765/` in a browser while the coordinator runs —
  terminal grid, task queue, per-pool usage, live event log, ready-to-commit banner.
  Polls `/state` + `/usage` + `/events` every 2s; no build step, no extra process.
- **Inspect state (raw)**: `curl 127.0.0.1:8765/state` (terminals, tasks, `ready_to_commit`,
  `head_connected`), `curl 127.0.0.1:8765/usage` (adds `by_pool`), `curl
  127.0.0.1:8765/events?limit=50&role=<opt>`, `curl 127.0.0.1:8765/tasks/<id>`, or
  `sqlite3 harness_state.db "select * from event_log"`.

Phase-2 demo workspace is `demo/` (gitignored); `project_config.json` points `backend` there.

## Git

Confirm with Garv before running `git commit` or `git push` on this repo, same as he'd want
on any project — don't assume silent commits are fine just because this is tooling rather
than a client project.
