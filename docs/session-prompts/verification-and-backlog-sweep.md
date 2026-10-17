# Session: discord-hub verification + backlog sweep

**Suggested session name:** `discord-hub: verification + backlog sweep`

Roadmap: `Automations/ops/session-prompts/README.md` (#3).

*When done, mark it up here.*

Copy everything below the line into the new session.

---

# Close discord-hub's unverified features and the small backlog items consumers are waiting on

**Read first:** `AGENTS.md`, `README.md`, `docs/roadmap.md` (the
"Two-consumer workstream" + "Backlog" sections are the source of this
prompt), `docs/api.md`, `docs/integrating.md`, `docs/decisions/`, and
the ops `RUNBOOK.md`
(`C:\Users\cedgo\Documents\Coding\Automations\ops\RUNBOOK.md`; the hub
runs on the droplet now, and the default branch is **`master`**).
**A bad hub deploy takes every bot offline.** Health-gated rollback
exists, but test locally first.

## Owed verifications (live, in the 🧪 LAB channels)

1. Reaction markers: "Not yet live-verified."
2. Thread deletion + history pages: "Neither … has been live-tested."
3. Voice receive hardening (ADR 0006): "pending live re-verification."
   This has probably been proven by daily teaching-agent voice use since
   10-02. Confirm from server logs/usage and mark it.
4. Live two-consumer run with the REAL consumers (Socrates +
   teaching-agent), both now running on the droplet.

## Backlog items to decide/do (present, user picks)

- **Thread archive + delete for consumers:** socratic-partner is blocked
  on it (its `docs/project-state.md` "Known limitations": empty thread on
  failed `/ask-now`, completed threads not archived). Session #4 depends
  on this.
- **Command autocomplete routing:** socratic-partner `/model` wants it.
- **"Project is offline" auto-reply** when a command callback is
  unreachable.
- **Hub API key:** the roadmap said localhost trust breaks remotely. On
  the droplet the hub is on the Docker network + Tailscale. Decide
  whether that's sufficient now, and record the decision as an ADR.
- **Drop the voice-recv PR-branch pin** if an upstream release ships DAVE
  decryption (check PyPI).
- Out of scope: caretaker proposals, message deletion, and PR buttons
  (buttons belong to `ops: agent dev loop — design`).

## Rules

Tests first for every change: agree the tests with the user, stop,
approve, then implement. Update `docs/roadmap.md` the same day.

## Success =

Each owed verification is verified or explicitly dropped; thread
archive/delete are available to consumers (unblocks #4); the backlog
items are done or have a recorded decision; roadmap updated; tick #3.

---

*When your done, say whatever you did here, don't edit the prompt above, just edit this part and below.*

---

## Outcome — 2026-10-17

**Owed verifications — all done live in the 🧪 LAB:**

1. **Reaction markers ✅** — registered `#lab-text` with a throwaway
   receiver (`scripts/lab_receiver.py`, since removed along with the
   registration); 🔄 `added` and `removed` both arrived with correct
   marker/message/user. Also verified inbound message delivery.
2. **Thread deletion + history ✅** — history pages (newest-first,
   `before` paging) on channel and thread; deletion 204, repeat-delete
   404, parent channel refused 404; owner confirmation collected per
   ADR 0004.
3. **Voice receive hardening (ADR 0006) ✅ — and it was NOT actually
   working.** The "daily voice use proves it" assumption was false.
   Live testing exposed two silent breakages, both invisible to the
   `/health` gate:
   - the rebuilt image had **no `websockets` package** → uvicorn
     returned HTTP 404 for every `/voice/stream` handshake (fixed:
     declared `websockets>=12` in pyproject);
   - the slim image had **no `libopus0`** → `OpusNotLoaded` on every
     voice packet (fixed: apt line in `ops/docker/python-app.Dockerfile`,
     ops commit `a9a73e9`).
   After both fixes: full live session — auto-join, user's speech heard
   (STT), spoken reply. Re-verified.
   **Ops incident on the way:** pushing the Dockerfile change rebuilt
   discord-hub + voice-bridge simultaneously → OOM on the 1 GB droplet
   (comatose, DO-console hard reset). Recorded in the RUNBOOK: rebuild
   one service at a time by hand.
4. **Live two-consumer run with the real pair ✅** — teaching-agent full
   voice session while Socrates answered `/ask-now` (thread created,
   Socrates webhook identity, follow-ups delivered). See roadmap.

**Backlog items:**

- **Thread archive for consumers ✅ built** — `POST /threads/{id}/archive`
  (optional `{archived, locked}`; `archived:false` unarchives). No opt-in
  flag (reversible; ADR 0004 gates deletion only). 7 offline tests.
  Live-verified in the LAB. **Unblocks session #4.**
- **Command autocomplete routing ✅ built** — `"autocomplete": true` on a
  `PUT /commands` option routes an `autocomplete` event to the callback;
  `choices` (max 25) become suggestions; failure → empty list so Discord
  never hangs. 6 offline tests. Unblocks socratic-partner `/model`.
- **"Project is offline" auto-reply ✅ built** — unreachable command
  callback now gets an immediate ephemeral "unreachable right now"
  notice instead of a bare auto-defer. 4 offline tests.
- **Hub API key ✅ decided: not needed** — Docker network + Tailscale is
  the auth boundary (ADR 0010).
- **voice-recv PR pin: stays** — latest PyPI release (0.5.2a179,
  2025-06) predates DAVE; upstream PR #58 still open. Recorded in roadmap.

121 offline tests green; roadmap + api.md + integrating.md updated same
day; #3 ticked in `ops/session-prompts/README.md`.