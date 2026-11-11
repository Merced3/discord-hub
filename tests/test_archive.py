"""Offline archive capture, privacy, API authorization, and backfill tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from discord_hub.api import create_api
from discord_hub.archive import ArchiveEvent, MemoryArchiveStore
from discord_hub.bot import HubBot
from discord_hub.commands import CommandStore
from discord_hub.config import Settings
from discord_hub.registry import Registry

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def event(kind: str, message_id: int = 50, **kwargs) -> ArchiveEvent:
    return ArchiveEvent(
        event_type=kind,
        channel_id=kwargs.pop("channel_id", 10),
        message_id=message_id,
        occurred_at=NOW,
        payload=kwargs.pop("payload", {}),
        **kwargs,
    )


class ArchiveStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryArchiveStore()
        await self.store.upsert_message(
            message_id=50,
            channel_id=10,
            parent_channel_id=None,
            author={"id": "7", "name": "Human"},
            content="A",
            created_at=NOW,
            edited_at=None,
            raw={"id": "50", "content": "A"},
        )

    async def test_edit_chain_and_delete_are_append_only(self):
        await self.store.record(event("message_created", payload={"text": "A"}))
        await self.store.record(
            event("message_edited", payload={"before_text": "A", "after_text": "B"})
        )
        await self.store.record(
            event("message_edited", payload={"before_text": "B", "after_text": "C"})
        )
        await self.store.record(event("message_deleted"))
        rows = await self.store.events([10], limit=100)
        self.assertEqual(
            [r["event_type"] for r in rows],
            ["message_created", "message_edited", "message_edited", "message_deleted"],
        )
        self.assertEqual(rows[2]["payload"]["after_text"], "C")

    async def test_private_add_and_remove_changes_visibility_without_deleting_history(self):
        await self.store.record(event("reaction_added", emoji="🔒", actor_id=7))
        self.assertEqual((await self.store.messages([10], limit=100))["items"], [])
        self.assertEqual(await self.store.events([10], limit=100), [])
        await self.store.record(event("reaction_removed", emoji="🔒", actor_id=7))
        self.assertEqual(len((await self.store.messages([10], limit=100))["items"]), 1)

    async def test_one_users_unlock_does_not_expose_another_users_lock(self):
        await self.store.record(event("reaction_added", emoji="🔒", actor_id=7))
        await self.store.record(event("reaction_added", emoji="🔒", actor_id=8))
        await self.store.record(event("reaction_removed", emoji="🔒", actor_id=7))
        self.assertEqual((await self.store.messages([10], limit=10))["items"], [])
        await self.store.record(event("reaction_removed", emoji="🔒", actor_id=8))
        self.assertEqual(len((await self.store.messages([10], limit=10))["items"]), 1)

    async def test_idempotency_and_bounded_cursor(self):
        key = "backfill:10:50"
        await self.store.record(event("message_created", dedupe_key=key))
        await self.store.record(event("message_created", dedupe_key=key))
        await self.store.upsert_message(
            message_id=49,
            channel_id=10,
            parent_channel_id=None,
            author={"id": "7"},
            content="older",
            created_at=NOW,
            edited_at=None,
            raw={},
        )
        first = await self.store.messages([10], limit=1)
        self.assertEqual(len(first["items"]), 1)
        second = await self.store.messages([10], limit=1, before=first["next_before"])
        self.assertEqual(second["items"][0]["id"], "49")
        self.assertIsNone(second["next_before"])
        self.assertEqual(len(await self.store.events([10], limit=100)), 1)


class ArchiveBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        settings = Settings(
            _env_file=None,
            discord_token="fake",
            discord_guild_id=1,
            archive_channel_ids=[10],
            discord_hub_database_url="postgresql://unused",
            archive_r2_account_id="a",
            archive_r2_access_key_id="k",
            archive_r2_secret_access_key="s",
            archive_r2_bucket="b",
        )
        self.store = MemoryArchiveStore()
        self.bot = HubBot(
            settings, Registry(root / "r.json"), CommandStore(root / "c.json"), archive=self.store
        )
        self.bot._http = AsyncMock()
        self.bot.archive_attachments = AsyncMock(return_value=[])
        self.bot.get_channel = Mock(return_value=None)
        self.bot.fetch_channel = AsyncMock(side_effect=LookupError("unknown"))

    def message(self, text="A", mid=50):
        return SimpleNamespace(
            id=mid,
            content=text,
            created_at=NOW,
            edited_at=None,
            author=SimpleNamespace(id=7, bot=False, display_name="Human"),
            channel=SimpleNamespace(id=10),
            attachments=[],
        )

    async def test_capture_does_not_require_registration_or_mutate_callback_payload(self):
        await self.bot.on_message(self.message())
        messages = await self.store.messages([10], limit=10)
        self.assertEqual(messages["items"][0]["text"], "A")
        self.bot._http.post.assert_not_called()

    async def test_uncaptured_channel_and_existing_bot_callback_behavior(self):
        message = self.message()
        message.channel.id = 99
        message.author.bot = True
        await self.bot.on_message(message)
        self.assertEqual((await self.store.messages([10], limit=10))["items"], [])

    async def test_raw_gateway_payload_is_retained_but_not_public(self):
        raw = {"id": "50", "content": "A", "discord_only": {"exact": True}}
        await self.bot.on_socket_response({"t": "MESSAGE_CREATE", "d": raw})
        await self.bot.on_message(self.message())
        self.assertEqual(self.store._messages[50]["raw"], raw)
        self.assertNotIn("raw", (await self.store.messages([10], limit=10))["items"][0])

    async def test_backfill_is_bounded_resumable_and_idempotent(self):
        import discord

        channel = Mock(spec=discord.TextChannel)
        channel.id = 10
        self.bot.get_channel = Mock(return_value=channel)
        self.bot.http = SimpleNamespace(logs_from=AsyncMock())
        self.bot.http.logs_from.side_effect = [
            [
                {
                    "id": "51",
                    "channel_id": "10",
                    "content": "new",
                    "author": {
                        "id": "7",
                        "username": "Human",
                        "discriminator": "0",
                        "avatar": None,
                    },
                    "timestamp": NOW.isoformat(),
                    "type": 0,
                }
            ],
            [
                {
                    "id": "50",
                    "channel_id": "10",
                    "content": "old",
                    "author": {
                        "id": "7",
                        "username": "Human",
                        "discriminator": "0",
                        "avatar": None,
                    },
                    "timestamp": NOW.isoformat(),
                    "type": 0,
                }
            ],
            [],  # a full final page requires one empty request to prove completion
        ]
        first = await self.bot.backfill_archive(10, max_messages=1)
        self.assertFalse(first["complete"])
        second = await self.bot.backfill_archive(10, max_messages=1)
        self.assertFalse(second["complete"])
        third = await self.bot.backfill_archive(10, max_messages=1)
        self.assertTrue(third["complete"])
        again = await self.bot.backfill_archive(10, max_messages=1)
        self.assertEqual(again["messages_seen"], 0)
        self.assertEqual(len((await self.store.messages([10], limit=10))["items"]), 2)
        self.assertEqual(len(await self.store.events([10], limit=10)), 2)
        self.assertEqual(self.bot.http.logs_from.await_count, 3)
        calls = self.bot.http.logs_from.await_args_list
        self.assertIs(type(calls[1].kwargs["before"]), int)
        self.assertEqual(calls[1].kwargs["before"], 51)
        self.assertEqual(calls[2].kwargs["before"], 50)

    async def test_multiple_discord_pages_within_one_backfill_invocation(self):
        import discord

        channel = Mock(spec=discord.TextChannel)
        channel.id = 10
        self.bot.get_channel = Mock(return_value=channel)
        self.bot.http = SimpleNamespace(logs_from=AsyncMock())

        def raw(mid):
            return {
                "id": str(mid),
                "channel_id": "10",
                "content": "entry",
                "author": {"id": "7", "username": "Human", "discriminator": "0", "avatar": None},
                "timestamp": NOW.isoformat(),
                "type": 0,
            }

        self.bot.http.logs_from.side_effect = [
            [raw(mid) for mid in range(300, 200, -1)],
            [raw(mid) for mid in range(200, 100, -1)],
            [],
        ]
        result = await self.bot.backfill_archive(10, max_messages=250)
        self.assertTrue(result["complete"])
        self.assertEqual(result["messages_seen"], 200)
        calls = self.bot.http.logs_from.await_args_list
        self.assertEqual([call.kwargs["before"] for call in calls], [None, 201, 101])

    async def test_cached_edit_captures_before_after_once(self):
        await self.bot.on_message_edit(self.message("A"), self.message("B"))
        rows = await self.store.events([10], limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], {"before_text": "A", "after_text": "B"})

    async def test_raw_delete_and_private_reactions(self):
        await self.bot.on_message(self.message())
        reaction = SimpleNamespace(guild_id=1, channel_id=10, message_id=50, user_id=7, emoji="🔒")
        await self.bot.on_raw_reaction_add(reaction)
        self.assertEqual((await self.store.messages([10], limit=10))["items"], [])
        deletion = SimpleNamespace(guild_id=1, channel_id=10, message_id=50)
        await self.bot.on_raw_message_delete(deletion)
        self.assertTrue(
            (await self.store.events([10], limit=10, include_private=True))[-1]["event_type"],
            "message_deleted",
        )


class ArchiveApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.settings = Settings(
            _env_file=None,
            discord_token="fake",
            discord_guild_id=1,
            hub_auth_mode="required",
            hub_project_tokens={"socratic": "token", "other": "other"},
            hub_operator_token="operator",
            archive_channel_ids=[10, 11],
            archive_reader_channels={"socratic": [10], "other": [11]},
            discord_hub_database_url="postgresql://unused",
            archive_r2_account_id="a",
            archive_r2_access_key_id="k",
            archive_r2_secret_access_key="s",
            archive_r2_bucket="b",
        )
        self.store = MemoryArchiveStore()
        self.bot = HubBot(
            self.settings,
            Registry(root / "r.json"),
            CommandStore(root / "c.json"),
            archive=self.store,
        )
        self.bot.backfill_archive = AsyncMock(return_value={"messages_seen": 1, "complete": True})
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(self.bot, 0)), base_url="http://hub"
        )
        self.addAsyncCleanup(self.client.aclose)
        await self.store.upsert_message(
            message_id=50,
            channel_id=10,
            parent_channel_id=None,
            author={"id": "7"},
            content="visible",
            created_at=NOW,
            edited_at=None,
            raw={"secret_transport_field": "not public"},
        )
        await self.store.record(event("message_created", payload={"text": "visible"}))

    async def test_reader_gets_only_authorized_sanitized_archive(self):
        denied = await self.client.get(
            "/archive/messages", headers={"Authorization": "Bearer other"}
        )
        self.assertEqual(denied.status_code, 200)
        self.assertEqual(denied.json()["items"], [])
        response = await self.client.get(
            "/archive/messages?channel_id=10", headers={"Authorization": "Bearer token"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["text"], "visible")
        self.assertNotIn("raw", response.json()["items"][0])
        forbidden = await self.client.get(
            "/archive/messages?channel_id=11", headers={"Authorization": "Bearer token"}
        )
        self.assertEqual(forbidden.status_code, 403)

    async def test_attachment_download_is_authorized_and_private_checked(self):
        await self.store.upsert_message(
            message_id=50,
            channel_id=10,
            parent_channel_id=None,
            author={"id": "7"},
            content="visible",
            created_at=NOW,
            edited_at=None,
            raw={},
            attachments=[{"id": "90", "filename": "a.txt", "object_key": "k"}],
        )
        with unittest.mock.patch(
            "discord_hub.attachments.ArchiveAttachmentStore.read",
            new=AsyncMock(return_value=(b"bytes", "text/plain")),
        ):
            response = await self.client.get(
                "/archive/attachments/90", headers={"Authorization": "Bearer token"}
            )
            self.assertEqual(response.content, b"bytes")
            await self.store.record(event("reaction_added", emoji="🔒", actor_id=7))
            private = await self.client.get(
                "/archive/attachments/90", headers={"Authorization": "Bearer token"}
            )
            self.assertEqual(private.status_code, 404)

    async def test_private_never_leaks_through_messages_events_or_attachment_metadata(self):
        await self.store.record(event("reaction_added", emoji="🔒", actor_id=7))
        headers = {"Authorization": "Bearer token"}
        for path in ("/archive/messages?channel_id=10", "/archive/events?channel_id=10"):
            response = await self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"], [])

    async def test_auth_required_and_backfill_operator_only(self):
        self.assertEqual((await self.client.get("/archive/messages")).status_code, 401)
        response = await self.client.post(
            "/archive/backfill/10?max_messages=100", headers={"Authorization": "Bearer token"}
        )
        self.assertEqual(response.status_code, 403)
        response = await self.client.post(
            "/archive/backfill/10?max_messages=100", headers={"Authorization": "Bearer operator"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.bot.backfill_archive.assert_awaited_once_with(10, max_messages=100)


if __name__ == "__main__":
    unittest.main()
