# Multi-Agent Coding Harness — implementation.md

Built on the decisions locked in idea.md. This is the technical shape — not final code —
so it can be picked apart before anything gets built. A few places below are marked
**[assumption]** where I made a specific engineering call rather than leaving it vague;
push back on any of those.

---

## 1. How each engine is actually invoked (verified against current docs)

This matters because it changes the wrapper design, so it's worth being precise rather
than hand-wavy:

**Claude Code** (`claude`) — headless one-shot mode:
```
claude -p "<instructions>" --output-format stream-json \
  --permission-mode acceptEdits --mcp-config harness_tools.json
```
- `--output-format stream-json` streams structured events (tool calls, results) instead of
  just raw text — this is what lets the wrapper parse progress rather than scrape text.
- Every call's final result includes a `session_id`. **This is the key mechanism for
  continuity**: instead of keeping one giant process alive and piping stdin into it forever,
  the wrapper spawns a fresh `claude -p` call per instruction and uses
  `claude --resume <session_id> "<next instruction>"` for every follow-up. Same logical
  conversation, no fragile long-lived subprocess.
- `--permission-mode acceptEdits` (or `bypassPermissions` if edits alone aren't enough)
  is what allows this to run unattended. **[assumption]** — worth an early spike to confirm
  this is actually sufficient for full autonomy before building everything around it; some
  tool calls may still want interactive confirmation even in headless mode.

**Antigravity CLI** (`agy`) — `agy --print "<instructions>"`. Same shape, headless.

**Copilot CLI** (`copilot`) — `copilot -p "<instructions>" --no-ask-user --allow-all-tools`.
Same shape.

All three fit the identical wrapper pattern: spawn, stream output, capture result, done.

---

## 2. Head: the one real design trade-off in this whole spec

Garv talks to Head directly — but Head also needs to wake up on its own when a worker
finishes or the reviewer sends a verdict, without Garv typing anything. Those two
requirements pull in different directions:

- A raw, native interactive `claude` session is the richest experience for Garv (real TUI,
  checkpoints, etc.) — but nothing external can cleanly inject a message into a live
  interactive session. Simulating a keystroke into it has the exact flakiness Garv already
  ruled out for the worker terminals.
- A headless `-p`/`--resume` chain (like the workers) *can* be triggered from any source —
  Garv typing, or the coordinator pushing an event — because each turn is just another CLI
  invocation with the same session ID.

**[assumption] — going with the second option.** Head's terminal is a thin wrapper that
looks and feels like a running chat: Garv types a line, the wrapper turns it into
`claude -p "<line>" --resume <head_session_id> --mcp-config harness_tools.json` and prints
the reply; the coordinator does the exact same thing on Head's behalf when a worker finishes,
a review verdict comes in, or a quota warning fires. One continuous session either way, just
mediated by a script instead of Garv looking at a raw native TUI. The trade-off: Head loses
some native Claude Code UI niceties (live checkpoint browsing, etc.) in exchange for actually
being able to wake up on its own. If you'd rather keep the native TUI and have the head
require an occasional manual nudge from you to notice events, that's a real, valid
alternative — flag it if you want that instead.

### Head's MCP tools (the custom server, `harness_tools.json` → local stdio server)

| Tool | Purpose |
|---|---|
| `dispatch_task(role, instructions)` | Send a sub-task to backend or frontend |
| `check_worker_status(role)` | Poll a terminal's current state + task summary |
| `get_reviewer_feedback(task_id)` | Pull the latest reviewer verdict for a task |
| `flag_ready_to_commit(summary)` | Marks the dashboard flag + is naturally also said in chat |
| `set_active_project(config_path)` | Switch which repo/config this run targets (see §6) |

---

## 3. Coordinator

A single local FastAPI process (`localhost` only, no external exposure needed) that every
wrapper connects to over a WebSocket, plus a REST layer the dashboard reads from.

**State storage: SQLite** (one file, `harness_state.db`) — simple, file-based, handles the
light concurrent access from 5 wrapper processes fine, no extra service to run.

Rough tables:
- `terminals` — role, status (`idle` / `working` / `blocked` / `done` / `paused-quota`),
  current_session_id, current_task_id, last_updated
- `tasks` — id, role, instruction_text, status, created_at, completed_at, review_verdict
- `usage_ledger` — terminal_role, timestamp, cost_usd, duration_ms, call_type (see §4)
- `event_log` — append-only: timestamp, terminal, event_type, payload (this is the audit
  trail — the main defense against silent hallucination, since nothing happens that isn't
  logged and traceable back)

**Core flow the coordinator drives:**
1. Head calls `dispatch_task` → coordinator pushes a `dispatch` WS message to that worker's
   wrapper → wrapper spawns the CLI call → streams progress back → on completion, worker
   reports `done`/`error` + session_id.
2. Worker reports `done` → coordinator **automatically** enqueues a Reviewer dispatch
   (diff + task context) — this isn't a Head decision, it's a hard rule per idea.md.
3. Reviewer finishes → verdict is `pass` or `fail`.
   - `fail` → coordinator routes the feedback straight back into the *same worker session*
     (`--resume <worker_session_id> "<feedback>"`), no Head involvement needed for the loop
     itself.
   - `pass` → coordinator pushes an async event to Head (`worker_task_reviewed`, pass) →
     Head decides what's next (more sub-tasks, or tell Garv it's ready to commit).

---

## 4. Claude usage management — being honest about what's actually reliable here

I looked deeper into this while drafting, and the picture is more fragile than idea.md's
first pass suggested, worth correcting properly rather than quietly:

Claude Code *does* expose live rate-limit percentages (`rate_limits.five_hour`,
`.seven_day`) — but only to a **statusLine hook in an interactive session**, only after
that session's first response, and there are multiple open reports of the field going
missing intermittently even for paying Max subscribers. It's also not clear it's exposed at
all to headless `-p` calls, which is how Head/Backend/Frontend are actually being driven
here (§2). Building the core safety mechanism on top of something this flaky would be a
mistake.

**So the primary mechanism is self-tracked, not borrowed from an undocumented hook:**
- Every `-p`/`--resume` call's JSON result reliably includes `total_cost_usd` and
  `duration_ms` — these fields *are* documented and stable, unlike `rate_limits`.
- The coordinator keeps a rolling ledger (the `usage_ledger` table) of these across Head +
  Backend + Frontend combined, since they share one pool. It compares that against
  conservative, configurable thresholds (defaulting to the low end of Anthropic's published
  ranges — assume 45 msgs/5hr and 40 hrs/week rather than the higher end, to fail safe).
- When a rolling total nears its threshold, Head proactively checkpoints and pauses the
  relevant terminal(s) *before* actually hitting the real wall, and tells Garv.
- **Reactive backstop**: if the self-tracked estimate is off and Claude actually rate-limits
  a call anyway, the wrapper recognizes the specific error response and treats it exactly
  like a proactive pause — checkpoint, notify, resume later via the stored session_id.
- If a statusLine hook's data happens to be available (e.g. if Garv also runs an ordinary
  interactive Claude Code session elsewhere that has one configured), the coordinator can
  read it as a bonus calibration signal — never a dependency.

---

## 5. Reviewer and Docs — fallback logic

Both check their primary engine's own error responses for anything that looks like a quota
exhaustion (rate limit / billing error from `agy` or `copilot`). On that signal, the
coordinator re-routes the *same pending task* to the router instead, using Opus 5 or GPT-5.6
sol only — the router call is built directly against its API (bearer token + chat-completions
shaped request), independent of the CLI-subprocess pattern used for the other three.

**[open item]** — I don't yet know whether Antigravity CLI or Copilot CLI expose their own
remaining-quota numbers in their JSON/verbose output the way Claude Code's cost field does.
Worth checking both during implementation rather than assuming either way; the dashboard's
usage section should degrade gracefully (just show what's available) rather than requiring
all four sources.

---

## 6. Config layer (what makes this reusable across projects)

A single `project_config.json` per project, e.g.:
```json
{
  "repo_path": "C:/projects/gallasaathi",
  "roles": {
    "backend": { "engine": "claude", "working_dir": "backend/" },
    "frontend": { "engine": "claude", "working_dir": "frontend/" },
    "reviewer": { "engine": "agy", "model": "<garv's choice>" },
    "docs": { "engine": "copilot", "working_dir": "docs/" }
  }
}
```
Head's `set_active_project` tool swaps which config is loaded, so switching to a different
project doesn't mean touching any code — just pointing at a different file.

---

## 7. Dashboard

FastAPI serves both the coordinator API and a small custom frontend (plain HTML/JS or a
minimal build — no framework decision needed yet). Reads live off the coordinator's
REST/WebSocket layer:
- Status grid for all 5 terminals
- Task queue
- Per-terminal log tail (from `event_log`)
- "Ready to commit" banner
- Usage section: Claude (self-tracked, §4), plus Copilot/Gemini/router spend wherever
  those turn out to be queryable (§5's open item)

---

## 8. What to build and check first, before the rest

Given the two flagged uncertainties above, the sane build order is:
1. Confirm `--permission-mode acceptEdits`/`bypassPermissions` actually allows unattended
   multi-step work in a throwaway test project (§1's assumption).
2. Build the coordinator + one worker wrapper (backend) end-to-end on a trivial task, using
   the self-tracked usage ledger from day one.
3. Add Head (with MCP tools) once dispatch/report works.
4. Add Reviewer + the auto-trigger-on-done rule.
5. Add Docs + the router fallback logic.
6. Dashboard last — it's pure observability, nothing depends on it existing yet.

Let me know what to change before this turns into actual code.
