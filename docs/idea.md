# Multi-Agent Coding Harness — idea.md

Status: Brainstorm locked. No deadline — building this as unhurried infrastructure.
Scope: General-purpose / reusable across any of Garv's projects, not tied to one repo.
Platform: Native Windows (PowerShell/CMD), no WSL2.

This document captures *what* the system is and *how the pieces connect*.
It intentionally does NOT contain file layouts, schemas, or code — that's implementation.md,
written after this is reviewed and approved.

---

## 1. The five terminals

All five run as **visible terminal windows** Garv opens himself. None of them require Garv
to type into them directly — each is driven by a small wrapper script that owns a CLI tool
as a subprocess and feeds it instructions programmatically (stdin), while Garv watches the
live output scroll by.

| Role | Engine | Budget pool |
|---|---|---|
| **Head** | Claude Code (Anthropic) | Claude Pro subscription |
| **Backend** | Claude Code (Anthropic) | Claude Pro subscription |
| **Frontend** | Claude Code (Anthropic) | Claude Pro subscription |
| **Reviewer** | Google Antigravity CLI (`agy`), Gemini model | Google Pro plan |
| **Docs** | GitHub Copilot CLI (`copilot`) | Copilot Premium |

**Fallback (both Reviewer and Docs only):** the $550 agent-router pool, restricted to
**Opus 5 or GPT-5.6 sol only**. DeepSeek-v4-flash and GLM-5.3 are permanently excluded from
touching any code or repo content — Garv doesn't trust that data exposure. Head/Backend/Frontend
have **no model fallback at all** — see §4.

### Head
- The only terminal Garv ever talks to. It's his single point of contact with the whole system.
- Built as a real Claude Code session with a **custom local MCP server** giving it actual tools:
  `dispatch_task`, `check_worker_status`, `get_reviewer_feedback`, `escalate_to_garv`,
  `flag_ready_to_commit`, plus whatever else implementation turns out to need.
- **Event-triggered, not a watch-loop.** It wakes up only when something needs a decision —
  breaking down a new prompt, a worker finishing or getting stuck, a reviewer verdict coming
  back, a genuine ambiguity that needs Garv's input. It never sits there polling.
- On a new prompt from Garv: analyzes it, decides which of Backend/Frontend (or both) are
  actually needed, dispatches sub-tasks to those, and leaves the rest idle.
- If Garv sends a new prompt while Head is mid-task, it queues in order rather than interrupting.
- Manages Claude's own usage limits — see §4.

### Backend / Frontend
- Full agentic coding sessions. Work in separate folders/files by convention (see §3), so no
  merge conflicts arise from running concurrently.
- Receive sub-task instructions auto-piped into their subprocess by the coordinator — Garv
  never types into these windows.
- When a sub-task is marked done, Reviewer is triggered automatically (see below) — the worker
  doesn't decide this itself.
- On reviewer feedback that isn't a pass, the same worker gets the feedback piped back in and
  keeps iterating until Reviewer signs off.

### Reviewer
- Fires **only** when a worker marks its sub-task done — never on every save, never on every commit.
- Job: read the diff, run the project's test suite, and send structured feedback straight back
  to the worker that produced the change. Loops until it passes.
- Reports final pass/fail status up to Head once satisfied.
- No write access to code — it only reads and reports. (If this turns out to be too limiting in
  practice, that's a call to revisit in implementation, not something assumed now.)

### Docs
- Updates markdown documentation in the repo, line by line, based on what changed elsewhere.
- Repo-local only — no Google Drive sync.

---

## 2. Coordination layer

A small local coordinator process (FastAPI + websockets, `localhost` only) is the hub every
wrapper script talks to. This avoids any OS-specific IPC — plain loopback networking works
identically on native Windows, so nothing here depends on WSL, tmux, or named pipes.

Responsibilities:
- Receives Garv's prompts (via Head) and queues them if Head is busy.
- Routes Head's `dispatch_task` calls to the right worker's subprocess stdin.
- Tracks live status per terminal (`idle` / `working` / `blocked` / `done` / `paused-quota`)
  in shared state (SQLite or a JSON store — implementation detail).
- Routes Reviewer's feedback back to the worker that needs it.
- Logs everything — every dispatch, every status change, every reviewer verdict — both for the
  dashboard and as an audit trail (this is the main lever against silent hallucination: nothing
  happens that isn't logged and traceable).

---

## 3. Git and file ownership

**No agent ever runs `git commit` or `git pull`.** Garv does every git operation himself, by
hand, when Head tells him it's ready (surfaced both in Head's chat reply and as a flagged item
on the dashboard).

Because of this, there's no need for git worktrees, per-agent branches, or a merge queue —
agents avoid collisions simply by working in separate folders/files (backend code, frontend
code, docs) at the same time.

---

## 4. Claude usage management (the part that protects Garv's subscription)

Head, Backend, and Frontend all draw from the same shared Claude Pro pool (5-hour rolling
window + weekly cap, shared across every Claude surface). Running three concurrent Claude Code
sessions is a real risk to that budget — this is the one part of the design that needs active
management rather than just "hope it's fine."

- Claude Code exposes live rate-limit data (5-hour and weekly usage) to hook/status-line
  scripts during an active session. The coordinator captures this as a side effect of Head's
  own session running.
- When a terminal (Head, Backend, or Frontend) gets close to its cap, Head **checkpoints that
  terminal's current task, pauses it, and tells Garv** — no swapping to another model, since
  Garv doesn't want anything but Anthropic's own models touching the main dev work.
- Once the window/week resets, Head resumes the paused task automatically, picking up where
  it left off.
- Reviewer and Docs are exempt from this problem entirely, since they're on separate budgets
  (Google Pro / Copilot), with the router as a shared overflow if either of those runs dry.

Caveat worth flagging honestly: the rate-limit data Head relies on rides on Claude Code's
internal plumbing (not a formally guaranteed public API). If Anthropic changes that plumbing,
this piece may need a small fix later — it isn't a permanent guarantee, just the best real
mechanism available today.

---

## 5. Dashboard

A custom web app (not Streamlit — Garv wants more UI control than that gives). Shows:
- Live status of all 5 terminals (idle/working/blocked/done/paused-quota)
- The current task queue
- Tailed logs per terminal
- "Ready to commit" flags
- Usage/spend visibility across all four budget pools (Claude Pro %, Copilot credits, Gemini
  usage, router $ spent)

---

## 6. Still open (Garv's call, not blocking this doc)

- Exact Gemini model for Reviewer (Flash vs Flash-Lite vs something newer by the time this is
  built) — Garv is choosing this himself.
- Project name for the harness itself.
- Exact per-project config format (repo path, folder→role mapping) — this is implementation.md's
  job, not this doc's.

---

## Next step

Once Garv confirms this matches his intent, this gets followed by **implementation.md** —
the actual technical spec (wrapper script design, MCP tool schemas, coordinator API shape,
state storage, dashboard tech breakdown) — before any code gets written, with explicit
approval gates along the way.
