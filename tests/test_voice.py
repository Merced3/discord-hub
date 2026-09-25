"""Offline tests for voice ownership (ADR 0005). No real voice connection."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry

A, B = "http://localhost:9001/cb", "http://localhost:9002/cb"


class VoiceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.bot = HubBot(settings, Registry(root / "r.json"), CommandStore(root / "c.json"))
        self.client_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client_http.aclose)
        self.vc = Mock(spec=discord.VoiceClient)
        self.vc.is_connected = Mock(return_value=True)
        self.vc.is_playing = Mock(return_value=False)
        self.vc.play = Mock()
        self.vc.stop = Mock()
        self.vc.disconnect = AsyncMock()
        self.vc.move_to = AsyncMock()
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id, self.channel.guild = 55, SimpleNamespace(id=1)
        self.channel.connect = AsyncMock(return_value=self.vc)
        self.text = Mock(spec=discord.TextChannel)
        self.bot.get_channel = Mock(side_effect=lambda cid: {55: self.channel, 56: self.text}.get(cid))
        self.bot.fetch_channel = AsyncMock(side_effect=lambda cid: {55: self.channel, 56: self.text}[cid])

    async def join(self, owner=A, cid=55):
        return await self.client_http.post("/voice/join", json={"channel_id": cid, "owner": owner})

    async def test_status_starts_disconnected(self):
        status = (await self.client_http.get("/voice/status")).json()
        self.assertEqual(status, {"connected": False, "owner": None, "channel_id": None})

    async def test_join_then_status(self):
        response = await self.join()
        self.assertEqual(response.status_code, 200)
        self.channel.connect.assert_awaited_once()
        status = (await self.client_http.get("/voice/status")).json()
        self.assertEqual(status, {"connected": True, "owner": A, "channel_id": "55"})

    async def test_second_owner_rejected_409_and_no_move(self):
        await self.join()
        response = await self.join(owner=B)
        self.assertEqual(response.status_code, 409)
        self.vc.move_to.assert_not_awaited()
        self.channel.connect.assert_awaited_once()

    async def test_same_owner_rejoin_moves_channels(self):
        await self.join()
        other = Mock(spec=discord.VoiceChannel)
        other.id, other.guild = 57, SimpleNamespace(id=1)
        self.bot.get_channel.side_effect = lambda cid: {55: self.channel, 57: other}.get(cid)
        self.bot.fetch_channel.side_effect = lambda cid: {55: self.channel, 57: other}[cid]
        response = await self.join(cid=57)
        self.assertEqual(response.status_code, 200)
        self.vc.move_to.assert_awaited_once_with(other)
        self.assertEqual((await self.client_http.get("/voice/status")).json()["channel_id"], "57")

    async def test_join_rejects_text_channel_and_unknown(self):
        self.assertEqual((await self.join(cid=56)).status_code, 404)
        self.bot.get_channel.side_effect = lambda cid: None
        self.bot.fetch_channel.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="fake"), {"message": "fake", "code": 0}
        )
        self.assertEqual((await self.join(cid=99)).status_code, 404)

    async def test_leave_requires_owner(self):
        await self.join()
        self.assertEqual((await self.client_http.post("/voice/leave", json={"owner": B})).status_code, 403)
        self.assertEqual((await self.client_http.post("/voice/leave", json={"owner": A})).status_code, 204)
        self.vc.disconnect.assert_awaited_once()
        self.assertEqual((await self.client_http.get("/voice/status")).json()["connected"], False)

    async def test_leave_without_connection_404(self):
        self.assertEqual((await self.client_http.post("/voice/leave", json={"owner": A})).status_code, 404)

    async def test_freed_connection_can_be_claimed_by_another_owner(self):
        await self.join()
        await self.client_http.post("/voice/leave", json={"owner": A})
        self.channel.connect.reset_mock()
        response = await self.join(owner=B)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client_http.get("/voice/status")).json()["owner"], B)

    async def test_play_requires_owner_and_body(self):
        await self.join()
        self.assertEqual((await self.client_http.post("/voice/play?owner=" + B, content=b"x" * 3840)).status_code, 403)
        self.assertEqual((await self.client_http.post("/voice/play?owner=" + A, content=b"")).status_code, 422)
        self.assertEqual((await self.client_http.post("/voice/play", content=b"x" * 3840)).status_code, 422)
        response = await self.client_http.post("/voice/play?owner=" + A, content=b"x" * 3840)
        self.assertEqual(response.status_code, 202)
        self.vc.play.assert_called_once()

    async def test_play_replaces_current_playback(self):
        await self.join()
        self.vc.is_playing = Mock(return_value=True)
        await self.client_http.post("/voice/play?owner=" + A, content=b"x" * 3840)
        self.vc.stop.assert_called_once()
        self.vc.play.assert_called_once()

    async def test_stop_requires_owner(self):
        await self.join()
        self.assertEqual((await self.client_http.post("/voice/stop", json={"owner": B})).status_code, 403)
        self.assertEqual((await self.client_http.post("/voice/stop", json={"owner": A})).status_code, 204)

    async def test_play_without_connection_404(self):
        response = await self.client_http.post("/voice/play?owner=" + A, content=b"x" * 3840)
        self.assertEqual(response.status_code, 404)

    async def test_dropped_connection_clears_ownership(self):
        await self.join()
        self.vc.is_connected = Mock(return_value=False)
        self.assertEqual((await self.client_http.get("/voice/status")).json()["connected"], False)
        # Another owner may now join.
        response = await self.join(owner=B)
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
