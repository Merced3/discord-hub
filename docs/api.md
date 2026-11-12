# API contract (draft)

This is the contract consuming projects rely on. It is intentionally
small and Discord-free: no Discord types, no Discord vocabulary beyond
"channel" and "message".

Base URL: `http://localhost:8100` (configurable).

## Authentication and ownership (ADR 0011)

Production uses a distinct bearer token for each project:

```http
Authorization: Bearer <project-token>
```

`GET /health` is public. When `HUB_AUTH_MODE=required`, every other HTTP
route and the voice WebSocket require a valid project or operator token.
Missing/invalid credentials get `401`; cross-project ownership failures get
`403`. WebSocket authentication uses the handshake header (never a token in
the URL). During migration, `optional` accepts tokenless legacy callers but
rejects invalid tokens; `disabled` retains pre-migration local behavior.
See [`auth-rollout.md`](auth-rollout.md).

Registrations and command routes have an authenticated project owner. An
operation on a registered channel, inherited thread, or forum post is
limited to its owner. This includes using its Discord webhook identity,
moderating it, and reading its history. Unregistered-channel bot operations
remain available to authenticated projects. Authentication adds no request
quota and does not replace Discord permissions or deletion consent.

Every hub callback to a known project includes these headers:

```http
X-Discord-Hub-Timestamp: <unix-seconds>
X-Discord-Hub-Signature: v1=<hex-hmac>
X-Discord-Hub-Project: <non-secret-project-id>
```

Verify HMAC-SHA256 with your project token over
`"<timestamp>.<exact-raw-body>"`, using constant-time comparison and a
five-minute timestamp window. Verify before parsing or acting on JSON.
Unsigned callbacks can exist only for unowned legacy routes during rollout.

Registration creation still returns the secret Discord `webhook_url`.
Registration lists and diagnostics omit it; consumers can list only their
own registrations. Never put API tokens or Discord webhook URLs in logs.

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
channel's registration webhook — identity belongs to the *channel's owner*.
Only that authenticated project may use the registered route; callers cannot
claim another project's identity. The response gains a `"via": "webhook"`
field.

### `GET /forums/{channel_id}/posts`
List posts in a forum channel (ADR 0009). Active posts come from cache;
`?include_archived=true` also paginates archived posts, bounded by
`?limit=` (1–100, default 50) so no request can crawl an old forum
unbounded.

**Response `200`** (shape illustrative)
```json
{
  "channel_id": "123",
  "posts": [
    {
      "id": "456",
      "title": "My post",
      "author": { "id": "789", "name": "Merced" },
      "tags": [{ "id": "111", "name": "holding" }],
      "created_at": "2026-10-08T17:00:00+00:00",
      "comment_count": 3,
      "pinned": false,
      "locked": false,
      "archived": false
    }
  ],
  "truncated": false
}
```
`truncated: true` means more archived posts likely exist beyond `limit`.
Errors: `404` not a forum channel, `403` bot lacks View Channel / Read
Message History.

### `POST /forums/{channel_id}/posts`
Create a post (ADR 0009).

**Request**
```json
{ "title": "My post", "text": "body", "tag_ids": ["111"] }
```
`title` 1–100 chars; `text` is chunked like regular sends — the first
chunk is the post body, the rest become comments. `tag_ids` must come
from the forum's `available_tags` (see `/server/structure`); unknown ids
get `422` before Discord is touched. Bot identity for now (`"via":
"bot"`); webhook identity arrives with ADR 0009 phase 3.

**Response `201`** — the post summary (same shape as the list endpoint,
plus `forum_channel_id` and `via`).

Errors: `404` not a forum channel, `403` missing Create Posts, `422`
unknown tag ids.

Commenting on a post is plain `POST /messages` with the post id as
`channel_id`; reactions, edits, typing, and attachments likewise reuse
the existing message endpoints.

In a **registered** forum, untagged post creation goes through the
project's registration webhook (`"via": "webhook"`, ADR 0002), with
fallback to bot identity if the webhook send fails. When `tag_ids` are
present the post is always bot-created — Discord cannot apply tags on a
webhook execute.

### `PATCH /forums/posts/{post_id}`
Moderation-class edits (ADR 0009). All fields optional; at least one
required.

**Request**
```json
{ "title": "new name", "tag_ids": ["111"], "archived": false, "locked": true, "pinned": true }
```
Gated: the parent forum must be registered with
`allow_forum_moderation: true` (`403` otherwise), and Discord's Manage
Threads still applies. Note Discord allows very few pinned posts per
forum (error 30047) — a failed pin surfaces as `502`.

### `POST /forums/{channel_id}/tags` · `PATCH /forums/{channel_id}/tags/{tag_id}` · `DELETE /forums/{channel_id}/tags/{tag_id}`
Administer the forum's available tag set (caretaker, ADR 0009). Create
takes `{ "name": "holding", "moderated": false }`; update takes optional
`name` / `moderated`. Tag ids are preserved across renames. Gated on
`allow_forum_moderation` plus Discord's Manage Channels. Tag names must
be unique per forum (`502` with Discord's detail otherwise).

### Forum lifecycle events
A forum registered with `forum_events: true` receives callback POSTs:

- `{ "type": "forum.post_created", ...post summary }`
- `{ "type": "forum.post_updated", "changed": ["name" | "tags" | "archived" | "locked" | "pinned"], ...post summary }`
  — renames included, per the owner's requirement
- `{ "type": "forum.post_deleted", "id", "title", "forum_channel_id" }`
- `{ "type": "forum.channel_updated", "changed": ["name" | "available_tags" | "slowmode"], ... }`
  — the forum channel itself was renamed or its tag set changed

Delivery is best-effort with no retries, same as message callbacks.

### `GET /forums/posts/{post_id}`
One post: the summary fields above plus `forum_channel_id`, the starter
message `body`, and its `attachments`. Comments are read with
`GET /channels/{post_id}/history` — a post is a thread.

Errors: `404` not a forum post, `403` missing permissions.

Forum channels appear in `GET /server/structure` as `type: "forum"`
with an `available_tags` list (`id`, `name`, `emoji`, `moderated`).

### `POST /messages/attachments?channel_id=123&filename=report.json&text=optional`
Attach one file to a message (ADR 0007). The request **body is the raw
file bytes** (`application/octet-stream`); the hub never reads a
consumer's disk. `filename` is what Discord displays. `text` is optional
message content. One file per request; send several requests for several
files. Content-agnostic by design — charts, logs, JSON, anything.

**Size cap: enforced before Discord is touched, and adaptive** (ADR
0008) — the cap follows the server's live boost tier: 10 MiB base, 50
MiB at tier 2, 100 MiB at tier 3, with no hub restart needed. Larger
files get an immediate `413` with a "too big" detail and the rejection
is logged hub-side. Preflight against the 10 MiB base to be safe
everywhere. For bigger files, post a link instead — URLs in
`POST /messages` text render inline.

**Response `201`** — same as `POST /messages`: `{ "id": "456",
"channel_id": "123", "via": "webhook" | "bot" }`. Registered channels
keep their webhook identity (webhooks support file uploads); unregistered
channels use the plain bot path. No `reply_to_message_id` on this
endpoint.

Errors: `413` too big, `422` missing `channel_id`/`filename` or empty
body, `404` unknown or not a text channel, `403` bot lacks permission.

### `PATCH /messages/{message_id}`
Edit a message the hub previously sent. The caller supplies the message
id it stored from the send response — the hub keeps no message history.

**Request**
```json
{ "channel_id": 123, "text": "corrected content" }
```

**Response `200`**
```json
{ "id": "456", "channel_id": "123",
  "edited_at": "2026-01-01T12:05:00+00:00", "via": "bot" }
```

Only hub-sent messages can be edited (Discord's rule: bots can only edit
their own messages). Bot-sent messages are edited directly; webhook-sent
ones are edited through the channel's registration webhook. Unlike
sending, an edit is a single message — no chunking — so `text` must fit
Discord's 2000-character limit.

Errors: `422` empty text or over 2000 characters, `404` channel or
message not found, `403` message not sent by the hub or bot lacks
permission.

### `POST /registrations`
Claim a channel for a project. Creates a webhook in the channel for the
project's identity (ADR 0002). Registration persists across hub restarts.
Both text and forum channels can be registered; a forum's posts inherit
its registration.

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
deregistering revokes it. Forum opt-ins (ADR 0009, both default `false`):
`allow_forum_moderation` permits post PATCH and tag administration;
`forum_events` subscribes the callback to forum lifecycle events. Event
opt-ins: `"voice_events": true` (voice join/leave/move) and
`"reaction_events": true` (reaction markers) — see
their sections below.

**Response `201`** — the registration, including the created `webhook_url`.

Errors: `409` channel already claimed by another project (the hub refuses
ambiguity rather than picking a winner), `404` unknown channel, `403`
bot lacks Manage Webhooks. A duplicate claim is rejected before creating a
webhook. Registration ownership is bound to the caller's authenticated
project id (ADR 0011).

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

Consumers must ask the owner to confirm each deletion. The registration
flag remains declared intent, not a replacement for that confirmation.
Authentication verifies which project is calling; it does not prove the
human approved a deletion. No consumer database records or local files are
touched.

Responses: `204` deleted, `403` parent registration didn't opt in (or doesn't
exist), outside the configured server, or Discord permission denied, `404`
missing or not a thread, `502` other upstream Discord failure. A repeated
call can return `404`; no automatic retries or bulk deletion.

### `POST /threads/{thread_id}/archive`
Archive (or unarchive) a thread in the configured server. Reversible, so
unlike deletion it needs no opt-in flag and no owner confirmation (ADR
0004 gates deletion only). Body is optional; defaults archive the thread
unlocked. `locked: true` also locks it (requires Manage Threads).

**Request (optional)**
```json
{ "archived": true, "locked": false }
```
Pass `"archived": false` to unarchive.

**Response `200`**
```json
{ "id": "111", "archived": true, "locked": false }
```

Errors: `404` missing or not a thread, `403` outside the configured
server or Discord permission denied, `502` other upstream Discord
failure.

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
List the caller's current registrations. Secret `webhook_url` fields are
omitted. The operator can list all projects for controlled lab lifecycle
operations, with the same redaction.

### `DELETE /registrations/{channel_id}`
Release a channel and delete its webhook. Only the owning project or the
operator may release it. `204` on success, `404` if the channel was not
registered, `403` if another project owns it.

### `GET /commands`
List redacted slash-command declarations for generated help and diagnostics.
A project receives only its own declarations; the operator receives every
owned project. The response is `{"projects": [{"project_id", "commands"}]}`,
with commands in the same shape accepted by `PUT /commands`. Callback URLs,
webhook URLs, and credentials are never returned. Tokenless legacy callers
receive `403` in optional mode; normal authentication failures receive `401`.
Unowned legacy declarations are omitted.

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
Add `"autocomplete": true` to a `string`/`integer`/`number` option to have
typing suggestions routed to your callback (see below).

**Autocomplete routing** — when a user types into an option declared with
`"autocomplete": true`, the hub POSTs to the callback and the response's
`choices` become Discord's suggestions (max 25, names ≤ 100 chars):
```json
{
  "type": "autocomplete",
  "command": "model",
  "option": { "name": "model", "value": "gp" },
  "options": { "model": "gp" },
  "channel_id": "123",
  "user": { "id": "789", "name": "cedgo" }
}
```
Respond with `{ "choices": [ { "name": "gpt-5", "value": "gpt-5" } ] }`.
Discord needs an answer within ~3 seconds; on callback error/timeout the
hub answers with an empty list, so autocomplete never hangs.

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

On callback error/timeout the hub answers immediately with an ephemeral
"this project is unreachable right now" notice (no internals leaked)
instead of leaving the user staring at a bare defer.

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

One voice connection per server, explicitly owned. Authenticated project
identity determines ownership. The existing `owner` callback-URL field is
retained for request compatibility and display; it is not proof of identity.

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
Refused with `409` while a duplex stream is attached (send through the
stream instead).

### `WS /voice/stream?owner=...`
The duplex stream (ADR 0005, Stage 2). Owner-only; one stream per voice
connection — a second attach is closed with code `1008`. Send the project
bearer token in the WebSocket handshake's `Authorization` header. Requires
an active owned voice connection (`/voice/join` first). The stream closes
when the websocket drops; `/voice/leave` also closes it.

**Hub → owner** (JSON text messages):
- Audio, one message per 20 ms frame per speaker:
  `{ "type": "audio", "user_id": "123", "pcm": "<base64 s16le 48kHz stereo>",
  "timestamp": 1759100000.123 }`
- Speaking activity (a transport signal, not a guaranteed utterance
  boundary — consumers decide interruption policy):
  `{ "type": "speaking", "user_id": "123", "state": "started" | "stopped" }`

**Owner → hub**: binary messages of raw PCM (s16le, 48 kHz, stereo).
Played continuously; the buffer is silence-filled when empty so playback
never terminates mid-conversation. Frames are 3840 bytes (20 ms); trailing
partial frames are dropped.

Live-verified 2026-09-29 (full-duplex echo in the LAB voice channel).

### Voice-state events (registration opt-in)
Add `"voice_events": true` to a channel registration and the hub POSTs
guild-wide voice join/leave/move events to that project's callback URL
(once per project, not per registered channel):

```json
{
  "type": "voice_state",
  "user": {"id": "123", "name": "cedgo"},
  "before_channel_id": null,
  "after_channel_id": "456"
}
```

`before_channel_id = null` is a join, `after_channel_id = null` is a
leave, both set is a move. Mute/deafen toggles and bot users are not
reported. What a join *means* (start a session, page the owner) is the
project's policy — the hub only routes. Intended use: a project starts
its voice session when its owner joins a voice channel, entirely from
the project side.

### Reaction markers (registration opt-in)
A general "reaction -> action" signal. The hub config maps emojis to
marker names (`REACTION_MARKERS` env var, JSON object — see
`.env.example`); add `"reaction_events": true` to a channel registration
and the hub POSTs to that project's callback URL (once per project)
whenever a configured emoji is added to or removed from **any message in
the server**, including messages years old (raw gateway events, no cache
needed):

```json
{
  "type": "reaction_marker",
  "action": "added",
  "emoji": "🔄",
  "marker": "belief_change",
  "message_id": "456",
  "channel_id": "123",
  "user_id": "789"
}
```

`action` is `"added"` or `"removed"` — removing the reaction undoes the
mark. The hub's own reactions are never reported. Whose reaction counts
and what a marker *means* is the project's policy; the hub only routes.
The message content is not included — fetch it via
`GET /channels/{id}/history` if needed.

### Message edit events (automatic)
When a user edits a message in a registered channel (or a thread under
it), the hub POSTs an edit event to that project's callback URL — same
routing as inbound messages, no opt-in flag needed:

```json
{
  "type": "message_edit",
  "message_id": "456",
  "channel_id": "123",
  "author": {"id": "789", "name": "cedgo"},
  "before_text": "orignal content",
  "after_text": "original content",
  "edited_at": "2026-01-01T12:05:00+00:00"
}
```

Every edit is its own event: a message edited three times delivers three
events (`A→B`, `B→C`, `C→D`), so a consumer can replay the full history.
Edits by bots (including the hub itself) are not reported. Threaded
messages include the same `"thread"` block as inbound messages.

Caveat: `before_text` is the pre-edit content **only if the hub saw the
original message while it was running** (Discord's message cache). For a
message sent before the hub last restarted, `before_text` is `null` —
treat it as "unknown prior state", not "was empty". The event still
tells you which message changed and what it became.

## Source archive (ADR 0012)

Capture is disabled unless `ARCHIVE_CHANNEL_IDS` names explicit text/forum IDs.
Threads inherit their parent's capture setting. Registration is unrelated:
capturing a channel does not route it, and routing it does not capture it.
Postgres retains raw Discord `jsonb` and an append-only event chain; attachment
bytes are immutable R2 objects. Capture never changes existing callback bodies
or signatures.

### `GET /archive/messages?channel_id=123&limit=50&before=123`

Newest-first current message projections. `channel_id` is optional; without it,
read only the caller's explicitly granted channels. `limit` is 1–100 and
`before` is an exclusive Discord message ID. The response is `{items,
next_before}`; continue while `next_before` is non-null. Records include source
IDs/provenance, author, text, UTC timestamps, deletion state, and attachment
metadata, never raw transport payloads or Discord CDN URLs.

### `GET /archive/events?channel_id=123&limit=100&before=456`

Append-only `message_created`, `message_edited`, `message_deleted`,
`reaction_added`/`reaction_removed` (🔄 🔒 ⭐), and thread lifecycle events.
Edits retain every before/after pair. `before` here is an exclusive archive
event ID, not a Discord message ID. The response is `{items, next_before}`.
Raw gateway/REST payloads remain internal.

Both reads require a project token listed in `ARCHIVE_READER_CHANNELS`; the
operator can read all captured channels. Tokenless legacy callers have no
archive access. A request naming a channel outside its grant gets `403`.
**While any user's 🔒 reaction is active, that message, all its events, and its
attachments are absent from every downstream response.** Removing the last lock
restores read access. The hub does not interpret the other markers.

### `GET /archive/attachments/{attachment_id}`

Authenticated bytes from R2 with the captured media type. The same channel
grant and 🔒 exclusion apply; absent/private objects return `404`.

### `POST /archive/backfill/{channel_id}?max_messages=500`

Operator only. One bounded (1–5000 messages), resumable, idempotent backfill.
Returns `{channel_id, messages_seen, complete, next_before}`. Repeat until
`complete`; a full final Discord page may require one empty request. A forum
enumerates active plus bounded archived posts and checkpoints each thread.
Backfill preserves REST raw payloads and current marker reactions. Discord
cannot provide historical edits/deletions that predate capture, so backfilled
edits use `before_text: null`; no prior content is invented.

## Versioning rule

Additive changes (new endpoints, new optional fields) are always fine.
Changing or removing anything above requires a version bump and a note
here, because multiple projects depend on this contract.
