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
- [x] **Forum support** (ADR 0009) — forums are registrable; posts
  (which are threads) inherit the registration. Reads: forums in
  `/server/structure` with `available_tags`, `GET /forums/{id}/posts`
  (bounded archived pagination), `GET /forums/posts/{id}`. Writes:
  `POST /forums/{id}/posts` (chunked bodies; webhook identity when
  untagged, bot identity when tagged — a Discord webhook constraint),
  comments via the existing message endpoints. Moderation behind the
  per-registration `allow_forum_moderation` flag: `PATCH
  /forums/posts/{id}` (rename/retag/archive/lock/pin) and tag-set
  administration (`POST/PATCH/DELETE /forums/{id}/tags`). Lifecycle
  events behind `forum_events`: post created/updated (with a `changed`
  list — renames detected)/deleted and forum-channel updates. Verified
  live end-to-end in the 🧪 LAB (`scripts/check_forums.py`, 17 checks);
  cleanup exercised the ADR 0004 human-confirmed deletion path, 20/20
  posts removed.

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
  **Real-pair run 2026-10-17**: teaching-agent full voice session (auto-join,
  STT heard the user, spoken reply) while Socrates answered `/ask-now` —
  thread created, question posted with Socrates' webhook identity, follow-ups
  delivered. Both consumers simultaneously live through the hub.
- [x] Thread deletion scoped by per-registration opt-in flag
  (`allow_thread_deletion`, default false) with per-deletion confirmation
  collected by the consumer (ADR 0004).
- [x] Read-only newest-first history pages for text channels and threads.
  **11 additional offline safety/contract tests passed (25 total).**
  **Live-tested 2026-10-17** in the 🧪 LAB: newest-first pagination with
  `before`, thread history reads; deletion 204, repeat-delete 404, parent
  channel refused 404 (owner-confirmed per ADR 0004).
- [x] **Command autocomplete routing** — options declared with
  `"autocomplete": true` in `PUT /commands` route typing suggestions to
  the owning project's callback (`autocomplete` event; response `choices`,
  max 25, ~3s budget). Failure returns an empty list — autocomplete never
  hangs. Built for socratic-partner's `/model`. 6 offline tests.
- [x] **Thread archive/unarchive** — `POST /threads/{id}/archive`
  (optional body `{archived, locked}`; `archived: false` unarchives).
  Reversible, so no opt-in flag (ADR 0004 gates deletion only). Built
  for socratic-partner's completed-session cleanup. 7 offline tests.
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
  after Discord began padding voice payloads. **Live re-verified
  2026-10-17** (full session: user speech → STT → spoken reply). The
  re-verification caught that voice had actually been **broken since the
  VPS move**, masked by health checks that only probe `/health`: (1) the
  rebuilt image had no `websockets` package, so uvicorn returned HTTP 404
  for every `/voice/stream` handshake (fixed: declared the dep); (2) the
  slim image had no `libopus0`, so discord.py raised `OpusNotLoaded` on
  every packet (fixed in the shared `python-app.Dockerfile`).
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
  8 offline tests. **Live-verified 2026-10-17** in the 🧪 LAB: 🔄 added
  and removed both delivered with the right marker/message/user.
- [ ] Caretaker proposals — nothing applied without explicit permission.

## Backlog (full picture, with expectations)

1. **Voice receive/streaming + speaking events** — ✅ done (Stage 2,
   ADR 0005). Follow-up checked 2026-10-17: latest PyPI release
   (0.5.2a179, 2025-06) predates DAVE and upstream PR #58 is still open,
   so the PR-branch pin **stays**; re-check at the next upstream release.
4. **VPS deployment (24/7 operation)** — ✅ done (hub runs on the
   droplet; see the ops RUNBOOK). **Hub API key: decided against** —
   Docker network + Tailscale is the auth boundary (ADR 0010).
2. ~~**Live two-consumer acceptance run**~~ ✅ done 2026-10-17 (see the
   workstream note above).
3. **Caretaker proposals** — hub/agent proposes server-structure improvements;
   nothing applied without a yes. Needs Manage Channels + Manage Roles
   (portal session). The original 24/7-manager thesis. Medium effort;
   the "good taste" part iterates forever.
5. **Hub-owned message deletion** — tiny; only if a consumer asks again.
6. ~~**"Project is offline" auto-reply**~~ ✅ done 2026-10-17 — an
   unreachable command callback now gets an immediate ephemeral
   "unreachable right now" notice instead of a bare auto-defer.
   4 offline tests.

## Explicitly out of scope (for now)

- Multi-guild support (the hub serves one server; revisit only if a real
  second server appears)
- Archive storage, retention policies, and consumer database cleanup. Read-only
  message-history access is now planned; archival policy remains outside the hub.
