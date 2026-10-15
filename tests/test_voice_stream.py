"""Offline tests for the duplex voice stream (ADR 0005, Stage 2).

No real voice connection: the voice client is mocked. Websocket tests run
through starlette's TestClient against the real FastAPI app.
"""

import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from starlette.testclient import TestClient

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry
from discord_hub.voice import _PCM_FRAME_BYTES, StreamAudioSource

A, B = "http://localhost:9001/cb", "http://localhost:9002/cb"


class StreamAudioSourceTests(unittest.TestCase):
    def test_read_returns_pushed_frames_then_silence(self):
        source = StreamAudioSource()
        source.push(b"a" * _PCM_FRAME_BYTES * 2)
        self.assertEqual(source.read(), b"a" * _PCM_FRAME_BYTES)
        self.assertEqual(source.read(), b"a" * _PCM_FRAME_BYTES)
        self.assertEqual(source.read(), b"\x00" * _PCM_FRAME_BYTES)

    def test_partial_trailing_bytes_are_dropped(self):
        source = StreamAudioSource()
        source.push(b"a" * (_PCM_FRAME_BYTES + 10))
        self.assertEqual(source.read(), b"a" * _PCM_FRAME_BYTES)
        self.assertEqual(source.read(), b"\x00" * _PCM_FRAME_BYTES)

    def test_is_not_opus(self):
        self.assertFalse(StreamAudioSource().is_opus())


class StreamApiTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.bot = HubBot(settings, Registry(root / "r.json"), CommandStore(root / "c.json"))

        from discord.ext import voice_recv

        self.vc = Mock(spec=voice_recv.VoiceRecvClient)
        self.vc.is_connected = Mock(return_value=True)
        self.vc.is_playing = Mock(return_value=False)
        self.vc.play = Mock()
        self.vc.stop = Mock()
        self.vc.listen = Mock()
        self.vc.stop_listening = Mock()
        self.vc.disconnect = AsyncMock()
        self.vc.move_to = AsyncMock()
        import discord

        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id, self.channel.guild = 55, SimpleNamespace(id=1)
        self.channel.connect = AsyncMock(return_value=self.vc)
        self.bot.get_channel = Mock(return_value=self.channel)
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)

        self.client = TestClient(create_api(self.bot, 0))
        self.addCleanup(self.client.close)
        self.client.post("/voice/join", json={"channel_id": 55, "owner": A})

    def test_stream_requires_owner_param(self):
        try:
            with self.client.websocket_connect("/voice/stream"):
                self.fail("should not accept a stream without an owner")
        except Exception:
            pass

    def test_stream_refused_for_non_owner(self):
        try:
            with self.client.websocket_connect(f"/voice/stream?owner={B}"):
                self.fail("should not accept a non-owner stream")
        except Exception:
            pass

    def test_stream_refused_without_connection(self):
        self.client.post("/voice/leave", json={"owner": A})
        try:
            with self.client.websocket_connect(f"/voice/stream?owner={A}"):
                self.fail("should not accept a stream with no connection")
        except Exception:
            pass

    def test_audio_event_forwarded_and_outbound_buffered(self):
        with self.client.websocket_connect(f"/voice/stream?owner={A}") as ws:
            stream = self.bot.voice._session.stream
            self.assertIsNotNone(stream)
            self.vc.listen.assert_called_once()
            self.vc.play.assert_called_once()

            # inbound: an audio event reaches the websocket
            member = SimpleNamespace(id=42, bot=False)
            data = SimpleNamespace(pcm=b"\x01\x02" * 960)
            stream.on_audio(member, data)
            event = ws.receive_json()
            self.assertEqual(event["type"], "audio")
            self.assertEqual(event["user_id"], "42")
            self.assertEqual(base64.b64decode(event["pcm"]), b"\x01\x02" * 960)

            # outbound: binary frames land in the playback buffer
            ws.send_bytes(b"b" * _PCM_FRAME_BYTES)
            import time

            deadline = time.time() + 2
            while time.time() < deadline:
                if stream.outbound.read() == b"b" * _PCM_FRAME_BYTES:
                    break
            else:
                self.fail("outbound frame never reached the playback buffer")

        # websocket closed: the slot is freed for a new stream
        self.assertIsNone(self.bot.voice._session.stream)

    def test_speaking_events_forwarded_and_bots_ignored(self):
        with self.client.websocket_connect(f"/voice/stream?owner={A}") as ws:
            stream = self.bot.voice._session.stream
            human = SimpleNamespace(id=7, bot=False)
            bot_member = SimpleNamespace(id=8, bot=True)
            stream.on_speaking_start(bot_member)
            stream.on_speaking_start(human)
            event = ws.receive_json()
            self.assertEqual(event, {"type": "speaking", "user_id": "7", "state": "started"})
            stream.on_speaking_stop(human)
            event = ws.receive_json()
            self.assertEqual(event["state"], "stopped")

    def test_second_stream_refused_while_attached(self):
        with self.client.websocket_connect(f"/voice/stream?owner={A}"):
            try:
                with self.client.websocket_connect(f"/voice/stream?owner={A}"):
                    self.fail("second concurrent stream must be refused")
            except Exception:
                pass

    def test_play_refused_while_stream_attached(self):
        with self.client.websocket_connect(f"/voice/stream?owner={A}"):
            response = self.client.post(f"/voice/play?owner={A}", content=b"x" * 3840)
            self.assertEqual(response.status_code, 409)

    def test_leave_closes_stream_and_disconnects(self):
        with self.client.websocket_connect(f"/voice/stream?owner={A}"):
            pass
        response = self.client.post("/voice/leave", json={"owner": A})
        self.assertEqual(response.status_code, 204)
        self.vc.stop_listening.assert_called_once()
        self.vc.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
