"""Offline tests: unreachable command callback -> honest ephemeral reply."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from discord_hub.bot import HubBot
from discord_hub.commands import CommandSpec, CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry

CALLBACK = "http://127.0.0.1:1/dead"  # port 1: nothing listens there


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._body


def _interaction():
    return SimpleNamespace(
        id=1,
        channel_id=5,
        user=SimpleNamespace(id=7, display_name="User"),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


class OfflineReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.store = CommandStore(root / "cmds.json")
        self.bot = HubBot(settings, Registry(root / "regs.json"), self.store)
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)
        self.spec = CommandSpec(name="ping", description="Test")
        self.store.set_project_commands(CALLBACK, [self.spec])

    async def test_connection_failure_answers_offline_notice_not_defer(self):
        interaction = _interaction()
        with self.assertLogs("discord_hub.bot", level="ERROR"):
            await self.bot._invoke_command(self.spec, interaction, {})
        interaction.response.send_message.assert_awaited_once()
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("unreachable", text)
        self.assertEqual(interaction.response.send_message.await_args.kwargs["ephemeral"], True)
        interaction.response.defer.assert_not_awaited()

    async def test_timeout_answers_offline_notice(self):
        self.bot._http.post = Mock(side_effect=TimeoutError("slow project"))
        interaction = _interaction()
        with self.assertLogs("discord_hub.bot", level="ERROR"):
            await self.bot._invoke_command(self.spec, interaction, {})
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()

    async def test_offline_notice_leaks_no_internals(self):
        interaction = _interaction()
        with self.assertLogs("discord_hub.bot", level="ERROR"):
            await self.bot._invoke_command(self.spec, interaction, {})
        text = interaction.response.send_message.await_args.args[0]
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn(CALLBACK, text)
        self.assertNotIn("http", text.lower().replace("https", "http", 0))

    async def test_normal_callback_path_unchanged(self):
        self.bot._http.post = Mock(return_value=_FakeResp({"text": "pong"}))
        interaction = _interaction()
        await self.bot._invoke_command(self.spec, interaction, {})
        interaction.response.send_message.assert_awaited_once_with("pong", ephemeral=False)


if __name__ == "__main__":
    unittest.main()
