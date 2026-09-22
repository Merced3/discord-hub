# 0003 — Project-owned slash commands, answered by callback response

**Status:** accepted

## Context

socratic-partner's entire control surface is slash commands (`/status`,
`/ask-now`, `/done`, `/interval`, ...). The hub originally declared slash
commands out of scope, which made migration impossible. The challenge:
support them without the hub learning what any command *means*.

## Decision

Projects **declare** their commands over the hub API
(`PUT /commands`: name, description, typed options). The hub registers
them with Discord (guild-scoped) and routes invocations to the declaring
project's callback. Command names are unique across projects; conflicts
are refused with `409`, same as channel registrations.

**The callback's HTTP response IS the interaction response.** Discord
requires an initial response within 3 seconds, so instead of a separate
"respond" endpoint, the hub delivers the invocation as a POST and waits
(up to 2.5s) for the project's response body:

- `{ "text": "...", "ephemeral": true }` → hub sends it as the response
- `{ "defer": true, "ephemeral": true }` → hub defers ("thinking...");
  the project later calls `POST /interactions/{id}/followups`

If the callback errors or times out, the hub auto-defers so the
interaction survives and the project can still follow up.

## Why

- The hub stays agnostic: it knows command *shape* (names, options,
  types) and routing, never command *meaning*.
- One round trip keeps the 3-second Discord budget explicit and visible
  to the project — no hidden retries, no orphaned interactions.
- Declaration via API means no hub redeploy when a project adds a
  command; commands persist in `data/commands.json` on keyed by project.

## Consequences

- Interaction responses appear under the **bot's** identity (Courier);
  webhooks can't answer interactions. Accepted — commands are the bot's
  surface; per-project identity applies to channel messages (ADR 0002).
- The bot's invite needs the `applications.commands` scope.
- Timestamps: projects render relative times with Discord's markdown
  (`<t:UNIX:R>`), documented in api.md — no hub feature, no discord.py
  needed in projects.
