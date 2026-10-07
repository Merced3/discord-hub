# 0007 — Outbound file attachments: bytes over the wire, flat 10 MiB cap

**Status:** accepted — the flat-cap choice is superseded by ADR 0008
(adaptive tier lookup); the endpoint shape, bytes-over-wire, and
fail-before-upload decisions stand.

## Context

The 2026-10-05 review (`docs/reviews/2026-10-05-file-attachments-review.md`)
deferred this under PRINCIPLES #3: teaching-agent's `/lecture` need was
convenience-grade and already served by Tailscale links. A **second real
need** then arrived: the trading engine must send photos, logs, and JSON
files to Discord, and it must not care what the files *are* (agnostic
transport). The gate is passed; the sketch in the review is the starting
point.

Design questions: how consumers hand bytes to the hub, how attachments
interact with webhook identity (ADR 0002), and what happens above
Discord's upload caps.

## Decision

**One file per request, raw bytes.** `POST /messages/attachments` takes
the file as the raw request body (`application/octet-stream`) with
`channel_id` and `filename` as query parameters and optional `text` as
message content. Same precedent as `/voice/play`: the hub never reads a
consumer's disk, never sees a file path, and no `python-multipart`
dependency is added for a transport-only concern. Consumers wanting
several attachments send several requests.

**Webhook identity is preserved.** Discord webhooks support file
uploads, so in a registered channel the attachment goes through the
channel's registration webhook (display name, avatar, thread) exactly
like text sends. Unregistered channels fall back to the plain bot send.

**~~Flat 10 MiB cap~~ (superseded: ADR 0008 makes the cap follow the
server's live boost tier) — hard fail either way.** Discord's upload limit for bots on a
free-tier server is 10 MiB; boosted tiers raise it, and webhook limits
have historically differed. Rather than probe per-guild tiers, the hub
enforces one documented cap — **10 MiB total request body** — and
rejects anything larger **before touching Discord** with `413` and a
plain "too big" detail. The rejection is logged as a failure hub-side.
Consumers needing bigger files keep using links (the teaching-agent
pattern), which costs nothing and always works.

**No reply support.** `reply_to_message_id` is not offered on this
endpoint (webhooks can't attach-and-reply; the one consumer doesn't ask).
Additive later if a real need appears.

## Why

- **Agnostic by construction:** the hub transports bytes + a filename;
  whether the bytes are a chart PNG, a JSONL log, or anything else is
  the consumer's meaning (PRINCIPLES #1, #4).
- **Flat cap over tier detection:** a single number is a stable contract
  consumers can preflight against; tier-aware limits would change the
  API's behavior when someone boosts the server.
- **Fail before upload:** rejecting at the API boundary gives the caller
  an immediate synchronous answer instead of a Discord 400 deep in an
  async send, and keeps partial uploads off the server.

## Consequences

- Trading-engine and teaching-agent convenience use are both served;
  the feature stays general.
- If the server is boosted and the owner wants the higher limit, the cap
  is one constant (`MAX_ATTACHMENT_BYTES`) plus a contract-doc edit —
  deliberately cheap to change, deliberately not automatic.
- Large-file sharing via links remains a first-class pattern, not a
  fallback hack.
