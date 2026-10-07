# 0008 — Adaptive server-tier limits, looked up live

**Status:** accepted (supersedes the flat-cap part of ADR 0007)

## Context

ADR 0007 set a deliberate **flat** 10 MiB attachment cap, arguing a single
number is a stable contract and tier detection wasn't worth it. The owner
then asked for the opposite posture: when the server gets boosted, *every*
limit that Discord raises should follow automatically — no code edits, no
restarts, and no per-feature coupling to what a "boost level" means.

ADR 0007's stability argument still matters; what changed is that the
stability can live in **one table** instead of one constant per feature.

## Decision

**One capabilities module, checked at request time.**
`src/discord_hub/capabilities.py` holds `TIER_LIMITS`: limit name →
{tier: value}. The bot reads the guild's boost tier **live** from
`guild.premium_tier` (Discord reports it; no polling, no cache) and
features call `bot.tier_limit("name")` exactly when they need to enforce
a limit. A boost — or an expired boost — takes effect on the very next
request.

**Features are decoupled from tiers.** A feature knows a limit *name*
(`"attachment_bytes"`), never tier arithmetic. What tier 2 *means* for
uploads exists only in the table.

**Unlisted tiers inherit the base.** A tier without an entry falls back
to the tier-0 value (tier 1 does not raise Discord's upload cap, for
instance), and an unknown guild (bot not ready yet) is treated as tier 0.
Limits can only be intentionally raised, never accidentally dropped.

**Not everything is tier-scaled — the table is honest about that.**
Bot message text length is 2000 characters on *every* tier (the 4000-char
perk is Nitro for human users), so message chunking stays a constant in
`bot.py` and is explicitly *not* in the table, with a comment saying why.

## Consequences

- Raising or adding a limit (Discord re-prices, new Discord feature, a
  new hub feature with a tier-scaled limit) is one table entry, not a
  code hunt.
- The 413 contract is unchanged in shape; the number in the message now
  reflects the live cap, and the log line records the tier it saw.
- The API doc states the cap as "10 MiB base, scales with server boost
  tier" — consumers preflight against the base to stay safe everywhere.
