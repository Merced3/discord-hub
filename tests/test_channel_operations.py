"""Offline safety/contract tests for deletion and paginated history."""
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord
import httpx

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registration, Registry


class ChannelOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.registry = Registry(root / "regs.json")
        self.bot = HubBot(settings, self.registry, CommandStore(root / "cmds.json"))
        self.thread = Mock(spec=discord.Thread)
        self.thread.id, self.thread.parent_id = 11, 10
        self.thread.guild = SimpleNamespace(id=1, me=object())
        self.thread.delete = AsyncMock()
        self.thread.permissions_for.return_value = discord.Permissions(
            view_channel=True, read_message_history=True
        )
        self.bot.get_channel = Mock(return_value=self.thread)
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)

    def opt_in(self, channel_id: int = 10, flag: bool = True) -> None:
        self.registry.add(Registration(
            channel_id=channel_id, callback_url="http://127.0.0.1:9/cb",
            allow_thread_deletion=flag,
        ))

    async def test_deletion_refused_when_parent_not_opted_in(self):
        self.opt_in(flag=False)
        response = await self.client.delete("/threads/11")
        self.assertEqual(response.status_code, 403)
        self.thread.delete.assert_not_awaited()

    async def test_deletion_refused_when_parent_unregistered(self):
        response = await self.client.delete("/threads/11")
        self.assertEqual(response.status_code, 403)
        self.thread.delete.assert_not_awaited()

    async def test_deletion_allowed_only_under_opted_in_parent_and_configured_guild(self):
        self.opt_in()
        self.thread.parent_id = 20  # different, unregistered parent
        self.assertEqual((await self.client.delete("/threads/11")).status_code, 403)
        self.thread.parent_id, self.thread.guild.id = 10, 2  # wrong server
        self.assertEqual((await self.client.delete("/threads/11")).status_code, 403)
        self.thread.delete.assert_not_awaited()
        self.thread.guild.id = 1
        response = await self.client.delete("/threads/11")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.thread.delete.assert_awaited_once()

    async def test_deletion_never_deletes_a_parent_channel(self):
        self.opt_in()
        parent = Mock(spec=discord.TextChannel)
        parent.delete = AsyncMock()
        self.bot.get_channel.return_value = parent
        self.assertEqual((await self.client.delete("/threads/10")).status_code, 404)
        parent.delete.assert_not_awaited()

    async def test_archived_uncached_thread_can_be_resolved(self):
        self.opt_in()
        self.bot.get_channel.return_value = None
        self.thread.archived = True
        self.assertEqual((await self.client.delete("/threads/11")).status_code, 204)
        self.bot.fetch_channel.assert_awaited_once_with(11)

    async def test_discord_deletion_errors_have_explicit_statuses(self):
        self.opt_in()
        for error_cls, status, expected in [
            (discord.Forbidden, 403, 403), (discord.NotFound, 404, 404),
            (discord.HTTPException, 500, 502),
        ]:
            with self.subTest(status=status):
                self.thread.delete.side_effect = error_cls(
                    SimpleNamespace(status=status, reason="fake"), {"message": "fake", "code": 0}
                )
                self.assertEqual((await self.client.delete("/threads/11")).status_code, expected)

    def install_history(self):
        def message(mid):
            return SimpleNamespace(
                id=mid, author=SimpleNamespace(id=7, display_name="User", bot=False),
                created_at=datetime(2026, 1, 1, tzinfo=UTC), edited_at=None,
                content=f"message-{mid}", attachments=[SimpleNamespace(
                    id=8, filename="note.txt", size=3, content_type="text/plain",
                    url="https://example.invalid/attachment",
                )],
            )

        async def history(*, limit, before, oldest_first):
            for mid in [300, 200, 100][:]:
                if before is None or mid < before.id:
                    if limit == 0:
                        break
                    yield message(mid)
                    limit -= 1
        self.thread.history = Mock(side_effect=history)

    async def test_history_pagination_is_newest_first_and_exclusive(self):
        self.install_history()
        first = await self.client.get("/channels/11/history?limit=2")
        self.assertEqual(first.status_code, 200)
        page = first.json()
        self.assertEqual([m["id"] for m in page["messages"]], ["300", "200"])
        self.assertEqual(page["next_before"], "200")
        self.assertEqual(page["messages"][0]["attachments"][0]["filename"], "note.txt")
        self.assertEqual(page["messages"][0]["author"]["id"], "7")
        self.assertEqual(page["messages"][0]["edited_at"], None)
        second = (await self.client.get("/channels/11/history?limit=2&before=200")).json()
        self.assertEqual([m["id"] for m in second["messages"]], ["100"])
        self.assertIsNone(second["next_before"])
        empty = (await self.client.get("/channels/11/history?before=100")).json()
        self.assertEqual(empty["messages"], [])
        self.assertIsNone(empty["next_before"])
        for call in self.thread.history.call_args_list:
            self.assertFalse(call.kwargs["oldest_first"])

    async def test_history_rejects_invalid_bounds_before_discord_access(self):
        for query in ["limit=0", "limit=101", "before=0", "before=not-an-id"]:
            with self.subTest(query=query):
                self.assertEqual((await self.client.get(f"/channels/11/history?{query}")).status_code, 422)
        self.bot.get_channel.assert_not_called()

    async def test_history_requires_configured_server_and_read_permissions(self):
        self.install_history()
        self.thread.guild.id = 2
        self.assertEqual((await self.client.get("/channels/11/history")).status_code, 403)
        self.thread.guild.id = 1
        self.thread.permissions_for.return_value = discord.Permissions(view_channel=True)
        self.assertEqual((await self.client.get("/channels/11/history")).status_code, 403)
        self.thread.history.assert_not_called()

    async def test_history_rejects_forum_container(self):
        self.bot.get_channel.return_value = Mock(spec=discord.ForumChannel)
        self.assertEqual((await self.client.get("/channels/11/history")).status_code, 404)

    async def test_history_discord_not_found_and_forbidden(self):
        self.bot.get_channel.return_value = None
        for error_cls, status in [(discord.NotFound, 404), (discord.Forbidden, 403)]:
            with self.subTest(status=status):
                self.bot.fetch_channel.side_effect = error_cls(
                    SimpleNamespace(status=status, reason="fake"), {"message": "fake", "code": 0}
                )
                self.assertEqual((await self.client.get("/channels/11/history")).status_code, status)
