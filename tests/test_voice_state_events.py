"""Offline tests for routed voice-state events (join/leave/move -> projects).

Discord operations are mocked; callback delivery uses real localhost HTTP.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import discord
from aiohttp import web

from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry


class VoiceStateEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.registry = Registry(root / "r.json")
        self.bot = HubBot(settings, self.registry, CommandStore(root / "c.json"))
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)

        self.received: list[tuple[str, dict]] = []

        async def callback(request: web.Request) -> web.Response:
            self.received.append((request.path, await request.json()))
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/{project}", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = runner.addresses[0][1]
        self.url_a = f"http://127.0.0.1:{port}/a"
        self.url_b = f"http://127.0.0.1:{port}/b"

        self.member = SimpleNamespace(id=7, display_name="cedgo", bot=False)

    def register(self, channel_id: int, callback_url: str, voice_events: bool) -> None:
        from discord_hub.registry import Registration

        self.registry.add(
            Registration(
                channel_id=channel_id,
                callback_url=callback_url,
                voice_events=voice_events,
            )
        )

    @staticmethod
    def voice_state(channel_id: int | None) -> discord.VoiceState:
        channel = SimpleNamespace(id=channel_id) if channel_id else None
        return Mock(spec=discord.VoiceState, channel=channel)

    async def test_opted_in_project_receives_join_event(self):
        self.register(10, self.url_a, voice_events=True)
        await self.bot.on_voice_state_update(
            self.member, self.voice_state(None), self.voice_state(55)
        )
        self.assertEqual(len(self.received), 1)
        path, payload = self.received[0]
        self.assertEqual(path, "/a")
        self.assertEqual(payload["type"], "voice_state")
        self.assertEqual(payload["user"], {"id": "7", "name": "cedgo"})
        self.assertIsNone(payload["before_channel_id"])
        self.assertEqual(payload["after_channel_id"], "55")

    async def test_project_without_opt_in_gets_nothing(self):
        self.register(10, self.url_a, voice_events=False)
        await self.bot.on_voice_state_update(
            self.member, self.voice_state(None), self.voice_state(55)
        )
        self.assertEqual(self.received, [])

    async def test_move_and_leave_payloads(self):
        self.register(10, self.url_a, voice_events=True)
        await self.bot.on_voice_state_update(
            self.member, self.voice_state(55), self.voice_state(56)
        )
        await self.bot.on_voice_state_update(
            self.member, self.voice_state(56), self.voice_state(None)
        )
        self.assertEqual(
            [(p["before_channel_id"], p["after_channel_id"]) for _, p in self.received],
            [("55", "56"), ("56", None)],
        )

    async def test_mute_toggle_is_not_reported(self):
        self.register(10, self.url_a, voice_events=True)
        await self.bot.on_voice_state_update(
            self.member, self.voice_state(55), self.voice_state(55)
        )
        self.assertEqual(self.received, [])

    async def test_bot_users_are_not_reported(self):
        self.register(10, self.url_a, voice_events=True)
        bot_member = SimpleNamespace(id=8, display_name="jockie", bot=True)
        await self.bot.on_voice_state_update(
            bot_member, self.voice_state(None), self.voice_state(55)
        )
        self.assertEqual(self.received, [])

    async def test_delivered_once_per_project_not_per_channel(self):
        self.register(10, self.url_a, voice_events=True)
        self.register(11, self.url_a, voice_events=True)
        self.register(12, self.url_b, voice_events=True)
        await self.bot.on_voice_state_update(
            self.member, self.voice_state(None), self.voice_state(55)
        )
        self.assertEqual(sorted(path for path, _ in self.received), ["/a", "/b"])

    async def test_flag_persists_across_registry_reload(self):
        self.register(10, self.url_a, voice_events=True)
        reloaded = Registry(self.registry._path)
        self.assertTrue(reloaded.get(10).voice_events)
        # Old registrations without the field still load (default False).
        self.registry._path.write_text(
            '[{"channel_id": 99, "callback_url": "http://x/cb"}]', encoding="utf-8"
        )
        self.assertFalse(Registry(self.registry._path).get(99).voice_events)


if __name__ == "__main__":
    unittest.main()
