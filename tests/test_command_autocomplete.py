"""Offline tests for autocomplete routing to project callbacks."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp

from discord_hub.bot import HubBot
from discord_hub.commands import CommandSpec, CommandStore, build_command
from discord_hub.config import Settings
from discord_hub.registry import Registry

CALLBACK = "http://127.0.0.1:9/cb"


class _FakeResp:
    """Async context manager mimicking aiohttp's response."""

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
        namespace=SimpleNamespace(model="gpt"),
    )


class AutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(_env_file=None, discord_token="fake", discord_guild_id=1)
        self.store = CommandStore(root / "cmds.json")
        self.bot = HubBot(settings, Registry(root / "regs.json"), self.store)
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)
        self.spec = CommandSpec(
            name="model",
            description="switch model",
            options=[{"name": "model", "type": "string", "autocomplete": True}],
        )
        self.store.set_project_commands(CALLBACK, [self.spec])

    async def test_build_command_wires_autocomplete_per_option(self):
        spec = CommandSpec(
            name="c",
            description="d",
            options=[
                {"name": "model", "type": "string", "autocomplete": True},
                {"name": "plain", "type": "string"},
            ],
        )
        cmd = build_command(spec, AsyncMock(), AsyncMock(return_value=[]))
        handlers = cmd.callback.__discord_app_commands_param_autocomplete__
        self.assertIn("model", handlers)
        self.assertNotIn("plain", handlers)

    async def test_routes_event_and_returns_choices(self):
        self.bot._http.post = Mock(
            return_value=_FakeResp({"choices": [{"name": "gpt-5", "value": "gpt-5"}]})
        )
        choices = await self.bot._autocomplete(self.spec, _interaction(), "model", "gp")
        self.assertEqual(choices, [{"name": "gpt-5", "value": "gpt-5"}])
        event = self.bot._http.post.call_args.kwargs["json"]
        self.assertEqual(event["type"], "autocomplete")
        self.assertEqual(event["command"], "model")
        self.assertEqual(event["option"], {"name": "model", "value": "gp"})
        self.assertEqual(self.bot._http.post.call_args.args[0], CALLBACK)

    async def test_callback_failure_returns_no_choices(self):
        # real session to a dead port: connection refused
        choices = await self.bot._autocomplete(self.spec, _interaction(), "model", "gp")
        self.assertEqual(choices, [])

    async def test_unregistered_command_returns_no_choices(self):
        orphan = CommandSpec(name="nope", description="d")
        choices = await self.bot._autocomplete(orphan, _interaction(), "model", "x")
        self.assertEqual(choices, [])

    async def test_handler_converts_to_choices_and_caps_at_25(self):
        raw = [{"name": f"n{i}", "value": f"v{i}"} for i in range(30)]
        cmd = build_command(self.spec, AsyncMock(), AsyncMock(return_value=raw))
        complete = cmd.callback.__discord_app_commands_param_autocomplete__["model"]
        choices = await complete(_interaction(), "n")
        self.assertEqual(len(choices), 25)
        self.assertEqual(choices[0].name, "n0")
        self.assertEqual(choices[0].value, "v0")

    async def test_handler_never_raises_on_callback_error(self):
        cmd = build_command(self.spec, AsyncMock(), AsyncMock(side_effect=RuntimeError("boom")))
        complete = cmd.callback.__discord_app_commands_param_autocomplete__["model"]
        self.assertEqual(await complete(_interaction(), "x"), [])


if __name__ == "__main__":
    unittest.main()
