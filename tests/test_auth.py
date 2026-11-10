"""Cross-host API authentication, ownership, and callback signing checks.

All Discord operations are fake. Callback tests use a local HTTP server and
never touch project or production state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from dataclasses import asdict
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
from discord_hub.registry import Registration, Registry

A_TOKEN = "project-a-secret"
B_TOKEN = "project-b-secret"
OPERATOR_TOKEN = "operator-secret"


class AuthApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(
            _env_file=None,
            discord_token="fake-discord-token",
            discord_guild_id=1,
            hub_auth_mode="required",
            hub_project_tokens={"a": A_TOKEN, "b": B_TOKEN},
            hub_operator_token=OPERATOR_TOKEN,
        )
        self.registry = Registry(self.root / "registrations.json")
        self.commands = CommandStore(self.root / "commands.json")
        self.bot = HubBot(self.settings, self.registry, self.commands)
        self.bot._http = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot._http.close)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.callbacks = []
        self.callback_event = __import__("asyncio").Event()

        async def callback(request):
            self.callbacks.append(
                {"headers": request.headers, "body": await request.read()}
            )
            self.callback_event.set()
            return web.json_response({"text": "ok"})

        app = web.Application()
        app.router.add_post("/a", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.callback_url = f"http://127.0.0.1:{runner.addresses[0][1]}/a"
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.create_webhook = AsyncMock(
            return_value=SimpleNamespace(
                url="https://discord.invalid/webhook/secret", delete=AsyncMock()
            )
        )
        self.bot.get_channel = Mock(return_value=self.channel)
        self.bot.tree.sync = AsyncMock(side_effect=lambda **kw: self.bot.tree.get_commands(**kw))

    @property
    def a_headers(self):
        return {"Authorization": f"Bearer {A_TOKEN}"}

    @property
    def b_headers(self):
        return {"Authorization": f"Bearer {B_TOKEN}"}

    @property
    def operator_headers(self):
        return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}

    async def register_a(self):
        response = await self.client.post(
            "/registrations",
            headers=self.a_headers,
            json={"channel_id": 10, "callback_url": self.callback_url},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response

    async def test_health_is_public_but_api_requires_valid_credentials(self):
        self.assertEqual((await self.client.get("/health")).status_code, 200)
        self.assertEqual((await self.client.get("/registrations")).status_code, 401)
        self.assertEqual(
            (await self.client.get("/registrations", headers=self.b_headers)).status_code,
            200,
        )
        response = await self.client.get(
            "/registrations", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("wrong", response.text)

    async def test_registration_ownership_and_secret_redaction(self):
        created = await self.register_a()
        self.assertEqual(created.json()["project_id"], "a")
        self.assertEqual(
            created.json()["webhook_url"], self.channel.create_webhook.return_value.url
        )

        own = await self.client.get("/registrations", headers=self.a_headers)
        self.assertEqual(len(own.json()), 1)
        self.assertNotIn("webhook_url", own.json()[0])
        other = await self.client.get("/registrations", headers=self.b_headers)
        self.assertEqual(other.json(), [])

        response = await self.client.delete("/registrations/10", headers=self.b_headers)
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.registry.get(10))
        with patch(
            "discord_hub.bot.discord.Webhook.from_url",
            return_value=SimpleNamespace(delete=AsyncMock()),
        ):
            response = await self.client.delete("/registrations/10", headers=self.a_headers)
        self.assertEqual(response.status_code, 204)

    async def test_operator_can_snapshot_claim_and_restore_for_original_project(self):
        await self.register_a()
        listed = await self.client.get("/registrations", headers=self.operator_headers)
        self.assertEqual(listed.json()[0]["project_id"], "a")
        self.assertNotIn("webhook_url", listed.json()[0])
        with patch(
            "discord_hub.bot.discord.Webhook.from_url",
            return_value=SimpleNamespace(delete=AsyncMock()),
        ):
            response = await self.client.delete(
                "/registrations/10", headers=self.operator_headers
            )
        self.assertEqual(response.status_code, 204)
        restored = await self.client.post(
            "/registrations",
            headers=self.operator_headers,
            json={
                "channel_id": 10,
                "callback_url": self.callback_url,
                "project_id": "a",
            },
        )
        self.assertEqual(restored.status_code, 201, restored.text)
        self.assertEqual(self.registry.get(10).project_id, "a")

    async def test_project_cannot_send_through_another_projects_identity(self):
        await self.register_a()
        webhook = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        with patch("discord_hub.bot.discord.Webhook.from_url", return_value=webhook):
            response = await self.client.post(
                "/messages", headers=self.b_headers, json={"channel_id": 10, "text": "spoof"}
            )
            self.assertEqual(response.status_code, 403)
            webhook.send.assert_not_called()

            response = await self.client.post(
                "/messages", headers=self.a_headers, json={"channel_id": 10, "text": "mine"}
            )
        self.assertEqual(response.status_code, 201, response.text)

    async def test_callback_signature_covers_exact_body(self):
        await self.register_a()
        message = SimpleNamespace(
            id=100,
            channel=self.channel,
            author=SimpleNamespace(bot=False, id=5, display_name="Human"),
            content="hello",
            created_at=datetime.now(UTC),
        )
        await self.bot.on_message(message)
        self.assertEqual(len(self.callbacks), 1)
        delivered = self.callbacks[0]
        timestamp = delivered["headers"]["X-Discord-Hub-Timestamp"]
        signature = delivered["headers"]["X-Discord-Hub-Signature"]
        expected = hmac.new(
            A_TOKEN.encode(), timestamp.encode() + b"." + delivered["body"], hashlib.sha256
        ).hexdigest()
        self.assertEqual(signature, f"v1={expected}")
        self.assertEqual(delivered["headers"]["X-Discord-Hub-Project"], "a")
        self.assertEqual(json.loads(delivered["body"])["text"], "hello")

    async def test_voice_and_commands_use_authenticated_project_identity(self):
        await self.register_a()
        response = await self.client.put(
            "/commands",
            headers=self.a_headers,
            json={
                "callback_url": self.callback_url,
                "commands": [{"name": "alpha", "description": "A"}],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = await self.client.put(
            "/commands",
            headers=self.b_headers,
            json={
                "callback_url": self.callback_url,
                "commands": [{"name": "beta", "description": "B"}],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.commands.project_of("alpha"), "a")


class RolloutAndStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_mode_keeps_legacy_calls_but_rejects_bad_tokens(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                _env_file=None,
                discord_token="fake",
                discord_guild_id=1,
                hub_auth_mode="optional",
                hub_project_tokens={"a": A_TOKEN},
            )
            bot = HubBot(settings, Registry(root / "r.json"), CommandStore(root / "c.json"))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_api(bot, 0)), base_url="http://hub"
            ) as client:
                self.assertEqual((await client.get("/registrations")).status_code, 200)
                response = await client.get(
                    "/registrations", headers={"Authorization": "Bearer wrong"}
                )
                self.assertEqual(response.status_code, 401)

    async def test_legacy_state_backfill_is_explicit_and_persistent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "registrations.json"
            legacy = Registration(channel_id=10, callback_url="http://consumer/a")
            path.write_text(json.dumps([asdict(legacy)]), encoding="utf-8")
            registry = Registry(path)
            self.assertEqual(registry.unowned(), [legacy])
            registry.backfill_projects({"http://consumer/a": "a"})
            self.assertEqual(registry.get(10).project_id, "a")
            self.assertEqual(Registry(path).get(10).project_id, "a")
            self.assertEqual(registry.unowned(), [])

    async def test_command_project_and_legacy_json_survive_restart(self):
        from discord_hub.commands import CommandSpec

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "commands.json"
            url = "http://consumer/a"
            store = CommandStore(path)
            store.set_project_commands_for_project(
                url, [CommandSpec("alpha", "A")], "a"
            )
            restarted = CommandStore(path)
            self.assertEqual(restarted.project_of("alpha"), "a")
            self.assertEqual(restarted.owner_of("alpha"), url)

            legacy_path = root / "legacy-commands.json"
            legacy_path.write_text(
                json.dumps({url: [asdict(CommandSpec("old", "Old"))]}),
                encoding="utf-8",
            )
            legacy = CommandStore(legacy_path)
            self.assertEqual(legacy.unowned_command_names(), ["old"])
            legacy.backfill_projects({url: "a"})
            self.assertEqual(CommandStore(legacy_path).project_of("old"), "a")


if __name__ == "__main__":
    unittest.main()
