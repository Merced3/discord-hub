"""Offline integration checks. No real .env, token, Discord, or project state.

Discord operations are mocked; callback delivery uses real localhost HTTP.
Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp import web
import discord
import httpx

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandSpec, CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registration, Registry


class MultiClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = Settings(_env_file=None, discord_token="fake-token", discord_guild_id=1)
        self.registry = Registry(self.root / "registrations.json")
        self.commands = CommandStore(self.root / "commands.json")
        self.bot = HubBot(settings, self.registry, self.commands)
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.events = {"a": [], "b": []}
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.received_b = asyncio.Event()

        async def callback(request):
            project = request.match_info["project"]
            payload = await request.json()
            self.events[project].append(payload)
            if project == "b":
                self.received_b.set()
            if request.query.get("block"):
                self.blocked.set()
                await self.release.wait()
            if request.query.get("fail"):
                return web.Response(status=503)
            return web.json_response({"text": project, "ephemeral": True})

        app = web.Application()
        app.router.add_post("/{project}", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        self.addCleanup(self.release.set)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        self.urls = {p: f"http://127.0.0.1:{port}/{p}" for p in self.events}
        self.channels = {}
        for cid, project in [(10, "a"), (20, "b")]:
            channel = Mock(spec=discord.TextChannel)
            channel.id = cid
            channel.create_webhook = AsyncMock(return_value=SimpleNamespace(
                url=f"https://example.invalid/{project}", delete=AsyncMock()
            ))
            self.channels[cid] = channel
        self.bot.get_channel = Mock(side_effect=self.channels.get)
        self.bot.tree.sync = AsyncMock(side_effect=lambda **kw: self.bot.tree.get_commands(**kw))

    async def register_both(self):
        for cid, project in [(10, "a"), (20, "b")]:
            response = await self.client.post("/registrations", json={
                "channel_id": cid, "callback_url": self.urls[project],
                "display_name": project.upper(), "avatar_url": f"https://example.invalid/{project}.png",
            })
            self.assertEqual(response.status_code, 201, response.text)

    def message(self, cid, *, bot=False):
        return SimpleNamespace(
            id=1000 + cid, channel=self.channels.get(cid, SimpleNamespace(id=cid)),
            author=SimpleNamespace(bot=bot, id=5, display_name="Human"),
            content="hello", created_at=datetime.now(UTC),
        )

    def thread(self, cid, parent):
        thread = Mock(spec=discord.Thread)
        thread.id, thread.parent_id, thread.name = cid, parent, f"thread-{cid}"
        self.channels[cid] = thread
        return thread

    async def test_two_channel_routes_and_unregistered_and_bot_messages(self):
        await self.register_both()
        await asyncio.gather(*(self.bot.on_message(self.message(cid)) for cid in [10, 20, 99]))
        await self.bot.on_message(self.message(10, bot=True))
        self.assertEqual([e["channel_id"] for e in self.events["a"]], ["10"])
        self.assertEqual([e["channel_id"] for e in self.events["b"]], ["20"])
        reloaded = Registry(self.root / "registrations.json")
        self.assertEqual({r.channel_id for r in reloaded.all()}, {10, 20})

    async def test_threads_inherit_only_their_parent_route(self):
        await self.register_both()
        self.thread(11, 10)
        self.thread(21, 20)
        self.thread(91, 99)
        await asyncio.gather(*(self.bot.on_message(self.message(cid)) for cid in [11, 21, 91]))
        for project, cid, parent in [("a", 11, 10), ("b", 21, 20)]:
            self.assertEqual(len(self.events[project]), 1)
            self.assertEqual(self.events[project][0]["thread"], {
                "id": str(cid), "name": f"thread-{cid}", "parent_channel_id": str(parent),
            })

    async def test_outbound_identity_on_channels_and_threads(self):
        await self.register_both()
        self.thread(11, 10)
        self.thread(21, 20)
        hooks = {p: SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
                 for p in ["a", "b"]}
        with patch("discord_hub.bot.discord.Webhook.from_url",
                   side_effect=lambda url, **kw: hooks[url.rsplit("/", 1)[-1]]):
            for cid in [10, 11, 20, 21]:
                response = await self.client.post("/messages", json={"channel_id": cid, "text": "hi"})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["via"], "webhook")
        for project, tid in [("a", 11), ("b", 21)]:
            calls = hooks[project].send.await_args_list
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertEqual(call.kwargs["username"], project.upper())
                self.assertEqual(call.kwargs["avatar_url"], f"https://example.invalid/{project}.png")
            self.assertIs(calls[1].kwargs["thread"], self.channels[tid])

    async def test_duplicate_registration_returns_409_without_creating_webhook(self):
        await self.register_both()
        response = await self.client.post("/registrations", json={
            "channel_id": 10, "callback_url": self.urls["b"],
        })
        self.assertEqual(response.status_code, 409)
        self.channels[10].create_webhook.assert_awaited_once()
        self.assertEqual(self.registry.get(10).callback_url, self.urls["a"])

    async def test_concurrent_claims_create_only_one_webhook(self):
        responses = await asyncio.gather(*(self.client.post("/registrations", json={
            "channel_id": 10, "callback_url": self.urls[p],
        }) for p in ["a", "b"]))
        self.assertEqual(sorted(r.status_code for r in responses), [201, 409])
        self.channels[10].create_webhook.assert_awaited_once()

    async def test_slow_project_does_not_block_healthy_project_and_times_out(self):
        await self.register_both()
        self.registry.get(10).callback_url += "?block=1"
        with patch("discord_hub.bot._MESSAGE_CALLBACK_BUDGET_S", 0.25):
            task = asyncio.create_task(self.bot.on_message(self.message(10)))
            await asyncio.wait_for(self.blocked.wait(), 2)
            await self.bot.on_message(self.message(20))
            self.assertTrue(self.received_b.is_set())
            self.assertFalse(task.done(), "B must finish while A is still blocked")
            with self.assertLogs("discord_hub.bot", level="WARNING"):
                await asyncio.wait_for(task, 2)
        self.release.set()
        self.assertEqual(len(self.events["b"]), 1)

    async def test_http_failure_does_not_break_other_project(self):
        await self.register_both()
        self.registry.get(10).callback_url += "?fail=1"
        with self.assertLogs("discord_hub.bot", level="WARNING"):
            await asyncio.gather(self.bot.on_message(self.message(10)), self.bot.on_message(self.message(20)))
        self.assertEqual(len(self.events["b"]), 1)

    async def test_refused_callback_does_not_break_other_project(self):
        await self.register_both()
        # Reserve and then release a local port, leaving no callback listening.
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        self.registry.get(10).callback_url = f"http://127.0.0.1:{port}/offline"
        with self.assertLogs("discord_hub.bot", level="WARNING"):
            await asyncio.gather(self.bot.on_message(self.message(10)), self.bot.on_message(self.message(20)))
        self.assertEqual(len(self.events["b"]), 1)

    async def declare(self, project, names):
        return await self.client.put("/commands", json={
            "callback_url": self.urls[project],
            "commands": [{"name": name, "description": "Test command"} for name in names],
        })

    async def test_two_command_sets_conflict_and_independent_replacement(self):
        results = await asyncio.gather(self.declare("a", ["alpha"]), self.declare("b", ["beta"]))
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertEqual((await self.declare("b", ["alpha"])).status_code, 409)
        self.assertEqual(self.commands.owner_of("alpha"), self.urls["a"])
        self.assertEqual(self.commands.owner_of("beta"), self.urls["b"])
        await self.declare("a", ["gamma"])
        names = {c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=1))}
        self.assertEqual(names, {"gamma", "beta"})
        await self.declare("a", [])
        self.assertEqual(self.commands.owner_of("beta"), self.urls["b"])

    async def test_command_invocations_reach_only_the_declaring_project(self):
        for project, name in [("a", "alpha"), ("b", "beta")]:
            await self.declare(project, [name])
            interaction = SimpleNamespace(
                id=10 if project == "a" else 20, channel_id=10,
                user=SimpleNamespace(id=5, display_name="Human"),
                response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            )
            command = self.bot.tree.get_command(name, guild=discord.Object(id=1))
            await command.callback(interaction)
            interaction.response.send_message.assert_awaited_once_with(project, ephemeral=True)
        self.assertEqual([e["command"] for e in self.events["a"]], ["alpha"])
        self.assertEqual([e["command"] for e in self.events["b"]], ["beta"])

    async def test_failed_sync_keeps_routes_and_persisted_commands(self):
        await self.declare("a", ["alpha"])
        await self.declare("b", ["beta"])
        before = (self.root / "commands.json").read_text()
        self.bot.tree.sync.side_effect = RuntimeError("fake Discord failure")
        with self.assertRaises(RuntimeError):
            await self.bot.set_commands(self.urls["b"], [CommandSpec("gamma", "Test")])
        self.assertEqual((self.root / "commands.json").read_text(), before)
        self.assertIsNone(self.commands.owner_of("gamma"))
        self.assertEqual({c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=1))},
                         {"alpha", "beta"})

    async def test_duplicate_command_declaration_rejected_before_mutation(self):
        await self.declare("a", ["alpha"])
        response = await self.declare("b", ["beta", "beta"])
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.commands.owner_of("beta"))

    async def test_failed_registration_save_rolls_back_and_cleans_up_webhook(self):
        with patch.object(self.registry, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.bot.register(10, self.urls["a"])
        self.assertIsNone(self.registry.get(10))
        self.channels[10].create_webhook.return_value.delete.assert_awaited_once()

    async def test_failed_removal_preserves_registration(self):
        await self.register_both()
        with patch.object(self.registry, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.registry.remove(10)
        self.assertIsNotNone(self.registry.get(10))


if __name__ == "__main__":
    unittest.main()
