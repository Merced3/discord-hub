"""Opt-in managed-Postgres archive checks; never uses the production URL implicitly."""

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg

from discord_hub.archive import ArchiveEvent, PostgresArchiveStore
from discord_hub.registry import Registry


@unittest.skipUnless(os.environ.get("DISCORD_HUB_TEST_DATABASE_URL"), "Postgres URL not set")
class PostgresArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ["DISCORD_HUB_TEST_DATABASE_URL"]
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        Registry(Path(self.temp.name) / "r.json", database_url=self.url)  # apply Alembic chain
        self.store = PostgresArchiveStore(self.url)
        self.message_id = 8_900_000_000_000_001
        self.channel_id = 8_900_000_000_000_000

        async def cleanup():
            def delete():
                with psycopg.connect(self.url) as conn:
                    conn.execute(
                        "DELETE FROM archive_events WHERE message_id=%s", (self.message_id,)
                    )
                    conn.execute(
                        "DELETE FROM archive_messages WHERE message_id=%s", (self.message_id,)
                    )
                    conn.execute(
                        "DELETE FROM archive_backfills WHERE channel_id=%s", (self.channel_id,)
                    )

            import asyncio

            await asyncio.to_thread(delete)

        self.addAsyncCleanup(cleanup)

    async def test_raw_json_idempotency_privacy_and_checkpoint(self):
        now = datetime.now(UTC)
        raw = {"id": str(self.message_id), "content": "A", "custom": [1, {"ok": True}]}
        await self.store.upsert_message(
            message_id=self.message_id,
            channel_id=self.channel_id,
            parent_channel_id=None,
            author={"id": "7", "name": "Human"},
            content="A",
            created_at=now,
            edited_at=None,
            raw=raw,
            attachments=[{"id": "9", "object_key": "key"}],
        )
        created = ArchiveEvent(
            event_type="message_created",
            channel_id=self.channel_id,
            message_id=self.message_id,
            occurred_at=now,
            raw=raw,
            dedupe_key=f"test:{self.message_id}",
        )
        self.assertIsNotNone(await self.store.record(created))
        self.assertIsNone(await self.store.record(created))
        await self.store.record(
            ArchiveEvent(
                event_type="reaction_added",
                channel_id=self.channel_id,
                message_id=self.message_id,
                actor_id=7,
                emoji="🔒",
                occurred_at=now,
            )
        )
        self.assertEqual((await self.store.messages([self.channel_id]))["items"], [])
        self.assertIsNone(await self.store.attachment(9))
        await self.store.record(
            ArchiveEvent(
                event_type="reaction_removed",
                channel_id=self.channel_id,
                message_id=self.message_id,
                actor_id=7,
                emoji="🔒",
                occurred_at=now,
            )
        )
        self.assertEqual(len((await self.store.messages([self.channel_id]))["items"]), 1)
        self.assertEqual((await self.store.attachment(9))["object_key"], "key")
        await self.store.save_checkpoint(self.channel_id, 123, False)
        self.assertEqual((await self.store.checkpoint(self.channel_id))["next_before"], 123)


if __name__ == "__main__":
    unittest.main()
