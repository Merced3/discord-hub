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

## Planned (not yet implemented)

### `POST /messages`
Send a text message. Body: `{ "channel_id": "123", "text": "hello" }`.

### `POST /registrations`
Claim a channel for a project. Body:
`{ "channel_id": "123", "callback_url": "http://localhost:9000/discord" }`.
Refuses with `409` if the channel is already claimed (no ambiguity).

Inbound messages are then delivered to the callback as
`POST {callback_url}` with `{ "channel_id", "author", "text" }`.

### `POST /voice/join`, `POST /voice/leave`, `POST /voice/play`
Voice channel control — the foundation the teaching agent's audio
conversations will be built on.

## Versioning rule

Additive changes (new endpoints, new optional fields) are always fine.
Changing or removing anything above requires a version bump and a note
here, because multiple projects depend on this contract.
