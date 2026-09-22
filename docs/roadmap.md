# Roadmap

## Exists now

- [x] Project skeleton, config from environment, clean startup/shutdown
- [x] `GET /health` — liveness for the automation-harness
- [x] `GET /server/structure` — the caretaker's view of the server

## Next up (in rough order)

1. **`POST /messages`** — the first thing socratic-partner needs to drop
   its own discord code. Small, high value.
2. **`POST /registrations` + inbound delivery** — the hub stops being
   send-only and becomes a real router.
3. **Migrate socratic-partner** — replace its `discord_bot.py` internals
   with hub API calls. Done in the socratic-partner repo/session.
4. **Voice foundation** — join/leave/play. The teaching agent's
   prerequisite.
5. **Caretaker proposals** — given the structure snapshot, the hub (or an
   agent driving it) can suggest a better server layout. Always
   permission-gated; nothing is applied without a yes.
6. **Automation-harness integration** — run the hub inside the harness:
   single-instance lock, graceful shutdown, persistent schedules (e.g.
   periodic server-structure snapshots), structured logs, `status.json`.

## Explicitly out of scope (for now)

- Multi-guild support (the hub serves one server; revisit only if a real
  second server appears)
- Slash commands owned by the hub (projects may add their own later via
  the hub if needed)
- Message history/archiving
