"""Offline tests for the voice-recv receive-path hardening (ADR 0006).

No Discord connection: the wrapped voice-recv internals are exercised with
fake packets and stubbed originals.
"""

import unittest
from types import SimpleNamespace

from discord.ext.voice_recv import opus, reader

from discord_hub import voice_recv_compat


class FakeRTPPacket:
    """Minimal stand-in for rtp.RTPPacket at the decryptor seam."""

    def __init__(self, *, padding: bool = False, payload: int = 0x78, decrypted_data=b"audio"):
        self.padding = padding
        self.payload = payload
        self.decrypted_data = decrypted_data

    def __bool__(self):
        return True


class PaddingStripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        voice_recv_compat.apply_voice_recv_patches()

    def _decrypt(self, mode: str, packet, stub_result: bytes):
        decryptor = reader.PacketDecryptor.__new__(reader.PacketDecryptor)
        original = lambda self, p: stub_result  # noqa: E731
        wrapped = voice_recv_compat._wrap_decrypt_rtp(original)
        return wrapped(decryptor, packet)

    def test_no_padding_bit_leaves_payload_untouched(self):
        packet = FakeRTPPacket(padding=False)
        self.assertEqual(self._decrypt("m", packet, b"opus-frame\x01"), b"opus-frame\x01")

    def test_padding_stripped_per_rfc3550(self):
        # 4 pad bytes: 3 zeroes + count byte (count includes itself).
        packet = FakeRTPPacket(padding=True)
        self.assertEqual(self._decrypt("m", packet, b"opus-frame\x00\x00\x00\x04"), b"opus-frame")

    def test_padding_count_of_one_strips_only_count_byte(self):
        packet = FakeRTPPacket(padding=True)
        self.assertEqual(self._decrypt("m", packet, b"opus-frame\x01"), b"opus-frame")

    def test_padding_consuming_whole_payload_yields_empty(self):
        packet = FakeRTPPacket(padding=True)
        self.assertEqual(self._decrypt("m", packet, b"\x03\x03\x03"), b"")

    def test_invalid_padding_count_yields_empty_not_garbage(self):
        # Count larger than the payload must not slice into real audio.
        packet = FakeRTPPacket(padding=True)
        self.assertEqual(self._decrypt("m", packet, b"\x05"), b"")

    def test_zero_padding_count_is_malformed_per_rfc(self):
        packet = FakeRTPPacket(padding=True)
        self.assertEqual(self._decrypt("m", packet, b"frame\x00"), b"")

    def test_class_patch_actually_installed(self):
        # The module patches PacketDecryptor methods at import; bound via
        # __init__'s getattr, so new instances must see the wrapper.
        for mode in (
            "xsalsa20_poly1305",
            "xsalsa20_poly1305_suffix",
            "xsalsa20_poly1305_lite",
            "aead_xchacha20_poly1305_rtpsize",
        ):
            fn = getattr(reader.PacketDecryptor, "_decrypt_rtp_" + mode)
            self.assertTrue(getattr(fn, "_hub_patched", False), mode)

    def test_apply_is_idempotent(self):
        before = reader.PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize
        voice_recv_compat.apply_voice_recv_patches()
        self.assertIs(before, reader.PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize)


class ProcessPacketGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        voice_recv_compat.apply_voice_recv_patches()

    def _decoder(self):
        return SimpleNamespace(ssrc=1234)

    def test_real_opus_packet_passes_through(self):
        packet = FakeRTPPacket()
        original = lambda self, p: "voicedata"  # noqa: E731
        wrapped = voice_recv_compat._wrap_process_packet(original)
        self.assertEqual(wrapped(self._decoder(), packet), "voicedata")

    def test_non_audio_payload_is_dropped(self):
        # Video/RTX packets (camera on) must never reach the opus decoder.
        packet = FakeRTPPacket(payload=0x61)
        original = Mock_fail_on_call()
        wrapped = voice_recv_compat._wrap_process_packet(original)
        self.assertIsNone(wrapped(self._decoder(), packet))
        self.assertFalse(original.called)

    def test_empty_payload_is_dropped(self):
        # Pure-padding keepalive after the strip: nothing to decode.
        packet = FakeRTPPacket(decrypted_data=b"")
        original = Mock_fail_on_call()
        wrapped = voice_recv_compat._wrap_process_packet(original)
        self.assertIsNone(wrapped(self._decoder(), packet))
        self.assertFalse(original.called)

    def test_fake_packet_passes_through(self):
        # Jitter-buffer gap fill (falsy, no .payload) is legitimate PLC input.
        fake = _FakeGapPacket()
        original = lambda self, p: "plc"  # noqa: E731
        wrapped = voice_recv_compat._wrap_process_packet(original)
        self.assertEqual(wrapped(self._decoder(), fake), "plc")

    def test_undecodable_frame_is_dropped_not_raised(self):
        packet = FakeRTPPacket()

        def original(self, p):
            raise RuntimeError("OpusError: corrupted stream")

        wrapped = voice_recv_compat._wrap_process_packet(original)
        self.assertIsNone(wrapped(self._decoder(), packet))

    def test_class_patch_installed(self):
        self.assertTrue(getattr(opus.PacketDecoder._process_packet, "_hub_patched", False))


class Mock_fail_on_call:
    def __init__(self):
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        raise AssertionError("original must not be called")


class _FakeGapPacket:
    ssrc = 1234
    decrypted_data = b""

    def __bool__(self):
        return False


if __name__ == "__main__":
    unittest.main()
