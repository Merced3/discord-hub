# Session: discord-hub verification + backlog sweep

**Suggested session name:** `discord-hub: verification + backlog sweep`

Roadmap: `Automations/ops/session-prompts/README.md` (#3).

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
