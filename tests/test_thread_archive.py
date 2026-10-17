"""Offline contract tests for thread archive/unarchive (no opt-in flag,
unlike ADR 0004 deletion)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry


class ThreadArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.bot = HubBot(settings, Registry(root / "regs.json"), CommandStore(root / "cmds.json"))
        self.thread = Mock(spec=discord.Thread)
        self.thread.id, self.thread.parent_id = 11, 10
        self.thread.guild = SimpleNamespace(id=1, me=object())
        self.thread.edit = AsyncMock()
        self.bot.get_channel = Mock(return_value=self.thread)
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_archive_defaults_archived_true_unlocked(self):
        response = await self.client.post("/threads/11/archive")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "11", "archived": True, "locked": False})
        self.thread.edit.assert_awaited_once()
        kwargs = self.thread.edit.await_args.kwargs
        self.assertEqual(kwargs["archived"], True)
        self.assertEqual(kwargs["locked"], False)

    async def test_archive_with_lock(self):
        response = await self.client.post(
            "/threads/11/archive", json={"archived": True, "locked": True}
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.thread.edit.await_args.kwargs
        self.assertEqual(kwargs["archived"], True)
        self.assertEqual(kwargs["locked"], True)

    async def test_unarchive_round_trip(self):
        response = await self.client.post(
            "/threads/11/archive", json={"archived": False, "locked": False}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["archived"], False)
        kwargs = self.thread.edit.await_args.kwargs
        self.assertEqual(kwargs["archived"], False)

    async def test_missing_or_not_a_thread_is_404(self):
        self.bot.get_channel.return_value = Mock(spec=discord.TextChannel)
        self.assertEqual((await self.client.post("/threads/11/archive")).status_code, 404)
        self.thread.edit.assert_not_awaited()

    async def test_outside_configured_server_is_403(self):
        self.thread.guild.id = 2
        response = await self.client.post("/threads/11/archive")
        self.assertEqual(response.status_code, 403)
        self.thread.edit.assert_not_awaited()

    async def test_discord_forbidden_is_403(self):
        self.thread.edit = AsyncMock(
            side_effect=discord.Forbidden(Mock(status=403), "Missing Permissions")
        )
        response = await self.client.post("/threads/11/archive")
        self.assertEqual(response.status_code, 403)

    async def test_other_discord_failure_is_502(self):
        self.thread.edit = AsyncMock(
            side_effect=discord.HTTPException(Mock(status=500), "upstream exploded")
        )
        response = await self.client.post("/threads/11/archive")
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
