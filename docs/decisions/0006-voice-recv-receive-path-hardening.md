# ADR 0006 — Voice receive-path hardening lives in hub code, not the venv

## Context

Discord voice receive broke twice on the pinned voice-recv fork
(PR #58, `dave-decrypt` branch — itself required because PyPI voice-recv
cannot decrypt DAVE/E2EE frames, ADR 0005):

- **2026-09-29:** a single frame arriving before the ssrc→user map raised
  `OpusError` out of `PacketDecoder._process_packet` and permanently killed
  voice-recv's router thread — the whole listen starved.
- **2026-10-01:** Discord started padding RTP voice payloads. voice-recv
  parses the padding bit but never strips it, so the pad bytes shifted
  DAVE's end-of-frame marker and *every* frame failed decryption
  (`DecryptionFailed(UnencryptedWhenPassthroughDisabled)`) → opus decode
  failures → receive starved. Identical root cause as discord.js#11445,
  fixed upstream by discord.js#11449 and Discord.Net#3263.

Both times the fix was hand-patched into the hub's `.venv` — invisible to
git, reverted by any pip upgrade, and applied from another project's
session. Residual opus `invalid argument` errors remained even after the
padding patch.

## Decision

`src/discord_hub/voice_recv_compat.py` applies the fixes at import time,
in hub-owned code:

1. **Strip RTP padding** (RFC 3550 §5.1) right after transport decryption
   by wrapping `PacketDecryptor._decrypt_rtp_*`, before DAVE decrypt and
   opus decode see the payload. Invalid pad counts drop the payload.
2. **Drop undecodable frames** instead of letting one exception kill the
   router thread (wraps `PacketDecoder._process_packet`).
3. **Drop non-audio packets** (payload type ≠ 0x78) and empty
   (pure-padding keepalive) payloads before decode — mirroring the guards
   in discord.js#11449; this addresses the residual `invalid argument`
   errors.

Patches bind when voice-recv constructs its objects (`apply()` runs at
`voice.py` import, before any `listen()`), are idempotent, and raise
loudly if the pinned fork's internals change. The `.venv` was restored to
the pristine pinned fork; davey moved 0.1.4 → 0.1.6 (the version was
never the bug — padding was).

## Consequences

- A fresh `pip install` of the hub reproduces a working receive path;
  nothing lives only in the venv.
- If the voice-recv pin moves, `voice_recv_compat` must be re-verified
  against the new source (it fails loudly if internals renamed).
- Correct long-term home is upstream: if voice-recv ships these fixes
  (padding strip is the universally-needed one), this module shrinks or
  disappears. Check before touching the pin.
- `tests/test_voice_recv_compat.py` covers the strip math, drop
  semantics, and that the patches are actually installed.
- Live gate: re-run the echo probe / teaching-agent voice Tests 1–4 after
  deploy. Offline tests cannot prove Discord decryption.
