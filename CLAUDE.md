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

Not yet established — this is a from-scratch project with no scaffolding yet. Add build/test/
run commands here as soon as they exist, so future sessions don't have to rediscover them.

## Git

Confirm with Garv before running `git commit` or `git push` on this repo, same as he'd want
on any project — don't assume silent commits are fine just because this is tooling rather
than a client project.
