# 0002 — Per-project message identity via webhooks

**Status:** accepted

## Context

ADR 0001 consolidated everything into one bot, which means one name and
avatar ("Courier") on every message — even though messages originate from
distinct projects (Socrates, StratForge, the teaching agent). The owner
wants each project's messages to be visually distinguishable in Discord.

Discord webhooks allow exactly this: a webhook posts into a specific
channel, and each POST can override the display name and avatar. No extra
bots, no extra tokens.

## Decision

When a project registers a channel (`POST /registrations`), the hub
creates (or reuses) a **webhook in that channel** and uses it for that
project's outbound messages there, with the project's display name and
avatar. Plain bot sends remain the fallback for unregistered channels.

## Why

- Restores per-project identity without giving up the single-bot,
  single-token architecture from ADR 0001.
- Webhooks are channel-scoped, which matches the hub's routing model
  (channel ownership) — one webhook per project per channel.
- The hub manages webhook lifecycle (create, cache, clean up on
  deregistration); projects just supply `display_name` and optionally
  `avatar_url` at registration time.

## Consequences

- The bot needs the **Manage Webhooks** permission — add it when this
  feature lands, not before (per our grant-permissions-as-features-land
  rule).
- Webhook sends can't do everything bot sends can (e.g. some message
  types). Fallback to plain bot send when a capability gap is hit.
- Registration payload grows two optional fields; the API contract
  update happens with the registrations endpoint itself.
