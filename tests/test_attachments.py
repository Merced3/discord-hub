"""Offline checks for outbound file attachments (ADR 0007).

No real .env, token, or Discord. Discord operations are mocked.
Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord
import httpx

from discord_hub.api import MAX_ATTACHMENT_BYTES, create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registration, Registry


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = Settings(_env_file=None, discord_token="fake-token", discord_guild_id=1)
        self.registry = Registry(Path(self.temp.name) / "registrations.json")
        self.bot = HubBot(settings, self.registry, CommandStore(Path(self.temp.name) / "commands.json"))
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)

    def _post(self, params: dict, body: bytes) -> httpx.Response:
        return self.client.post("/messages/attachments", params=params, content=body)

    async def test_missing_query_params_is_422(self):
        resp = await self._post({"channel_id": "123"}, b"data")
        self.assertEqual(resp.status_code, 422)
        resp = await self._post({"filename": "x.png"}, b"data")
        self.assertEqual(resp.status_code, 422)

    async def test_empty_body_is_422(self):
        resp = await self._post({"channel_id": "123", "filename": "x.png"}, b"")
        self.assertEqual(resp.status_code, 422)

    async def test_oversize_is_413_and_never_touches_discord(self):
        big = b"x" * (MAX_ATTACHMENT_BYTES + 1)
        with self.assertLogs("discord_hub.api", level="WARNING") as logs:
            resp = await self._post({"channel_id": "123", "filename": "big.log"}, big)
        self.assertEqual(resp.status_code, 413)
        self.assertIn("too big", resp.json()["detail"])
        self.assertTrue(any("big.log" in line for line in logs.output))

    async def test_exactly_at_cap_is_accepted(self):
        channel = Mock(spec=discord.TextChannel)
        channel.id = 123
        sent = Mock(id=456)
        channel.send = AsyncMock(return_value=sent)
        self.bot.get_channel = Mock(return_value=channel)
        body = b"x" * MAX_ATTACHMENT_BYTES
        resp = await self._post({"channel_id": "123", "filename": "chart.png"}, body)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["via"], "bot")
        _, kwargs = channel.send.call_args
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["file"].filename, "chart.png")

    async def test_bot_path_sends_file_with_text(self):
        channel = Mock(spec=discord.TextChannel)
        channel.id = 123
        channel.send = AsyncMock(return_value=Mock(id=456))
        self.bot.get_channel = Mock(return_value=channel)
        resp = await self._post(
            {"channel_id": "123", "filename": "log.txt", "text": "daily report"}, b"log body"
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), {"id": "456", "channel_id": "123", "via": "bot"})
        _, kwargs = channel.send.call_args
        self.assertEqual(kwargs["content"], "daily report")
        self.assertEqual(kwargs["file"].filename, "log.txt")

    async def test_registered_channel_uses_webhook_with_identity(self):
        self.registry.add(
            Registration(
                channel_id=123,
                callback_url="http://localhost:9000/discord",
                webhook_url="https://discord.com/api/webhooks/1/token",
                display_name="Trader",
                avatar_url="https://example.com/t.png",
            )
        )
        channel = Mock(spec=discord.TextChannel)
        channel.id = 123
        self.bot.get_channel = Mock(return_value=channel)
        message = Mock(id=789)
        webhook = Mock()
        webhook.send = AsyncMock(return_value=message)
        with patch.object(discord.Webhook, "from_url", return_value=webhook):
            resp = await self._post({"channel_id": "123", "filename": "t.json"}, b"{}")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["via"], "webhook")
        _, kwargs = webhook.send.call_args
        self.assertEqual(kwargs["username"], "Trader")
        self.assertEqual(kwargs["avatar_url"], "https://example.com/t.png")
        self.assertTrue(kwargs["wait"])
        self.assertEqual(kwargs["file"].filename, "t.json")

    async def test_unknown_channel_is_404(self):
        self.bot.get_channel = Mock(return_value=None)
        # Not a text channel -> send_attachment raises LookupError -> 404
        self.bot.fetch_channel = AsyncMock(return_value=Mock(spec=discord.VoiceChannel))
        resp = await self._post({"channel_id": "999", "filename": "x.png"}, b"data")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
