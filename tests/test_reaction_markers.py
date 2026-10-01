"""Offline tests for reaction markers (emoji -> marker routing to projects).

Discord operations are mocked; callback delivery uses real localhost HTTP.
"""
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
import unittest.mock

import aiohttp
from aiohttp import web

from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry


def make_event(
    emoji: str, *, message_id: int = 500, channel_id: int = 42,
    user_id: int = 7, guild_id: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        emoji=emoji, message_id=message_id, channel_id=channel_id,
        user_id=user_id, guild_id=guild_id,
    )


class ReactionMarkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(
            _env_file=None, discord_token="fake", discord_guild_id=1,
            reaction_markers={"🔄": "belief_change", "🔒": "private"},
        )
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

    def register(self, channel_id: int, callback_url: str, reaction_events: bool) -> None:
        from discord_hub.registry import Registration

        self.registry.add(Registration(
            channel_id=channel_id, callback_url=callback_url,
            reaction_events=reaction_events,
        ))

    async def test_opted_in_project_receives_add_marker(self):
        self.register(10, self.url_a, reaction_events=True)
        await self.bot.on_raw_reaction_add(make_event("🔄"))
        self.assertEqual(len(self.received), 1)
        path, payload = self.received[0]
        self.assertEqual(path, "/a")
        self.assertEqual(payload["type"], "reaction_marker")
        self.assertEqual(payload["action"], "added")
        self.assertEqual(payload["emoji"], "🔄")
        self.assertEqual(payload["marker"], "belief_change")
        self.assertEqual(payload["message_id"], "500")
        self.assertEqual(payload["channel_id"], "42")
        self.assertEqual(payload["user_id"], "7")

    async def test_remove_undoes_the_mark(self):
        self.register(10, self.url_a, reaction_events=True)
        await self.bot.on_raw_reaction_remove(make_event("🔒"))
        self.assertEqual(self.received[0][1]["action"], "removed")
        self.assertEqual(self.received[0][1]["marker"], "private")

    async def test_unconfigured_emoji_is_ignored(self):
        self.register(10, self.url_a, reaction_events=True)
        await self.bot.on_raw_reaction_add(make_event("👍"))
        self.assertEqual(self.received, [])

    async def test_project_without_opt_in_gets_nothing(self):
        self.register(10, self.url_a, reaction_events=False)
        await self.bot.on_raw_reaction_add(make_event("🔄"))
        self.assertEqual(self.received, [])

    async def test_other_guild_is_ignored(self):
        self.register(10, self.url_a, reaction_events=True)
        await self.bot.on_raw_reaction_add(make_event("🔄", guild_id=999))
        self.assertEqual(self.received, [])

    async def test_hub_own_reaction_is_not_a_mark(self):
        self.register(10, self.url_a, reaction_events=True)
        bot_user = property(lambda self: SimpleNamespace(id=31337))
        with unittest.mock.patch.object(type(self.bot), "user", bot_user):
            await self.bot.on_raw_reaction_add(make_event("🔄", user_id=31337))
        self.assertEqual(self.received, [])

    async def test_delivered_once_per_project_not_per_channel(self):
        self.register(10, self.url_a, reaction_events=True)
        self.register(11, self.url_a, reaction_events=True)
        self.register(12, self.url_b, reaction_events=True)
        await self.bot.on_raw_reaction_add(make_event("🔄"))
        self.assertEqual(sorted(path for path, _ in self.received), ["/a", "/b"])

    async def test_flag_persists_across_registry_reload(self):
        self.register(10, self.url_a, reaction_events=True)
        reloaded = Registry(self.registry._path)
        self.assertTrue(reloaded.get(10).reaction_events)
        # Old registrations without the field still load (default False).
        self.registry._path.write_text(
            '[{"channel_id": 99, "callback_url": "http://x/cb"}]', encoding="utf-8"
        )
        self.assertFalse(Registry(self.registry._path).get(99).reaction_events)


if __name__ == "__main__":
    unittest.main()
