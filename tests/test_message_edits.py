"""Offline checks for message edits: inbound detection (on_message_edit /
on_raw_message_edit routing) and outbound editing (PATCH /messages/{id}).

No real .env, token, Discord, or project state. Discord operations are
mocked; callback delivery uses real localhost HTTP.
Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord
import httpx
from aiohttp import web

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry


class MessageEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = Settings(_env_file=None, discord_token="fake-token", discord_guild_id=1)
        self.registry = Registry(Path(self.temp.name) / "registrations.json")
        self.bot = HubBot(
            settings, self.registry, CommandStore(Path(self.temp.name) / "commands.json")
        )
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)
        # Webhook.from_url pulls the HTTP session off the client, which is
        # normally established at login; tests never log in, so inject it.
        self.bot.http._HTTPClient__session = self.bot._http
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)

        self.events = []

        async def callback(request):
            self.events.append(await request.json())
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/cb", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        self.callback_url = f"http://127.0.0.1:{port}/cb"

        self.channels = {}
        self.bot.get_channel = Mock(side_effect=self.channels.get)
        self.bot.fetch_channel = AsyncMock(side_effect=LookupError("unknown channel"))
        # Channel 10 exists before registration: registering creates the
        # project's webhook, which needs the channel. The webhook URL must
        # match discord.py's format (17-19 digit id, 60-68 char token).
        self.channel(10).create_webhook = AsyncMock(
            return_value=SimpleNamespace(
                url="https://discord.com/api/webhooks/123456789012345678/" + "a" * 60
            )
        )
        response = await self.client.post(
            "/registrations",
            json={
                "channel_id": 10,
                "callback_url": self.callback_url,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)

    def channel(self, cid=10):
        if cid in self.channels:
            return self.channels[cid]
        channel = Mock(spec=discord.TextChannel)
        channel.id = cid
        self.channels[cid] = channel
        return channel

    def message(self, cid=10, *, bot=False, text="edited", webhook_id=None):
        now = datetime.now(UTC)
        return SimpleNamespace(
            id=555,
            channel=self.channels.get(cid, SimpleNamespace(id=cid)),
            author=SimpleNamespace(bot=bot, id=5, display_name="Human"),
            content=text,
            created_at=now,
            edited_at=now,
            webhook_id=webhook_id,
        )

    def thread(self, cid, parent):
        thread = Mock(spec=discord.Thread)
        thread.id, thread.parent_id, thread.name = cid, parent, f"thread-{cid}"
        self.channels[cid] = thread
        return thread

    # -- inbound detection ------------------------------------------------

    async def test_cached_edit_delivers_before_and_after(self):
        self.channel()
        before = self.message(text="original")
        after = self.message(text="original, fixed")
        await self.bot.on_message_edit(before, after)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event["type"], "message_edit")
        self.assertEqual(event["message_id"], "555")
        self.assertEqual(event["channel_id"], "10")
        self.assertEqual(event["before_text"], "original")
        self.assertEqual(event["after_text"], "original, fixed")
        self.assertEqual(event["author"]["name"], "Human")
        self.assertIsNotNone(event["edited_at"])

    async def test_every_edit_is_its_own_event(self):
        self.channel()
        await self.bot.on_message_edit(self.message(text="A"), self.message(text="B"))
        await self.bot.on_message_edit(self.message(text="B"), self.message(text="C"))
        self.assertEqual(
            [(e["before_text"], e["after_text"]) for e in self.events],
            [("A", "B"), ("B", "C")],
        )

    async def test_edit_in_thread_inherits_parent_route(self):
        self.thread(11, 10)
        await self.bot.on_message_edit(self.message(11, text="A"), self.message(11, text="B"))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(
            self.events[0]["thread"],
            {
                "id": "11",
                "name": "thread-11",
                "parent_channel_id": "10",
            },
        )

    async def test_edit_from_bot_or_unregistered_channel_is_ignored(self):
        self.channel()
        await self.bot.on_message_edit(self.message(bot=True), self.message(bot=True))
        await self.bot.on_message_edit(self.message(99), self.message(99))  # not registered
        self.assertEqual(self.events, [])

    async def test_uncached_edit_reports_before_text_none(self):
        channel = self.channel()
        channel.fetch_message = AsyncMock(return_value=self.message(text="new state"))
        event = SimpleNamespace(channel_id=10, message_id=555, cached_message=None)
        await self.bot.on_raw_message_edit(event)
        self.assertEqual(len(self.events), 1)
        self.assertIsNone(self.events[0]["before_text"])
        self.assertEqual(self.events[0]["after_text"], "new state")

    async def test_raw_event_with_cached_message_is_skipped(self):
        event = SimpleNamespace(channel_id=10, message_id=555, cached_message=object())
        await self.bot.on_raw_message_edit(event)
        self.assertEqual(self.events, [])

    async def test_uncached_edit_of_vanished_message_is_dropped(self):
        channel = self.channel()
        response = Mock()
        response.status = 404
        response.reason = "Not Found"
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(response, {"message": "Unknown Message"})
        )
        event = SimpleNamespace(channel_id=10, message_id=555, cached_message=None)
        await self.bot.on_raw_message_edit(event)
        self.assertEqual(self.events, [])

    # -- outbound editing ---------------------------------------------------

    async def test_patch_edits_bot_sent_message(self):
        self.bot._connection.user = SimpleNamespace(id=999)
        channel = self.channel()
        edited = self.message(text="fixed")
        message = self.message(text="typo")
        message.author = SimpleNamespace(bot=True, id=999, display_name="hub")
        message.edit = AsyncMock(return_value=edited)
        channel.fetch_message = AsyncMock(return_value=message)

        response = await self.client.patch(
            "/messages/555",
            json={
                "channel_id": 10,
                "text": "fixed",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body,
            {
                "id": "555",
                "channel_id": "10",
                "edited_at": body["edited_at"],
                "via": "bot",
            },
        )
        message.edit.assert_awaited_once_with(content="fixed")

    async def test_patch_edits_webhook_sent_message_via_registration(self):
        channel = self.channel()
        message = self.message(webhook_id=777)
        channel.fetch_message = AsyncMock(return_value=message)
        edited = self.message(text="fixed")
        with patch("discord.Webhook.edit_message", new=AsyncMock(return_value=edited)) as edit:
            response = await self.client.patch(
                "/messages/555",
                json={
                    "channel_id": 10,
                    "text": "fixed",
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["via"], "webhook")
        edit.assert_awaited_once_with(555, content="fixed")

    async def test_patch_rejects_foreign_message_empty_and_oversize(self):
        channel = self.channel()
        human_message = self.message()  # author id 5, no webhook -> not ours
        channel.fetch_message = AsyncMock(return_value=human_message)
        response = await self.client.patch(
            "/messages/555",
            json={
                "channel_id": 10,
                "text": "hijack",
            },
        )
        self.assertEqual(response.status_code, 403)
        response = await self.client.patch(
            "/messages/555",
            json={
                "channel_id": 10,
                "text": "   ",
            },
        )
        self.assertEqual(response.status_code, 422)
        response = await self.client.patch(
            "/messages/555",
            json={
                "channel_id": 10,
                "text": "x" * 2001,
            },
        )
        self.assertEqual(response.status_code, 422)

    async def test_patch_unknown_message_and_channel(self):
        channel = self.channel()
        response = Mock()
        response.status = 404
        response.reason = "Not Found"
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(response, {"message": "Unknown Message"})
        )
        response = await self.client.patch(
            "/messages/555",
            json={
                "channel_id": 10,
                "text": "fixed",
            },
        )
        self.assertEqual(response.status_code, 404)
        response = await self.client.patch(
            "/messages/555",
            json={
                "channel_id": 99,
                "text": "fixed",
            },
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
