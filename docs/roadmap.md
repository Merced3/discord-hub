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
- [x] **Project-owned slash commands** (ADR 0003) — `PUT /commands`,
  invocation routing with the callback's response as the interaction
  response, defer + followups, typed/ranged options. Verified live.
- [x] **Messaging extras** — reply-to-message, auto-chunking, typing
  indicators, channel permission preflight. Verified live.

## Two-consumer workstream

See `integration-review.md` for the assessment of both consumers' requests.
The user reports both consumers now use the hub; migration is no longer the
next hub task.

- [x] Automated two-project routing, webhook identity, command collision,
  concurrent registration, callback isolation, and failure-recovery checks.
  **14 offline tests passed**; Discord operations are mocked, not live-verified.
- [x] Bounded ordinary callback delivery (five seconds); serialized command and
  registration updates; safer persisted-state writes.
- [ ] Live two-consumer acceptance run, when explicitly coordinated. Do not
  confuse the earlier one-consumer live checks with this coverage.
- [ ] Thread deletion with an explicit destructive-operation approval boundary.
- [ ] Read-only paginated history; optional deletion of hub-owned messages.
- [ ] Voice feasibility and ownership design, then join/leave and streaming
  audio with speaking events. STT/LLM/TTS remain consumer responsibilities.
- [ ] Caretaker proposals — nothing applied without explicit permission.

## Explicitly out of scope (for now)

- Multi-guild support (the hub serves one server; revisit only if a real
  second server appears)
- Archive storage, retention policies, and consumer database cleanup. Read-only
  message-history access is now planned; archival policy remains outside the hub.
