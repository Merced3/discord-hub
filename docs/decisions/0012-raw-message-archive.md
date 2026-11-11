# 0012. Capture Discord as an append-only source archive

Date: 2026-11-11

## Status

Accepted for implementation; owner selected an authenticated hub read API over
direct consumer database access.

## Context

The Documented Self needs an owned source record: `Reality → Capture →
Immutable Source Record → Context → Interpretation → Outputs`. The roadmap had
kept archive storage out of scope; the owner explicitly brought it in scope.
The initial capture boundary is an allowlist of personal channels and Judgment
forums, not the entire guild.

## Decision

- Capture is independent of channel registration and consumer callbacks. Exact
  gateway JSON is retained as `jsonb`; backfill retains Discord's REST JSON.
  UTC timestamps, Discord IDs, author/channel/thread provenance, and capture
  time are stored in Postgres.
- `archive_events` is append-only: create, every edit (A→B→C), deletion,
  🔄/🔒/⭐ reaction add/remove, and thread/forum-post lifecycle. The current
  message projection is reproducible capture state, not an interpretation.
- Attachment bytes are immutable R2 objects keyed by message/attachment ID and
  SHA-256. Postgres holds filename, size, media type, checksum, and object key.
  There is no automatic retention deletion.
- Only configured parent channel/forum IDs are captured. Threads inherit their
  parent. Adding a channel or granting it to a reader is an explicit owner
  configuration change, never inferred from registration.
- Consumers use authenticated `/archive` endpoints with per-project channel
  grants. Raw transport payloads are not exposed downstream. If any user's
  active 🔒 reaction marks a message, all of its messages, events, and
  attachment reads are excluded. Removing the last lock makes it readable again.
- Backfill is operator-triggered, bounded, newest-first, idempotent, and
  checkpointed. Forums enumerate active and bounded archived posts and backfill
  each post thread. Repeat calls resume rather than duplicate creates/reactions.

## Principle #1

This does not make the hub interpret conversations. Discord decoding, durable
source capture, provenance, object storage, and permissioned transport of that
record are transport-adjacent caretaker functions. Summaries, tags beyond the
literal marker emoji, beliefs, decisions, and all other meaning remain consumer
responsibilities. Existing signed callback bodies and routing are unchanged.

## Consequences and limits

- Discord cannot reconstruct edits or deletions that occurred before capture;
  backfilled edits therefore have `before_text: null`. A deletion observed
  before its message was backfilled is an event without invented content.
- Gateway capture while the hub is down is recovered by a later backfill only
  to the extent Discord still exposes the messages and current reactions.
- Capture failures are logged and do not block or mutate existing callbacks.
  The archive is not represented as exactly-once gateway delivery; idempotent
  backfill is the reconciliation mechanism.
- R2 upload and Postgres persistence are not one distributed transaction. A
  failed capture can leave an unreferenced immutable object; reconciliation and
  lifecycle cleanup are future operational work, never automatic source deletion.
- The API exposes the sanitized source record, not AI retrieval, summaries,
  cross-channel interpretation, or public sharing. Those need separate design.
