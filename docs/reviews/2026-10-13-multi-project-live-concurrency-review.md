# Multi-project concurrency: live verification (2026-10-13)

> **Dated review record, not the live guide.** For the contract, read
> `docs/api.md`; for offline coverage, `tests/test_multi_client.py` and
> `docs/reviews/2026-09-23-two-consumer-review.md`. This record captures
> the *live* follow-up: the same concurrency claims, verified against real
> Discord, as a gate before moving the projects off the local machine
> (see `../ops/PLAN.md`).

## The question

The offline multi-client tests prove the hub's internal logic (routing,
isolation, rollback) with Discord mocked. Before the ops plan could assume
"two or more projects using one hub at the same time" works in the cloud,
the boundary behaviors the offline tests cannot reach had to be checked
live: real rate limits, the real event loop under I/O, the real callback
timeout, and real restart persistence.

## Method

Live hub (`python -m discord_hub.main`), two fake projects registered on
two Lab channels, each with its own localhost callback listener that
timestamped every payload:

- **Test-A** → `#lab-text` (`1554202925012877373`), callback `:8201`
- **Test-B** → `#lab-text-2` (`1559622754066497618`), callback `:8202`

`#lab-text-2` was created for this test (the hub API has no channel-create
endpoint). Test scripts and raw event logs: `data/live_test/` (gitignored).

## Results — 5/5 passed

| Test | Outcome |
|---|---|
| 1. Concurrent outbound, 20 msgs (10 per project, fired together) | 20/20 `201`, both projects kept separate webhook identities, zero 429s surfaced |
| 2. Concurrent inbound (human messages in both channels ~1s apart) | Perfect routing isolation — each listener received only its own channel's events |
| 3. Fault isolation: listener A armed to hang 30s | Hub timed A's callback out at 5s, logged `message callback failed ... not retried`, and **B's message was delivered 120ms after creation while A was still frozen** |
| 4. Hard kill + restart | Reconnected in ~6s; all registrations persisted (`data/registrations.json`); callbacks flowing ~90–160ms after restart |
| 5. Heavy burst: 100 msgs (50 per project, all concurrent) | 100/100 delivered; **~2.4 msg/s effective ceiling**; latency median ~11s, tail ~41s; both projects slowed *equally* (A median 11.3s vs B 9.5s) |

## Findings that matter for ops

1. **Bursts are delayed, never dropped.** The ~2.4 msg/s ceiling is
   Discord's webhook rate limit, not the hub's. The hub queues; callers
   see latency, not errors. Treat slow sends under burst as normal, not an
   alarm condition. Fairness is verified: one chatty project cannot starve
   another.
2. **A hung or dead consumer cannot slow the others.** Live confirmation
   of the offline fault-isolation tests: the 5s callback timeout isolates
   the failure, the delivery is logged and *not retried* (documented in
   `api.md`).
3. **Inbound "simultaneity" is a non-problem by construction.** Discord
   delivers all events over one gateway websocket, strictly serialized, so
   inbound concurrency is really dispatch speed — verified back-to-back.
   The genuinely shared bottleneck is outbound (tests 1 and 5).
4. **`data/registrations.json` is crash-durable state.** It survives a
   hard kill. It must join the VPS backup list in the ops plan (Stage 4)
   alongside the SQLite files already named there.

## Known gaps (not tested, do not assume)

- **Callbacks over a network.** Everything ran localhost-to-localhost.
  Post-cloud, hub→project callbacks cross Tailscale; the 5s timeout and
  no-retry/no-durable-queue behavior become real risks of *silently
  dropped* inbound messages. This is the biggest cloud-specific unknown.
- **Voice under concurrency.** Voice was not exercised live here; if two
  projects will use voice simultaneously it needs its own session.
- **Multi-day stability, memory, reconnect storms.** A 6-second restart
  was verified; long-run supervision belongs to the harness/systemd layer
  in the ops plan.

## Housekeeping from this session

- `#lab-text`'s leftover `marker-watch` dev registration (from reaction-
  marker live testing) was removed at the owner's request, including its
  Discord webhook. `#lab-text` is currently unregistered.
- Live registrations after cleanup: Socrates (`:9100`) and Alvar (`:9200`)
  only.
- ~120 test messages remain in the Lab channels (Lab-only, harmless).
