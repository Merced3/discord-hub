# Two consumers: integration review and handoff

## The thesis

The hub should provide reusable Discord operations, not learn how to teach,
ask reflective questions, close conversations, or delete application records.
Two consumers now make the shared needs concrete. Their requests are inputs
to design, not instructions to copy unquestioningly.

## Source requests (consumer repositories are read-only here)

- `../socratic-partner/docs/integrations/discord-hub.md`: thread deletion.
- `../teaching-agent/docs/integrations/discord-hub.md`: thread/message
  deletion, history reads, voice transport, speaking events, and multi-client
  verification.

Consumer integration files are not edited by this session. Their own sessions
can mark requests fulfilled after checking the hub contract and evidence.

## Agreed order

1. Two-project reliability and regression tests.
2. Safe deletion and paginated history reads.
3. Voice feasibility, ownership, and bidirectional transport.

## First slice: implemented and tested offline

`tests/test_multi_client.py` uses two fake projects, real local HTTP callbacks,
temporary stores, and mocked Discord operations. It never reads the real
`.env`, starts Courier, or edits live registrations.

Coverage:

- Two registered channels route to their respective callbacks only.
- Threads inherit the correct parent's route; unregistered traffic and bots
  are ignored.
- Channel and thread sends use the intended webhook, display name, and avatar.
- Conflicting channel registrations return 409 without creating a stray webhook.
- Concurrent claims create only one webhook.
- Distinct command sets coexist; replacing/removing one leaves the other intact.
- Command invocations route to their declaring callback; name collisions return 409.
- A slow, failing, or unreachable callback does not block the other project's
  ordinary message delivery in these tests.
- Failed command sync preserves local routes and saved declarations.
- Failed registry writes roll back local changes; newly created webhooks are
  cleaned up when registration cannot be saved.

Implementation changes:

- Ordinary message callbacks have a five-second total deadline and no redirects.
  Consumers should acknowledge quickly and run slow work in the background.
- Callback failures are logged; there is no automatic retry or durable queue.
- Registration changes and guild command syncs are serialized separately.
- Command candidates are built before publication; saved command declarations
  change only after Discord sync succeeds. JSON stores use replace-on-save.

**Evidence boundary:** this is automated offline integration coverage, not a
new two-project live Discord acceptance run. Mocked Discord operations do not
prove real permissions, webhook rendering, or Discord rate-limit behavior.
There are no per-project delivery queues, concurrency quotas, or crash-durable
callback delivery guarantees. A sustained flood can still exhaust shared resources.

Command sync cannot be transactional across Discord and local disk: a network
timeout may leave the remote result unknown, or a disk failure may follow a
successful remote update. Local routes remain on the old set on failure; the
next successful full sync reconciles Discord. Do not describe this as an
exactly-once distributed transaction.

## Next: deletion and history (not implemented yet)

- Thread deletion is a reusable primitive; projects own confirmation UX and
  application cleanup. Decide the hub's explicit approval/scope boundary before
  enabling destructive calls. Never delete real threads just to test an endpoint.
- Registrations are routing claims, NOT authenticated project identities. The
  current API trusts local callers. No cross-project security guarantee exists.
- Message deletion should initially target Courier's own or hub-managed webhook
  messages, not arbitrary users' content.
- History should be bounded, paginated, ordered, and read-only. The hub returns
  message data; consumers decide what to archive and where.
- Attachment metadata is not a file backup. JSON export cannot recreate message
  IDs, original authorship, or all Discord history. It is not a lossless move.
- The hub must not coordinate database deletion in another project. Consumers
  need a recoverable cleanup workflow for partial failures themselves.

## Later: voice (not implemented yet)

- Validate current library support for receiving/decrypting Discord voice before
  committing to a transport implementation.
- One bot normally has one voice connection per guild. Use explicit ownership
  so a second client cannot silently move the connection or interrupt playback.
- Stream per-speaker audio with explicit format, timing, disconnect, and
  backpressure rules. Utterance buffering, STT, LLM, TTS, and interruption policy
  stay in consumers.
- Speaking activity is a transport signal, not a guaranteed utterance boundary.
- Do not accept arbitrary server-local file paths as a remote playback API.

## Running the checks

From the hub folder, after installing the sibling harness as in README:

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m unittest discover -s tests -v
```

No live Discord token or running consumer is needed. No consumer repo is changed.
