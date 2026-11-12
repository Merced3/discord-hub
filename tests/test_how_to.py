"""Offline contract tests for Courier-owned, generated #how-to content."""

from __future__ import annotations

import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import httpx

from discord_hub.api import create_api
from discord_hub.bot import HubBot
from discord_hub.commands import CommandSpec, CommandStore
from discord_hub.config import Settings
from discord_hub.how_to import HowToManager, HowToStateStore, render_project
from discord_hub.registry import Registry


class HowToRenderingTests(unittest.TestCase):
    def test_render_identifies_project_and_full_command_shape(self):
        spec = CommandSpec(
            "lesson",
            "Control the current lesson.",
            [
                {
                    "name": "action",
                    "description": "What to do.",
                    "type": "string",
                    "required": True,
                },
                {
                    "name": "minutes",
                    "description": "Optional duration.",
                    "type": "integer",
                    "min_value": 1,
                    "max_value": 60,
                    "autocomplete": True,
                },
            ],
        )
        chunks = render_project("teaching-agent", [spec])
        self.assertEqual(len(chunks), 1)
        text = chunks[0]
        self.assertIn("Teaching Agent", text)
        self.assertIn("`teaching-agent`", text)
        self.assertIn("`/lesson` — Control the current lesson.", text)
        self.assertIn("`action` (string, required): What to do.", text)
        self.assertIn(
            "`minutes` (integer, optional, 1..60, autocomplete): Optional duration.", text
        )
        self.assertNotIn("callback", text)

    def test_every_declaration_field_changes_the_rendered_fingerprint(self):
        base = CommandSpec("status", "Show status.")
        original = render_project("project", [base])
        changed = [
            CommandSpec("renamed", "Show status."),
            CommandSpec("status", "Show detailed status."),
            CommandSpec("status", "Show status.", [{"name": "verbose", "type": "boolean"}]),
        ]
        for spec in changed:
            with self.subTest(spec=spec):
                self.assertNotEqual(render_project("project", [spec]), original)
        self.assertNotEqual(render_project("other-project", [base]), original)

    def test_large_project_is_split_with_continuation_headers(self):
        specs = [CommandSpec(f"command-{i:02}", "X" * 90) for i in range(30)]
        chunks = render_project("large-project", specs)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertIn("Large Project (continued)", chunks[1])


class FakeChannel:
    def __init__(self):
        self.messages: dict[int, SimpleNamespace] = {}
        self.next_id = 100
        self.sent = []
        self.edited = []
        self.deleted = []

    def _add(self, text, *, author="Courier"):
        self.next_id += 1
        message = SimpleNamespace(
            id=self.next_id,
            content=text,
            author=SimpleNamespace(name=author),
            edit=AsyncMock(side_effect=partial(self._edit, self.next_id)),
            delete=AsyncMock(side_effect=partial(self._delete, self.next_id)),
        )
        self.messages[message.id] = message
        return message

    async def _edit(self, message_id, *, content):
        message = self.messages[message_id]
        message.content = content
        self.edited.append(message_id)
        return message

    async def _delete(self, message_id):
        self.deleted.append(message_id)
        del self.messages[message_id]

    async def send(self, content):
        message = self._add(content)
        self.sent.append(message.id)
        return message

    async def fetch_message(self, message_id):
        self._current_id = message_id
        try:
            return self.messages[message_id]
        except KeyError as exc:
            raise discord.NotFound(Mock(status=404), "missing") from exc

    def history(self, limit=100, before=None, oldest_first=False):
        messages = sorted(self.messages.values(), key=lambda m: m.id, reverse=True)
        if before is not None:
            messages = [m for m in messages if m.id < before.id]

        async def iterate():
            for message in messages[:limit]:
                self._current_id = message.id
                yield message

        return iterate()


class HowToManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = HowToStateStore(self.root / "how-to.json")
        self.channel = FakeChannel()
        self.manager = HowToManager(self.channel, self.state)

    async def reconcile(self, projects):
        await self.manager.reconcile(projects)

    def projects(self, description="Show status."):
        return [
            {
                "project_id": "alpha",
                "commands": [{"name": "status", "description": description, "options": []}],
            },
            {
                "project_id": "beta",
                "commands": [{"name": "ping", "description": "Ping.", "options": []}],
            },
        ]

    async def test_initial_reconcile_purges_legacy_content_once(self):
        for _ in range(5):
            self.channel._add("old copied text", author="Owner")
        await self.reconcile(self.projects())
        self.assertFalse(
            any(m.content == "old copied text" for m in self.channel.messages.values())
        )
        generated = set(self.channel.messages)
        await self.reconcile(self.projects())
        self.assertEqual(set(self.channel.messages), generated)
        self.assertEqual(self.channel.sent[-1], max(generated))
        self.assertEqual(len(self.channel.sent), 3)

    async def test_only_changed_project_is_edited(self):
        await self.reconcile(self.projects())
        before = {m.id: m.content for m in self.channel.messages.values()}
        await self.reconcile(self.projects("Show detailed runtime status."))
        changed = [
            mid
            for mid, text in before.items()
            if self.channel.messages[mid].content != text
        ]
        self.assertEqual(len(changed), 1)
        self.assertIn("Show detailed runtime status.", self.channel.messages[changed[0]].content)

    async def test_project_chunks_grow_and_shrink_without_stale_messages(self):
        await self.reconcile(self.projects())
        many = [
            {"project_id": "alpha", "commands": [
                {"name": f"command-{i:02}", "description": "X" * 90, "options": []}
                for i in range(30)
            ]},
            self.projects()[1],
        ]
        await self.reconcile(many)
        alpha_chunks = [m for m in self.channel.messages.values() if "Alpha" in m.content]
        self.assertGreater(len(alpha_chunks), 1)
        await self.reconcile(self.projects())
        self.assertEqual(sum("Alpha" in m.content for m in self.channel.messages.values()), 1)

    async def test_removed_project_and_empty_project_entries_are_deleted(self):
        await self.reconcile(self.projects())
        await self.reconcile([self.projects()[1]])
        self.assertFalse(any("Alpha" in m.content for m in self.channel.messages.values()))
        self.assertTrue(any("Beta" in m.content for m in self.channel.messages.values()))

    async def test_missing_stored_message_is_recreated_and_state_survives_restart(self):
        await self.reconcile(self.projects())
        missing = next(m for m in list(self.channel.messages.values()) if "Alpha" in m.content)
        await self.channel.fetch_message(missing.id)
        await missing.delete()
        restarted = HowToManager(self.channel, HowToStateStore(self.root / "how-to.json"))
        await restarted.reconcile(self.projects())
        self.assertTrue(any("Alpha" in m.content for m in self.channel.messages.values()))


class HowToBotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_command_change_triggers_redacted_reconciliation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                _env_file=None,
                discord_token="fake-token",
                discord_guild_id=1,
                how_to_channel_id=42,
                hub_auth_mode="required",
                hub_project_tokens={"alpha": "a-secret"},
            )
            commands = CommandStore(root / "commands.json")
            bot = HubBot(settings, Registry(root / "registrations.json"), commands)
            bot.get_channel = Mock(return_value=Mock(spec=discord.TextChannel))
            bot.tree.sync = AsyncMock(
                side_effect=lambda **kw: bot.tree.get_commands(**kw)
            )
            with patch("discord_hub.bot.HowToManager") as manager_type:
                manager = manager_type.return_value
                manager.reconcile = AsyncMock()
                synced = await bot.set_commands(
                    "http://alpha.internal/callback",
                    [CommandSpec("status", "A")],
                    "alpha",
                )
            self.assertEqual(synced, 1)
            manager.reconcile.assert_awaited_once_with([
                {
                    "project_id": "alpha",
                    "commands": [{"name": "status", "description": "A", "options": []}],
                }
            ])
            self.assertNotIn("callback", str(manager.reconcile.await_args))


class RedactedCommandsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        settings = Settings(
            _env_file=None,
            discord_token="fake-token",
            discord_guild_id=1,
            hub_auth_mode="required",
            hub_project_tokens={"alpha": "a-secret", "beta": "b-secret"},
            hub_operator_token="operator-secret",
        )
        self.commands = CommandStore(root / "commands.json")
        self.commands.set_project_commands_for_project(
            "http://alpha.internal/callback", [CommandSpec("status", "A")], "alpha"
        )
        self.commands.set_project_commands_for_project(
            "http://beta.internal/callback", [CommandSpec("ping", "B")], "beta"
        )
        self.bot = HubBot(settings, Registry(root / "registrations.json"), self.commands)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def get(self, token):
        return await self.client.get("/commands", headers={"Authorization": f"Bearer {token}"})

    async def test_project_gets_only_its_redacted_declarations(self):
        response = await self.get("a-secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "projects": [{"project_id": "alpha", "commands": [
                {"name": "status", "description": "A", "options": []}
            ]}]
        })
        self.assertNotIn("callback", response.text)
        self.assertNotIn("secret", response.text)

    async def test_operator_gets_all_and_legacy_caller_gets_nothing(self):
        response = await self.get("operator-secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["project_id"] for p in response.json()["projects"]], ["alpha", "beta"])
        self.assertEqual((await self.client.get("/commands")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
