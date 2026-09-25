# 0004 — Scoped thread deletion; history reads without archival policy

**Status:** accepted by the owner during the two-consumer review.

## Why

Both consumers need to remove development threads. The hub should expose the
Discord operation, not decide which application conversations or database rows
are disposable. The teaching agent also requests history access for external
backup/collection tools. Reading messages does not make the hub an archive.

## Deletion decision

- `DELETE /threads/{thread_id}` is disabled by default.
- The operator pre-approves **parent channels** in the hub's configuration:
  `THREAD_DELETION_ALLOWED_PARENT_IDS=[123456789012345678]`.
- The target must be a thread in the configured server beneath an approved
  parent. A parent channel itself can never be deleted through this endpoint.
- Consumers must obtain the user's confirmation for each deletion. The hub
  does not know what a lesson, conversation, or cleanup command means.
- Discord permissions still apply (Manage Threads); the hub never grants itself
  permissions or enables an allowlist based on a registration request.

This is a trusted-local-consumer policy, NOT independently verified consent.
The API currently has no caller authentication. Any caller able to access it
can delete an allowed thread. If untrusted clients or remote API access are
introduced, add authenticated/scoped access before relying on this boundary.
Binding to localhost is important but not an authentication system.

## Failure contract

204 means Discord acknowledged deletion. 403 means disabled, outside the
allowlist/server, or missing Discord permissions. 404 means missing or wrong
resource type. 502 means Discord rejected/failed the upstream operation in
another way. A repeated delete may return 404; consumers can reconcile an
already-missing thread as appropriate. No retries, bulk cleanup, or deletion
of application records is hidden inside this operation.

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
