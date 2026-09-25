# AGENTS.md — orientation for AI sessions

You are working on **discord-hub**, a standalone, always-on Discord bot
that is the *single* Discord connection for all of the owner's projects.
Other projects (socratic-partner, the teaching agent, etc.) contain **no**
Discord code — they talk to this hub over a local HTTP API.

## Non-negotiable design principles

1. **The hub knows Discord, not conversations.** Routing messages is in
   scope. Conversation rules (e.g. "one chat at a time") belong to the
   consuming project. Never bake a project's policy into the hub.
2. **Projects stay Discord-agnostic.** If a change would require a
   consuming project to import `discord` or know what an "intents" is,
   the change is wrong. Push that knowledge into the hub.
3. **Everything is permission-gated.** The hub may *propose* changes to
   server structure, but never applies them without explicit confirmation
   from the owner.
4. **Modularity over cleverness.** Features (messaging, voice, server
   caretaking) must be independently removable.
5. **Harness-embedded, not harness-supervised.** The hub installs the
   owner's automation-harness as a dependency and lets it own the
   long-running concerns: single-instance lock, graceful shutdown,
   persistent scheduling, structured logs, and status snapshots. The hub
   must not re-implement those itself.

## Tech stack

- Python 3.13+, `discord.py` for the Discord connection, FastAPI + uvicorn
  for the local API, `automation-harness` (sibling repo) for runtime
  lifecycle.
- Config via environment variables (see `.env.example`). Token comes from
  env, never from code or docs.

## Where things live

- `src/discord_hub/bot.py` — the discord.py bot (the connection itself)
- `src/discord_hub/api.py` — the FastAPI app other projects call
- `src/discord_hub/config.py` — settings from environment
- `src/discord_hub/main.py` — wires bot + API together, entry point
- `scripts/check_permissions.py` — reports granted/missing bot permissions
  per roadmap feature; run after any Developer Portal change
- `scripts/show_structure.py` — prints the server tree with channel IDs
- `docs/architecture.md` — how it works, concept-first
- `docs/api.md` — the HTTP contract for consuming projects
- `docs/roadmap.md` — what exists vs. what's planned
- `docs/integration-review.md` — two-consumer requirements, assessment,
  implementation evidence, boundaries, and handoff notes
- `tests/test_multi_client.py` — offline two-project regression suite;
  run with `python -m unittest discover -s tests -v`

## Repository boundaries

Only edit discord-hub in this session. The sibling socratic-partner and
teaching-agent repositories are read-only requirements sources, including
`docs/integrations/`. Record fulfillment/handoff notes here, not there.
Do not start live bots or mutate server state as part of automated tests.

## When picking up a new task

Read `README.md` first (the thesis), then `docs/roadmap.md` (current
state), then the module you're changing.
