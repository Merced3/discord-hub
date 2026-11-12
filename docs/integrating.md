# Integrating with discord-hub

The guide for a project that wants to talk to Discord. Read this first;
[`api.md`](api.md) is the precise endpoint reference. There is exactly one
rule underneath everything:

**The hub knows Discord. Your project knows your application. Neither does
the other's job.**

You never install a Discord library, never hold a Discord token, and never
see Discord types. You run a small HTTP callback server; the hub calls it
when Discord things happen to you, and you call the hub when you want
Discord things to happen.

## The moving parts

```
Discord ◄──► discord-hub ──POST events──► your callback server
                ▲
                └────── your HTTP calls (send, threads, commands, ...)
```

- **The hub** (`localhost:8100`) owns the bot connection, the webhook that
  carries your name/avatar, and the slash-command surface.
- **Your callback server** receives inbound messages and command
  invocations. Verify the hub's callback signature, acknowledge within the
  documented budgets (5s for messages, ~2.5s for commands), and do slow work
  in the background.

## Your project credential

The hub operator gives your project a random token. Store it in your
project's environment, never in source or logs. Send it on every hub call:

```http
Authorization: Bearer <project-token>
```

Use the same header when opening the voice WebSocket. The token identifies
your project; it does not add a usage quota. See
[`auth-rollout.md`](auth-rollout.md) for the gradual production rollout.

Callbacks from the hub carry `X-Discord-Hub-Timestamp`,
`X-Discord-Hub-Signature`, and `X-Discord-Hub-Project`. Before acting on an
event, calculate HMAC-SHA256 using your token over
`"<timestamp>.<exact-raw-body>"` and compare it with the signature using a
constant-time comparison. Reject timestamps more than five minutes from
your clock. Check the raw bytes before JSON parsing.

## The three steps to go live

### 1. Register your channel

`POST /registrations` with your channel ID, callback URL, and (optionally)
`display_name`, `avatar_url`, and `allow_thread_deletion`.

This gives you: a webhook identity (your messages appear as *you*, not the
bot), inbound delivery of every human message in the channel **and its
threads**, and ownership of the channel (nobody else can claim it; `409`).
Registrations persist across hub restarts; `DELETE /registrations/{id}`
releases the channel and cleans up the webhook.

Threads inherit the parent's registration. Inbound payloads include a
`thread` field so you can tell conversations apart. What a "session" or
"conversation" is remains entirely your business.

### 2. Receive messages at your callback

The hub POSTs each human message: `message_id`, `channel_id`, `author`,
`text`, `created_at`, plus `thread` when in a thread. Return any 2xx.
Failures are logged and **not retried** — if you need durability, build it
on your side (your own queue, your own storage).

Edits arrive the same way: when a user edits a message in your channel,
the hub POSTs `{"type": "message_edit", "message_id", "author",
"before_text", "after_text", "edited_at", ...}`. Every edit is its own
event, so you can replay the full A→B→C chain. One caveat: `before_text`
is `null` when the original was sent before the hub last restarted —
"unknown prior state", not "was empty".

### 3. Declare your slash commands

`PUT /commands` with your callback URL and command list (names,
descriptions, typed options). Replaces your whole set; other projects'
commands are untouched. Names are unique across all projects (`409`).
Descriptions and options also drive Courier's generated `#how-to` entry, so
keep them accurate and concise. `GET /commands` returns only your authenticated
project's redacted declaration view (the operator can list all projects).

When someone runs your command, the hub POSTs the invocation to your
callback and **your HTTP response is the Discord response**:

- `{ "text": "...", "ephemeral": true }` — shown immediately
- `{ "defer": true, "ephemeral": true }` — "thinking...", then call
  `POST /interactions/{interaction_id}/followups` when the work is done

If your callback is down or too slow, the hub auto-defers so the
interaction survives. Declare your commands every time you start — it's
idempotent and keeps the hub in sync after restarts.

## Sending and managing content

- `POST /messages` — send text. Auto-chunks long messages. Optional
  `reply_to_message_id`. Uses your webhook identity when the channel is
  registered to you. **Store the returned `id`** if you ever want to
  edit the message later; channel history is separate from the opt-in archive.
- `PATCH /messages/{id}` — edit a message you sent (body: `channel_id`,
  `text`). Works for both bot-path and webhook-path sends. Single
  message, no chunking: max 2000 characters.
- `POST /messages/attachments?channel_id=..&filename=..&text=..` — attach
  one file (body = raw bytes, any content type). Cap adapts to the
  server's boost tier (10 MiB base, up to 100 MiB); bigger files get an
  immediate `413` — post a link instead. Webhook identity applies.
- `POST /threads` — create a named thread in your channel.
- `POST /channels/{id}/typing` — show "typing..." (~10s) while you work.
- `GET /channels/{id}/permissions` — preflight before promising delivery.
- `POST /threads/{id}/archive` — archive a thread (optional body:
  `{"archived": true, "locked": false}`; set `archived: false` to
  unarchive). Reversible, so no opt-in flag needed — use this to tidy up
  finished sessions instead of deleting.
- `DELETE /threads/{id}` — delete a thread. **Only works if your
  registration opted in with `allow_thread_deletion: true`, and you must
  get the owner's confirmation first.** You own your database cleanup;
  the hub only performs the Discord deletion.
- `GET /channels/{id}/history` — paginated, newest-first reads. You own
  pagination, storage, and retention. Not a lossless backup.
- Command **autocomplete**: mark a string/integer/number option
  `"autocomplete": true` in `PUT /commands`; while the user types, your
  callback gets `{"type": "autocomplete", "option": {"name", "value"}}`
  and must answer `{"choices": [{"name", "value"}]}` (max 25) within ~3s.
  On error the hub returns an empty list for you.

## Reading the source archive

Archive reads are separate from registration and live Discord history. If the
owner grants your project channels in `ARCHIVE_READER_CHANNELS`, use
`GET /archive/messages` and `GET /archive/events` with your normal bearer token.
Paginate with each response's `next_before`. `GET /archive/attachments/{id}`
returns the stored bytes. Parent-channel grants include their threads, while
source thread IDs remain in each record.

The API never exposes raw Discord payloads. A message with any active 🔒 marker
and all its events/attachments are excluded. Do not treat this source record as
an interpretation: your project owns retrieval, summaries, and meaning. See
[`api.md`](api.md#source-archive-adr-0012) and ADR 0012.

## Voice

One voice connection per server, owned by one project at a time. Join with
your callback URL as `owner` (`POST /voice/join`); you get `409` if another
project holds it. Play audio by POSTing raw PCM (s16le, 48 kHz, stereo) to
`/voice/play?owner=...`; leave with `/voice/leave`.

For conversation (hearing users), attach the duplex stream:
`WS /voice/stream?owner=...` after joining. You receive per-speaker audio
frames (`{type: "audio", user_id, pcm (base64), timestamp}`) and speaking
events (`{type: "speaking", user_id, state}`); you send binary PCM back and
it plays continuously. One stream per connection. Speaking events are a
transport signal, not guaranteed utterance boundaries — buffering and
interruption policy are yours. STT/LLM/TTS (including *whose* voice your
project speaks with) are also yours; the hub only moves audio.

Want to start a session when a user joins a voice channel? Register any
of your channels with `"voice_events": true` and the hub POSTs guild-wide
join/leave/move events to your callback. The trigger policy (which user,
which channel, what to do) is yours.

Want to turn reactions into actions (e.g. "🔄 = this changed my mind")?
Ask the hub operator to add the emoji to `REACTION_MARKERS`, register
with `"reaction_events": true`, and you'll get a `reaction_marker`
callback (`added`/`removed`) for that emoji on any message in the server,
old ones included. The mark, its meaning, and whose reactions count are
yours; the hub just tells you it happened.

## Timeouts and timestamps

- Discord renders `<t:UNIX_TIMESTAMP:R>` as "in 3 hours" natively. No
  library needed — just format the markdown yourself.

## The boundaries that keep this clean

- **No application meaning.** The hub will never learn what your commands
  do, what your threads contain, or when your session ends. If a feature
  request requires that, it's the wrong feature.
- **No retries or queues** in the hub's delivery path. Callback failure is
  logged; your project decides what durability means for it.
- **Authenticated identity.** Your bearer token tells the hub which project
  is calling. Registration, command, interaction, channel, and voice
  ownership are enforced against that identity.
- **Verified callbacks.** The hub signs every callback with your project
  token. Check the timestamp and signature before trusting an event.
- **Private networking.** Local services bind locally; cross-machine access
  uses Tailscale, never a public tunnel. Keep tokens out of URLs and logs.
- **Permission-gated destruction.** Deletion requires your opt-in flag,
  owner confirmation, and Discord's own permissions (Manage Threads).

## Debugging checklist

1. `curl localhost:8100/health` — is the hub up and connected?
2. `curl localhost:8100/registrations` — is your channel claimed, with the
   right callback URL? Is your callback server actually listening?
3. "Connection refused" in the hub log = your callback server isn't up.
4. "Auto-deferring" = your callback was too slow or errored.
5. Commands not appearing? You must `PUT /commands` (again) after the hub
   restarts — and your callback must be reachable at declaration time.
