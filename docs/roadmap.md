# Roadmap

## Exists now

- [x] Project skeleton, config from environment, clean startup/shutdown
- [x] `GET /health` — liveness for the automation-harness
- [x] `GET /server/structure` — the caretaker's view of the server
- [x] `POST /messages` — send a text message to a channel
- [x] `scripts/check_permissions.py` + `scripts/show_structure.py` —
  verified live against the real server (message delivered end-to-end)
- [x] `POST /registrations` + inbound delivery — channel ownership,
  webhook identity (ADR 0002), incoming messages POSTed to the owning
  project's callback; persists across restarts. Verified live.
- [x] **Automation-harness integration** — the hub runs as a supervised
  harness service: single-instance lock (verified: second instance
  refuses), stale-lock recovery after a hard kill (verified), structured
  logs, `data/status.json`.
- [x] **Thread support** — `POST /threads`, threads inherit the parent
  channel's registration (inbound payloads carry a `thread` field;
  outbound sends into threads keep the project's webhook identity).
  Built for socratic-partner's session-per-thread design. Verified live.

## Next up (in rough order)

1. **Migrate socratic-partner** — replace its `discord_bot.py` internals
   with hub API calls. Done in the socratic-partner repo/session.
2. **Voice foundation** — join/leave/play. The teaching agent's
   prerequisite.
3. **Caretaker proposals** — given the structure snapshot, the hub (or an
   agent driving it) can suggest a better server layout. Always
   permission-gated; nothing is applied without a yes.

## Explicitly out of scope (for now)

- Multi-guild support (the hub serves one server; revisit only if a real
  second server appears)
- Slash commands owned by the hub (projects may add their own later via
  the hub if needed)
- Message history/archiving
