# File attachments: needs evaluation (2026-10-05)

> **Dated review record, not a roadmap item.** The task was "add outbound
> file sending"; the gate was PRINCIPLES #3 (build shared machinery on the
> second real need, not the first). The gate said **not yet**. Nothing was
> built. If a second need appears, this record is the starting point —
> the design sketch below captures what was already thought through.

## The ask

Let consumers attach files inline in Discord messages (a sibling endpoint
to `POST /messages` or an extension of it), Discord-agnostic at the hub
boundary, respecting the webhook identity path, with size-limit behavior
documented in an ADR.

## Evidence gathered

**The one stated motivation is a convenience, not a blocker.**
Teaching-agent's `/lecture` episodes are served as Tailscale links posted
as ordinary text (links already auto-render via `POST /messages`), and
that path is **live-verified 2026-10-05** in the 🧪 LAB channels
(`../teaching-agent/docs/current-state.md`). Inline upload would be
nicer on a phone, but nothing is waiting on it.

**No second consumer need exists in writing.**

- `../socratic-partner/docs/integrations/discord-hub.md` — asks for
  thread deletion only; its "Declined" section records no file asks.
- `../teaching-agent/docs/integrations/discord-hub.md` — open requests
  are history export, deletion primitives, multi-client verification.
  No attachment request.
- voice-bridge is a transport-only sibling, not a hub consumer of
  messaging; nothing about file sends in its docs.

**What already works without the feature:**

| Want | Existing path |
| --- | --- |
| Share a lecture episode | Tailscale URL in plain text (live-verified) |
| Share any file reachable by URL | URL in plain text; Discord renders it |
| Read attachments inbound | `GET /channels/{id}/history` already returns attachment metadata |

## Verdict: not yet

PRINCIPLES #3 is explicit: one project needing something proves nothing
about generality. There is one convenience-grade need, zero blocked
consumers, and zero second needs. Building now would also front-load the
ADR-required size-limit policy and a multipart contract before any
consumer has constraints to validate them against.

Reopen when a *second* real project needs to attach a file, or when a
*first* project becomes actually blocked (e.g. a phone workflow where
tapping a Tailscale link demonstrably fails). Record the new need here
or in a fresh review and reassess.

## Design sketch (for the future session, not built)

Captured so the thinking isn't lost; all of it is provisional until a
real second need supplies real constraints.

- **Boundary shape:** multipart POST to `/messages` (fields: `channel_id`,
  optional `text`, optional `reply_to_message_id`, one or more file
  parts) or a sibling `POST /messages/attachments`. Bytes over the wire,
  never file paths — the hub never reads a consumer's disk (same rule
  as `/voice/play`).
- **Webhook routing:** Discord webhooks *do* support file uploads, so
  registered channels keep identity via `webhook.send(file=...)`.
  `reply_to_message_id` forces the plain bot path, same as text.
- **Size limits (needs its ADR when built):** Discord caps uploads at
  the guild's boost tier (10 MiB free tier as of writing) for bot
  uploads; webhooks historically 8 MiB. The honest contract is: hub
  enforces a flat documented cap (e.g. 10 MiB), rejects larger with
  `413` and a clear message, and consumers keep using links for big
  files — which is exactly what teaching-agent already does.
- **Lab-first verification:** 🧪 LAB category (#lab-text) before any
  real channel (#learning, #reflection), per the standing testing rule.
