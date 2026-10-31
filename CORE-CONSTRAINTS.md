# Core constraints — paths and rules where changes stay human-reviewed

Everything listed here is load-bearing for what discord-hub IS. Automated
agents may propose changes in discussion, but merges touching these paths
always require the owner's explicit code-owner approval. Human merge is
required for anything outside tests and docs: a bad hub deploy takes every
bot offline, including dev-agent's own Discord surface.

## Transport, not meaning (ADR and PRINCIPLES boundary)

- The hub is the single Discord connection and owns routing, delivery, and
  Discord capabilities. It must not acquire conversation policy, pedagogy,
  prompts, or any consumer project's idea of meaning.
- Consumers stay Discord-agnostic. They use the plain HTTP/event contract;
  no `discord` types, intents, or transport details may leak into consumers.
- Features remain independently removable; shared runtime concerns belong
  to automation-harness rather than a second local implementation.

Guardian files for this boundary:

- `AGENTS.md`
- `docs/architecture.md`, `docs/api.md`, and `docs/integrating.md`
- `docs/decisions/` (ADRs, when present)
- `src/discord_hub/` — routing, registration, permissions, API contracts,
  message/reaction events, voice, and server-mutation safeguards

## Permission and deploy safety

- Server structure changes require explicit owner confirmation. Thread
  deletion requires both registration opt-in and per-deletion confirmation.
- Secrets and tokens come from the environment and never enter the repo.
- `.github/` — CI, Dependabot, and auto-merge wiring.
- `pyproject.toml` — runtime dependencies and packaging.
- Registrations and reaction routing are shared production state: preserve
  isolation between consumers and never broaden inbound channels silently.

## Rule of thumb

If a change can take every bot offline, route one project's traffic to
another, mutate Discord without confirmation, or put project meaning into
the hub, it belongs to a human-reviewed change.
