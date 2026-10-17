# 0010 — Hub API trust model on the droplet: no API key (for now)

Date: 2026-10-17
Status: accepted

## Context

The hub's HTTP API was designed around a localhost trust model: it binds
to the machine, and any local process may call it. The roadmap flagged
that this "breaks remotely" once the hub moved to the VPS.

On the droplet the API is reachable from exactly two places:

1. The Docker compose network — the only containers on it are ours
   (discord-hub, socratic-partner, teaching-agent, voice-bridge), defined
   in one compose file we control.
2. The Tailscale interface (compose binds `100.92.87.33:8100` plus
   `127.0.0.1:8100`). Tailscale is an authenticated WireGuard mesh; only
   devices enrolled in our tailnet can even open a connection, and the
   droplet exposes no public ports.

So although the network is no longer "localhost", every possible caller
is still a machine we own, authenticated at the network layer before any
HTTP happens.

## Decision

**No API key for now.** The Docker network + Tailscale boundary is the
authentication layer; a shared key would add a secret to rotate without
changing the effective trust set.

## Consequences

- Consumers keep calling the hub with no credentials; `docs/api.md`
  stays as written.
- The hub still treats registration flags (`allow_thread_deletion`,
  `allow_forum_moderation`) as *declared intent*, not caller
  authentication (ADR 0004) — that was already the model on localhost.
- Revisit this decision if any of these change:
  - a consumer ever runs outside the Docker network / tailnet,
  - a third-party or less-trusted service joins the compose network,
  - the hub starts serving more than one owner's projects.
