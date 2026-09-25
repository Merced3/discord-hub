# 0004 — Scoped thread deletion; history reads without archival policy

**Status:** accepted by the owner during the two-consumer review.
**Revised:** the scope mechanism moved from a hub `.env` allowlist to a
per-registration flag (see below), at the owner's direction.

## Why

Both consumers need to remove development threads. The hub should expose the
Discord operation, not decide which application conversations or database rows
are disposable. The teaching agent also requests history access for external
backup/collection tools. Reading messages does not make the hub an archive.

## Deletion decision

- `DELETE /threads/{thread_id}` works only for threads whose **parent
  channel's registration** declared `allow_thread_deletion: true`
  (default `false`). The project opts in when it claims the channel — one
  config place, in the project's own registration call. The hub keeps no
  per-project values in its own configuration.
- Deregistering the channel revokes the capability automatically.
- The target must be a thread in the configured server; a parent channel
  itself can never be deleted through this endpoint.
- Consumers must obtain the user's confirmation for each deletion. The hub
  does not know what a lesson, conversation, or cleanup command means.
- Discord permissions still apply (Manage Threads); the hub never grants
  itself permissions.

This is declared intent, NOT independently verified consent. The API has no
caller authentication: any local caller could register with the flag set.
The real guardrail is the consumer asking the owner before each deletion;
the flag exists to make intent explicit and prevent accidents. If untrusted
clients or remote API access are ever introduced, add authenticated/scoped
access before relying on this boundary.

## Failure contract

204 means Discord acknowledged deletion. 403 means the parent's registration
didn't opt in (or is gone), the thread is outside the server, or Discord
permissions are missing. 404 means missing or wrong resource type. 502 means
Discord rejected/failed the upstream operation in another way. A repeated
delete may return 404; consumers can reconcile an already-missing thread as
appropriate. No retries, bulk cleanup, or deletion of application records is
hidden inside this operation.

## History decision

`GET /channels/{channel_id}/history` returns bounded newest-first pages for a
text channel or an individual thread in the configured server. `before` is an
exclusive message-ID cursor. Projects own pagination jobs, storage, consent,
retention, and attachment downloads. Reading a parent does not recursively
include threads; reading a forum requires choosing an individual post/thread.

A metadata export is not a lossless server backup. Attachment URLs can expire;
message IDs and original authorship cannot be recreated by replaying JSON.

## Deferred

Single-message deletion is optional in the request and remains unimplemented.
Decide its safe ownership rules (bot vs. hub-managed webhook) before adding it.
Neither operation is an invitation for autonomous server restructuring.
