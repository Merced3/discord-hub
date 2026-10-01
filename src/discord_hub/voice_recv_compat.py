"""Receive-path hardening for discord-ext-voice-recv (ADR 0006).

The hub pins voice-recv to the PR #58 fork for inbound DAVE decryption,
but that fork still has three receive-path bugs that starve audio. They
are fixed here, in hub-owned code applied at import time, so a fresh
``pip install`` reproduces a working receive path and a pip upgrade of
voice-recv fails loudly instead of silently reverting behavior:

1. **RTP padding is never stripped** (root cause of the 2026-10-01
   outage). Discord now pads voice payloads; per RFC 3550 §5.1 the last
   payload byte is the pad count (including itself). Left in place, the
   padding shifts DAVE's end-of-frame marker and every frame fails
   decryption (``DecryptionFailed(UnencryptedWhenPassthroughDisabled)``)
   or opus decode. Same root cause/fix as discord.js#11449 and
   Discord.Net#3263. Fixed by wrapping the transport decryptors: strip
   padding right after transport decryption, before DAVE decrypt and
   opus decode ever see the payload.
2. **One undecodable packet kills the router thread.** A frame arriving
   before the ssrc->user map (or before the DAVE ratchet is ready) raises
   OpusError out of ``PacketDecoder._process_packet`` and permanently
   kills voice-recv's router thread — the true starvation cause found on
   2026-09-29. Fixed by dropping the frame instead.
3. **Non-audio packets reach the opus decoder.** Video/RTX packets (any
   payload type other than 0x78) and pure-padding keepalives (empty after
   the padding strip) produce spurious decode failures — the residual
   opus ``invalid argument`` errors seen after the first padding fix.
   Fixed by dropping them in the same wrapper, mirroring the guards in
   discord.js#11449.

Everything here keys off voice-recv internals pinned by commit in
pyproject.toml; if the pin moves, re-verify this module against the new
source.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_OPUS_PAYLOAD_TYPE = 0x78  # Discord's RTP payload type for opus audio

_applied = False


def _wrap_decrypt_rtp(original):
    def decrypt_rtp_stripping_padding(self, packet):
        data = original(self, packet)
        # RFC 3550 §5.1: when the padding bit is set, the final byte is the
        # pad count including itself. Discord pads voice payloads; the pad
        # bytes are not part of the DAVE ciphertext / opus frame.
        if getattr(packet, "padding", False) and data:
            pad = data[-1]
            if 0 < pad <= len(data):
                data = data[:-pad]
            else:
                log.warning(
                    "invalid RTP padding (count=%d, payload=%d bytes); dropping payload",
                    pad, len(data),
                )
                data = b""
        return data

    return decrypt_rtp_stripping_padding


def _wrap_process_packet(original):
    def process_packet_guarded(self, packet):
        # Only opus voice frames belong in the decoder. Real RTP packets
        # carry .payload; FakePacket (jitter-buffer gap fill) is falsy and
        # has none — it is legitimate PLC input and must pass through.
        if packet:
            if getattr(packet, "payload", _OPUS_PAYLOAD_TYPE) != _OPUS_PAYLOAD_TYPE:
                return None
            if not getattr(packet, "decrypted_data", None):
                # Pure-padding keepalive / undecryptable shell: no audio here.
                return None
        try:
            return original(self, packet)
        except Exception as exc:
            # One bad frame (DAVE ratchet not ready, ssrc not yet mapped,
            # corrupted opus) must not kill the router thread and the whole
            # listen — drop it and keep receiving.
            log.warning("dropping undecodable packet ssrc=%s: %s", self.ssrc, exc)
            return None

    return process_packet_guarded


def apply_voice_recv_patches() -> None:
    """Idempotently patch the pinned voice-recv fork. Call before listen()."""
    global _applied
    if _applied:
        return

    from discord.ext.voice_recv import opus, reader

    for mode in (
        "xsalsa20_poly1305",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305_lite",
        "aead_xchacha20_poly1305_rtpsize",
    ):
        name = "_decrypt_rtp_" + mode
        original = getattr(reader.PacketDecryptor, name, None)
        if original is None:
            raise RuntimeError(
                f"voice-recv internals changed: PacketDecryptor.{name} missing; "
                "re-verify voice_recv_compat against the pinned fork"
            )
        if getattr(original, "_hub_patched", False):
            continue
        wrapped = _wrap_decrypt_rtp(original)
        wrapped._hub_patched = True  # type: ignore[attr-defined]
        setattr(reader.PacketDecryptor, name, wrapped)

    original_process = opus.PacketDecoder._process_packet
    if not getattr(original_process, "_hub_patched", False):
        wrapped_process = _wrap_process_packet(original_process)
        wrapped_process._hub_patched = True  # type: ignore[attr-defined]
        opus.PacketDecoder._process_packet = wrapped_process

    _applied = True
    log.info("voice-recv receive-path hardening applied (ADR 0006)")
