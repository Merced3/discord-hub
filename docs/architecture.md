# Architecture

Concept-first. Jargon only where unavoidable.

## The big picture

```
                        ┌─────────────────────────────┐
 Discord servers ◄────► │         discord-hub         │
   (the real Discord)   │                             │
                        │  bot.py ── one connection   │
                        │  api.py ── one local API    │
                        └──────────┬──────────────────┘
                                   │  HTTP on localhost:8100
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
      socratic-partner     teaching agent        (future projects)
      "send this message"  "join voice, play     "what does my
                            this audio"           server look like?"
```

Two halves, one process:

- **The bot half** (`bot.py`) holds the actual Discord connection. It is
  the only thing in the entire ecosystem that knows the Discord library.
- **The API half** (`api.py`) is a small HTTP server on localhost. This is
  the *only* face other projects ever see.

`main.py` runs both halves together in one process.

## Why a service and not a shared library

A shared library would deduplicate code, but every project would still run
its own bot, hold its own token, and open its own connection — and Discord
voice channels make that painful fast (multiple bots fighting over one
voice channel). A single service means one bot, one token, one connection,
and it makes the "24/7 caretaker that sees the whole server" idea fall out
naturally, because something is always running and always watching.

## Message flow, both directions

**Outbound** (project → Discord): the project POSTs to the hub's API, the
bot half sends it. Simple request/response.

**Inbound** (Discord → project): when a message arrives, the hub looks up
which project registered interest in that channel, then delivers the
message to that project's callback URL. Registration is explicit: a
project says "messages in channel X belong to me" via
`POST /registrations`. The hub does not guess, and it does not decide
what the message *means*.

## The routing rule

The hub routes by **channel ownership**, nothing more. If two projects
both want the same channel, that's a configuration decision the owner
makes explicitly — the hub will refuse ambiguous registrations rather than
pick a winner.

## Tier-scaled limits: check the table, never hardcode

Some Discord limits scale with the server's boost tier (upload size,
today; others as Discord adds them). The hub's rule (ADR 0008): limits
live in **one table** — `capabilities.py`'s `TIER_LIMITS`, keyed by limit
*name* — and a feature calls `bot.tier_limit("name")` **at the moment it
needs to enforce the limit**. The tier is read live from
`guild.premium_tier`, never cached, so a boost (or an expired one) takes
effect on the next request with no restart and no per-feature code.

Consequences of the pattern:

- Features know limit *names* (`"attachment_bytes"`), never tier
  arithmetic. What "tier 2" means for uploads exists only in the table.
- Unlisted tiers inherit the tier-0 base, and an unknown guild (bot not
  ready) is treated as tier 0 — limits only ever rise intentionally.
- Adding a limit is one table entry plus one call site. If a limit does
  **not** scale with tier (bot message length is 2000 chars on every
  tier), it stays an ordinary constant elsewhere, with a comment saying
  why — the table is an honest record of what Discord actually scales.

## Runtime lifecycle

Long-running concerns — single-instance lock, graceful shutdown on
SIGINT/SIGTERM, persistent schedules, structured JSON logs, and a
`status.json` snapshot — are provided by embedding the owner's
**automation-harness** as a dependency (the same pattern as every other
automation in the ecosystem). The hub implements none of that itself;
it only supplies the workflow (bot + API) that the harness keeps alive.
