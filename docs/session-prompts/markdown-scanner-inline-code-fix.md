# Session: discord-hub markdown scanner phantom-closer fix

**Suggested session name:** `discord-hub: fix _scan_state inline-code blindness`

Found and root-caused during the dev-agent lab harness session
(2026-10-28, ops side); the diagnosis below is PROVEN against live data,
not suspected. Fix belongs here because the bug is hub-side (PRINCIPLES:
helpers change in their own session).

*When done, mark it up here.*

Copy everything below the line into the new session.

---

# Fix `_scan_state` treating markdown markers inside inline code spans as real formatting

**Read first:** `AGENTS.md`, `src/discord_hub/bot.py` (`_MdState`,
`_scan_state`, `_wrappers`, `_chunks` — all near the top), and the hub's
chunking tests. Default branch is **`master`**; the hub deploys to the
droplet on merge, and **a bad hub deploy takes every bot offline** —
health-gated rollback exists, but prove the fix in tests first.

## The proven bug

`_scan_state` toggles `_`, `` ` ``, `*` etc. as inline markdown but never
tracks whether it's **inside an inline code span**. Two failure modes
follow:

1. Underscores inside `` `code` `` (every snake_case identifier, e.g.
   `` `test_excited_uppercases_and_adds_exclamation` ``) toggle the
   phantom italic stack.
2. The code span's own closing backtick then fails to match the stack
   top (it's `_`), so ANOTHER `` ` `` is pushed.

Real captured case (dev-agent lab, 2026-10-28): an 894-char message —
no split involved — ended in a plain period, but Discord received a
trailing `` `_` ``. Running the hub's own scanner on the exact source
text reproduces it byte-for-byte:

```
phantom open markers: ['`', '_', '`']
appended suffix:      '`_`'
```

The same mechanism produces the `` **`_`***_* `` seam wreckage seen in
production (`#dev-voice-bridge` thread `1564485711208513576`) when a real
2000-char split happens mid-message: the closer/opener pair at the seam
is computed from a corrupted stack.

Note the suffix is appended even when NO split occurs (single-chunk
messages still get the closer), so this corrupts short messages too —
any message with snake_case inside backticks is a candidate.

## The fix (direction, not prescription)

Track inline-code state in `_scan_state`: when a `` ` `` opens an inline
code span, skip ALL marker scanning until the matching closing backtick
(double-backtick spans too, if you want to be thorough). Fences already
get this treatment; inline spans need the same. Keep backslash-escape
handling intact.

## Tests first (the usual rule)

Agree the tests with the user, stop, get approval, then implement.
Minimum coverage:

- snake_case identifiers inside `` `code` `` leave the stack EMPTY
  (the captured dev-agent text is a free fixture — reproduce it exactly,
  assert no suffix is appended)
- a real 2000-char split seam through snake_case-in-backticks produces
  matching closer/opener pairs with no phantom markers
- existing chunking tests keep passing (fences, escapes, `**` before `*`)

## Success =

The captured text round-trips byte-identical through `_chunks`; the
phantom-suffix case is test-pinned; roadmap/backlog entry closed;
merged to `master` and deployed clean (watch `#ops-alerts`).

---

## Outcome — 2026-10-28 (FIXED; offline verification green)

`_scan_state` now treats a backtick run as an inline-code delimiter and
ignores all other markdown markers until the same-length closing run. This
covers ordinary and double-backtick spans while preserving backslash escapes,
fences, and synthetic close/reopen wrappers for code spans that really do cross
a chunk boundary. `scripts/split_playground.py` received the same scanner fix
plus a behavioral self-check for closed snake_case code spans.

Tests were approved first and failed on the old implementation for the intended
reasons. The exact clean 894-character dev-agent message was recovered from the
lab pi transcript and pinned at
`tests/fixtures/dev_agent_phantom_suffix.txt`: it previously produced the
proven `['`', '_', '`']` stack and `` `_` `` suffix, and now round-trips as one
chunk with no synthetic wrapper. A forced hard split through snake_case inside
inline code also produces only matching backtick wrappers. Full verification:
**125 tests passed**, Ruff clean, and playground **16/16** checks passed.

**Test-registration decision:** do not add a hub-side ephemeral/TTL registration
mode for this need. Temporary lab ownership and the unregistered default are
ops/consumer lifecycle policy, not Discord transport; the existing registration
API already provides the required primitives. Keep `lab_registration.py` with
dev-agent while it is the sole automated consumer. If multiple projects automate
lab runs, extract a project-neutral ops lab helper rather than moving that policy
into discord-hub.

## While you're in here (optional backlog idea, owner 2026-10-28)

The 🧪 LAB channels' default state is now UNREGISTERED (owner policy):
tests claim a channel, then release it. Today that means every test
harness does snapshot/delete/re-register dances around the hub's
single-owner registrations. Consider (decide, don't necessarily build):
an opt-in **test registration mode** — e.g. a registration flag that
yields to any newer registration and auto-reclaims when the newer one
is deleted, or first-class ephemeral registrations with a TTL. If you
decide it's not worth it, record the decision; the claim/release script
in ops/dev-agent/scripts/ is the interim answer.
