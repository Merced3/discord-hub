# API contract (draft)

This is the contract consuming projects rely on. It is intentionally
small and Discord-free: no Discord types, no Discord vocabulary beyond
"channel" and "message".

Base URL: `http://localhost:8100` (configurable).

## Implemented

### `GET /health`
Liveness check for supervisors (automation-harness).

**Response `200`**
```json
{ "status": "ok", "discord_connected": true, "uptime_seconds": 1234 }
```

### `GET /server/structure`
Snapshot of how the server is laid out — categories, channels (text and
voice), and roles. Powers the "caretaker" view of the server.

**Response `200`** (shape illustrative)
```json
{
  "guild": "My Server",
  "categories": [
    {
      "name": "General",
      "channels": [
        { "id": "123", "name": "chat", "type": "text" },
        { "id": "124", "name": "Lounge", "type": "voice" }
      ]
    }
  ],
  "roles": ["admin", "bots"]
}
```

### `POST /messages`
Send a text message.

**Request**
```json
{ "channel_id": 123, "text": "hello" }
```

**Response `201`**
```json
{ "id": "456", "channel_id": "123" }
```

Errors: `404` channel unknown or not a text channel, `403` bot lacks
permission in that channel.

Optional `"reply_to_message_id": "456"` makes the message a reply to an
existing message (forces the plain bot path — webhooks can't reply).
Long texts are automatically chunked to fit Discord's limit.

If the channel is registered (below), the message is sent through the
channel's registration webhook — identity belongs to the *channel's owner*,
not the caller (the hub trusts local callers; it does not authenticate who
sent a given request). The response gains a `"via": "webhook"` field.

### `POST /registrations`
Claim a channel for a project. Creates a webhook in the channel for the
project's identity (ADR 0002). Registration persists across hub restarts.

**Request**
```json
{
  "channel_id": 123,
  "callback_url": "http://localhost:9000/discord",
  "display_name": "Socrates",
  "avatar_url": "https://example.com/socrates.png",
  "allow_thread_deletion": true
}
```
`display_name` and `avatar_url` are optional. `allow_thread_deletion`
(default `false`) opts the channel's threads into `DELETE /threads/{id}`;
deregistering revokes it.

**Response `201`** — the registration, including the created `webhook_url`.

Errors: `409` channel already claimed by another project (the hub refuses
ambiguity rather than picking a winner), `404` unknown channel, `403`
bot lacks Manage Webhooks. A duplicate claim is rejected before creating a
webhook. Registrations route traffic; they do not authenticate API callers.

**Inbound delivery** — when a (human) message arrives in a registered
channel, the hub POSTs to the callback URL:
```json
{
  "message_id": "456",
  "channel_id": "123",
  "author": { "id": "789", "name": "cedgo" },
  "text": "hello",
  "created_at": "2026-01-01T12:00:00+00:00"
}
```

Callbacks must acknowledge ordinary messages within **five seconds**. Return
2xx promptly and do slow processing in the consumer's background worker.
Redirects are not followed. Timeout, connection failure, or non-2xx is logged;
there is currently **no retry or durable delivery queue**. Other message
callbacks may run concurrently; ordering and exactly-once delivery are not
promised.

**Threads inherit the parent channel's registration.** A message in a
thread of a registered channel is delivered to the same callback, with an
extra `thread` field so the project knows which conversation it was:
```json
{ "thread": { "id": "111", "name": "Session 2026-09-22", "parent_channel_id": "123" } }
```

### `POST /threads`
Create a public thread in a text channel — e.g. one thread per session,
keeping a channel's history as a clean index of conversations.

**Request**
```json
{ "channel_id": 123, "name": "Session 2026-09-22" }
```

**Response `201`**
```json
{ "id": "111", "name": "Session 2026-09-22", "parent_channel_id": "123" }
```

Send into the thread with `POST /messages` using the thread's `id` as
`channel_id` (webhook identity applies, inherited from the parent).

Errors: `404` unknown channel, `403` bot lacks Create Public Threads.

### `DELETE /threads/{thread_id}`
Delete one thread (including its Discord messages), never a parent channel.
Works only when the thread's **parent channel registration** declared
`"allow_thread_deletion": true` (ADR 0004). The target must belong to the
hub's configured server, and the bot needs Discord's Manage Threads
permission.

Consumers must ask the owner to confirm each deletion. The flag is declared
intent, not verified consent — the hub does not independently authenticate
callers. No consumer database records or local files are touched.

Responses: `204` deleted, `403` parent registration didn't opt in (or doesn't
exist), outside the configured server, or Discord permission denied, `404`
missing or not a thread, `502` other upstream Discord failure. A repeated
call can return `404`; no automatic retries or bulk deletion.

### `GET /channels/{channel_id}/history?limit=50&before=123`
Read one page from a text channel or individual thread in the configured server.
Requires View Channel and Read Message History. `limit` is 1–100 (default 50).
`before` is an optional **exclusive message ID**, not a timestamp.

**Response `200`** (illustrative):
```json
{
  "channel_id": "123",
  "messages": [
    {
      "id": "456", "channel_id": "123",
      "author": {"id": "789", "name": "User", "bot": false},
      "created_at": "2026-01-01T12:00:00+00:00", "edited_at": null,
      "text": "hello",
      "attachments": [
        {"id": "987", "filename": "notes.txt", "size": 42,
         "content_type": "text/plain", "url": "https://example.com/notes.txt"}
      ]
    }
  ],
  "next_before": null
}
```

Pages are **newest first**. When a page is full, `next_before` is its last
message's ID; pass it as `before` to fetch older messages. A full final page
may be followed by an empty page. Stop when `next_before` is null.
This is not a snapshot across requests; concurrent edits/deletions can change
history while a consumer paginates.

A parent-channel read excludes thread contents; read each thread separately.
Forum containers are unsupported; forum post/thread IDs are supported.
Attachments are metadata only, and URLs may expire. Embeds, reactions, and
complete message-type data are not exported. This is not a lossless backup.

Errors: `422` invalid query bounds, `404` missing/unsupported channel, `403`
outside configured server or lacking permissions, `502` upstream failure.

### `GET /registrations`
List all current registrations.

### `DELETE /registrations/{channel_id}`
Release a channel and delete its webhook. `204` on success, `404` if the
channel was not registered.

### `PUT /commands`
Declare a project's slash commands (ADR 0003). Replaces the project's
whole set; the hub registers them with Discord and routes invocations to
`callback_url`. Command names are unique across projects.

**Request**
```json
{
  "callback_url": "http://localhost:9000/discord",
  "commands": [
    {
      "name": "interval",
      "description": "Set hours between conversations.",
      "options": [
        {
          "name": "hours",
          "description": "Whole hours from 1 to 720.",
          "type": "integer",
          "required": true,
          "min_value": 1,
          "max_value": 720
        }
      ]
    }
  ]
}
```
Option `type`: `integer` | `string` | `boolean` | `number`.

**Response `200`** — `{ "synced": 1 }`. Errors: `409` name owned by
another project, `403` bot invite lacks the `applications.commands` scope,
`422` duplicate names within a declaration or invalid command construction.
Updates are serialized and keep other projects' commands. Saved declarations
change after successful Discord sync. On sync failure local routes remain
unchanged; a later full sync reconciles any uncertain remote outcome.

**Invocation delivery** — when a user runs a command, the hub POSTs to the
callback:
```json
{
  "type": "command",
  "interaction_id": "999",
  "command": "interval",
  "options": { "hours": 6 },
  "channel_id": "123",
  "user": { "id": "789", "name": "cedgo" }
}
```

**The callback's HTTP response IS the interaction response** (Discord
allows ~3 seconds):
- `{ "text": "Interval set.", "ephemeral": true }` → shown as the response
- `{ "defer": true, "ephemeral": true }` → "thinking..."; follow up later

On callback error/timeout the hub auto-defers so the interaction survives.

### `POST /interactions/{interaction_id}/followups`
Send a follow-up after deferring. Body: `{ "text": "...", "ephemeral": true }`.
Interaction tokens live ~15 minutes; `404` if unknown or expired.

### `POST /channels/{channel_id}/typing`
Show "bot is typing..." for ~10 seconds. `202`.

### `GET /channels/{channel_id}/permissions`
The bot's granted permissions in a channel — preflight before promising
delivery.

## Rendering timestamps (no API needed)

Discord renders timestamp markdown natively — projects never need a
Discord library for this:

- `<t:1700000000:F>` → full date/time
- `<t:1700000000:R>` → relative ("in 3 hours")

Use a Unix timestamp; styles: `t T d D f F R`.

## Voice (ADR 0005)

One voice connection per server, explicitly owned. Owner = the project's
callback URL (same identity as registrations and commands).

### `GET /voice/status`
`{ "connected": bool, "owner": "..."|null, "channel_id": "..."|null }`.

### `POST /voice/join`
`{ "channel_id": 123, "owner": "http://localhost:9000/discord" }` → `200` with
status. Same owner may rejoin to move channels. `404` unknown/not a voice
channel, **`409` another project owns the connection**.

### `POST /voice/leave`, `POST /voice/stop`
`{ "owner": "..." }` → `204`. `403` not the owner, `404` no active connection.

### `POST /voice/play?owner=...`
Body is **raw PCM audio: s16le, 48 kHz, stereo** (`application/octet-stream`).
`202` accepted. Replaces anything currently playing (use `/voice/stop` to
interrupt explicitly). No file paths — the hub never reads a consumer's disk.

## Planned (not yet implemented)

### `WS /voice/stream`
Bidirectional per-speaker PCM plus speaking events
(`{type: "speaking", user_id, state}`). Blocked until a voice-recv release
ships DAVE decryption (ADR 0005); the contract is stable enough for
consumers to build against.

## Versioning rule

Additive changes (new endpoints, new optional fields) are always fine.
Changing or removing anything above requires a version bump and a note
here, because multiple projects depend on this contract.
