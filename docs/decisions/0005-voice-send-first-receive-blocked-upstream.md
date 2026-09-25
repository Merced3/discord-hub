# 0005 — Voice: send + ownership now; receive blocked on upstream

**Status:** accepted (owner chose during two-consumer workstream)

## Context

The teaching agent needs full-duplex voice: hear the learner, speak back,
and know when the learner starts talking (interruption). Feasibility check
(2026-09-23) found:

- **Send-side is solid.** discord.py 2.7.1 + `davey` (DAVE/E2EE protocol)
  + PyNaCl connects and plays audio natively.
- **Receive-side is blocked upstream.** discord.py has no receive support;
  `discord-ext-voice-recv` (alpha) adds it, but DAVE decryption exists only
  as open PRs (#54, #58) — unmerged, unreleased. Current builds would
  deliver encrypted gibberish.

## Decision

Build voice in two stages:

**Stage 1 (implemented, live-verified):** join / leave / play / stop /
status, plus explicit **voice ownership**:

- One bot = one voice connection per guild. A project joins with an
  `owner` identity (its callback URL, consistent with registrations and
  commands). A second project's join returns `409`; leave/stop/play from a
  non-owner return `403`. Dropping the connection clears ownership.
- Audio in is **raw PCM**: s16le, 48 kHz, stereo, posted as the request
  body (`application/octet-stream`). No server-local file paths — the hub
  never reads a consumer's disk. (FFmpeg-based file/URL playback may come
  later as a convenience layer, deliberately.)

- The bot needs the `voice_states` gateway intent (without it the voice
  handshake never receives the session ID and times out — found and fixed
  during live verification), plus Connect/Speak permissions and the `davey`
  + PyNaCl packages.

**Stage 2 (contract below, blocked):** bidirectional streaming and speaking
events. When the DAVE-decryption PRs land in a voice-recv release, the hub
gains:

- `WS /voice/stream` (owner only): hub pushes per-speaker PCM frames with
  `{user_id, pcm, timestamp}`; the owner pushes PCM frames back. One
  stream per voice connection; projects buffer utterances themselves.
- Speaking events on the same stream: `{type: "speaking", user_id,
  state: "started"|"stopped"}` — a transport signal, NOT a guaranteed
  utterance boundary; consumers decide interruption policy.

## Consequences

- Teaching-agent can build its STT→LLM→TTS pipeline against the Stage 2
  contract now; only the hub's receive half waits on upstream.
- No fragile pins: we do not depend on an unmerged PR branch of an alpha
  package. Revisit when a voice-recv release includes DAVE support.
- If the upstream package stalls permanently, fallback options are a
  receive sidecar process or switching voice libraries — reopen this ADR.
