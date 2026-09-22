# 0001 — A service, not a shared library

**Status:** accepted (project inception)

## Context

Multiple projects (socratic-partner, the teaching agent, StratForge,
others) each carry their own Discord code. We wanted one place for that
knowledge. Two options: an installable Python library each project
imports, or a standalone always-running service projects call over HTTP.

## Decision

A standalone service. One bot, one token, one Discord connection; other
projects call a local HTTP API and contain zero Discord code.

## Why

- **Voice decides it.** Discord voice channels are painful with multiple
  bots; the teaching agent's audio conversations want a single bot that
  owns voice.
- **The caretaker role falls out for free.** A 24/7 process connected to
  the whole server can report on and (with permission) reshape server
  structure; a library cannot, because nothing is running.
- **True agnosticism.** Library consumers would still need discord.py
  installed and a token configured. API consumers need neither.

## Consequences

- The hub becomes a piece of always-on infrastructure: if it's down,
  every project's Discord access is down. Mitigated by the
  automation-harness runtime (restart, health, status).
- Per-project bot *identity* (name/avatar on messages) is lost by
  default. Acceptable for now; Discord webhooks can restore per-project
  display names later if wanted.
- Older per-project bots (Ceds_Bot, Observer) stay alive until their
  projects migrate, then are retired.
