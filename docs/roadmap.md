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
- [x] **Outbound file attachments** (ADR 0007) — `POST /messages/attachments`,
  raw bytes + filename, content-agnostic; webhook identity preserved;
  adaptive cap follows the server's live boost tier (ADR 0008: 10 MiB
  base, 50/100 MiB at tiers 2/3, no restart), immediate `413` + logged
  rejection. Built for the trading engine (second real need; see the
  2026-10-05 review for the deferred-first evaluation). 9 offline tests.
  Live-verified in the 🧪 LAB: small file via webhook identity, 11 MiB
  file rejected with 413.
- [x] **Message edits, both directions** — outbound: `PATCH
  /messages/{id}` edits any hub-sent message (bot path or webhook path;
  caller supplies the stored message id, the hub keeps no history).
  Inbound: user edits in a registered channel POST a `message_edit`
  event (before/after text, every edit its own event) to the owning
  project — no opt-in flag, same routing as messages. Built for a
  personal-archive consumer that retains everything. 11 offline tests.
  Awaiting live verification.

## Two-consumer workstream

See `docs/reviews/2026-09-23-two-consumer-review.md` for the assessment of
both consumers' requests, and `docs/integrating.md` for the consumer guide.
The user reports both consumers now use the hub; migration is no longer the
next hub task.

- [x] Automated two-project routing, webhook identity, command collision,
  concurrent registration, callback isolation, and failure-recovery checks.
  **14 offline tests passed**; Discord operations are mocked, not live-verified.
- [x] Bounded ordinary callback delivery (five seconds); serialized command and
  registration updates; safer persisted-state writes.
- [x] Live two-consumer acceptance run — passed with two stand-in consumer
  backends against live Discord (routing, identity, command collisions,
  409 hijack refusal, inbound payloads). A run with the *real* Socrates +
  teaching-agent pair is still worthwhile once both are voice-ready.
  Found and fixed: API-created threads defaulted to private (now public).
- [x] Thread deletion scoped by per-registration opt-in flag
  (`allow_thread_deletion`, default false) with per-deletion confirmation
  collected by the consumer (ADR 0004).
- [x] Read-only newest-first history pages for text channels and threads.
  **11 additional offline safety/contract tests passed (25 total).** Neither
  deletion nor history has been live-tested in this workstream.
- [ ] Optional deletion of hub-owned messages (ownership rules still to design).
- [x] **Voice send + ownership** (ADR 0005) — join/leave/play/stop/status,
  one owned connection per guild (409 on contention, 403 for non-owners),
  raw-PCM playback. Live-verified: Courier joined Livestream and played
  audio through Discord's E2EE (DAVE) stack. 13 offline ownership tests.
  Required the `voice_states` gateway intent (declared in code; not a
  privileged intent, no portal toggle needed).
- [x] **Voice receive/streaming + speaking events** (ADR 0005, Stage 2) —
  `WS /voice/stream` owner-only duplex: per-speaker PCM frames and
  started/stopped speaking events to the owner, continuous PCM playback
  back. Uses voice-recv pinned to the PR #58 branch until an upstream
  release ships DAVE decryption. **Live-verified 2026-09-29**: full-duplex
  echo in the LAB voice channel. 11 new offline tests. STT/LLM/TTS remain
  consumer responsibilities. Receive-path hardening (RTP padding strip,
  drop-frame guard, non-audio guard) landed hub-side 2026-10-02 (ADR 0006)
  after Discord began padding voice payloads; pending live re-verification.
- [x] **Voice-state events** — registrations opt in with `voice_events: true`
  and receive guild-wide join/leave/move events at their callback URL.
  Lets projects trigger sessions when a user joins a voice channel without
  the hub learning any session policy. Live-verified. 7 offline tests.
- [x] **Reaction markers** — a general "reaction → action" signal. Hub
  config maps emojis to marker names (`REACTION_MARKERS`); registrations
  opt in with `reaction_events: true` and receive `reaction_marker`
  callbacks (`added`/`removed`) for those emojis on any message in the
  server, old messages included (raw gateway events). Removing the
  reaction undoes the mark. What a marker means is the project's policy.
  8 offline tests. Not yet live-verified.
- [ ] Caretaker proposals — nothing applied without explicit permission.

## Backlog (full picture, with expectations)

1. **Voice receive/streaming + speaking events** — ✅ done (Stage 2,
   ADR 0005). Follow-up: drop the PR-branch pin once a voice-recv release
   ships DAVE decryption.
2. **Live two-consumer acceptance run** — real simultaneous registrations,
   identities, command collisions, one callback down. Offline tests cover the
   logic; this proves it against live Discord. Expect one session, mostly
   verification.
3. **Caretaker proposals** — hub/agent proposes server-structure improvements;
   nothing applied without a yes. Needs Manage Channels + Manage Roles
   (portal session). The original 24/7-manager thesis. Medium effort;
   the "good taste" part iterates forever.
4. **VPS deployment (24/7 operation)** — systemd on Linux; graceful shutdown
   finally works properly there. Remote access also makes the optional
   **hub API key** worth adding (localhost trust model breaks remotely).
   Small-medium ops session.
5. **Hub-owned message deletion** — tiny; only if a consumer asks again.
6. **"Project is offline" auto-reply** — when a command callback is
   unreachable, answer the user with something honest instead of a bare
   auto-defer. Small quality-of-life item.

## Explicitly out of scope (for now)

- Multi-guild support (the hub serves one server; revisit only if a real
  second server appears)
- Archive storage, retention policies, and consumer database cleanup. Read-only
  message-history access is now planned; archival policy remains outside the hub.
