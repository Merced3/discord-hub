"""Postgres registration/command state checks (live URL required, never committed)."""

from __future__ import annotations

import os
import unittest
from dataclasses import asdict

import psycopg

from discord_hub.commands import CommandNameTakenError, CommandSpec, CommandStore
from discord_hub.registry import ChannelTakenError, Registration, Registry
from discord_hub.state_migrate import migrate_state


@unittest.skipUnless(os.environ.get("DISCORD_HUB_TEST_DATABASE_URL"), "Postgres URL not set")
class PostgresStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ["DISCORD_HUB_TEST_DATABASE_URL"]

    def setUp(self):
        # Isolated application tables in the designated test DB only.
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS command_specs, channel_registrations CASCADE")
            conn.execute("DROP TABLE IF EXISTS alembic_version")

    def registration(self, channel=10, project="a"):
        return Registration(
            channel_id=channel,
            callback_url=f"http://consumer.invalid/{project}",
            webhook_url=f"https://discord.invalid/{project}/secret",
            display_name=project,
            voice_events=True,
        )

    def test_registrations_survive_restart_and_reject_duplicate_ownership(self):
        store = Registry(database_url=self.url)
        store.add(self.registration())
        restarted = Registry(database_url=self.url)
        self.assertEqual(restarted.get(10), self.registration())
        with self.assertRaises(ChannelTakenError):
            restarted.add(self.registration(project="b"))
        self.assertEqual(restarted.get(10).display_name, "a")

    def test_failed_registration_does_not_change_memory_or_database(self):
        store = Registry(database_url=self.url)
        stale = Registry(database_url=self.url)
        store.add(self.registration())
        with self.assertRaises(ChannelTakenError):
            stale.add(self.registration(project="b"))
        self.assertEqual(len(stale.all()), 0)
        with psycopg.connect(self.url) as conn:
            count = conn.execute("SELECT count(*) FROM channel_registrations").fetchone()[0]
            self.assertEqual(count, 1)

    def test_removal_is_transactional_and_persistent(self):
        store = Registry(database_url=self.url)
        store.add(self.registration())
        self.assertEqual(store.remove(10), self.registration())
        self.assertIsNone(Registry(database_url=self.url).get(10))

    def test_command_collision_and_replacement_survive_restart(self):
        store = CommandStore(database_url=self.url)
        a, b = "http://consumer.invalid/a", "http://consumer.invalid/b"
        store.set_project_commands(a, [CommandSpec("alpha", "A")])
        with self.assertRaises(CommandNameTakenError):
            store.set_project_commands(b, [CommandSpec("alpha", "Other")])
        store.set_project_commands(b, [CommandSpec("beta", "B")])
        store.set_project_commands(a, [CommandSpec("gamma", "G")])
        restarted = CommandStore(database_url=self.url)
        self.assertIsNone(restarted.owner_of("alpha"))
        self.assertEqual(restarted.owner_of("gamma"), a)
        self.assertEqual(restarted.owner_of("beta"), b)

    def test_json_migration_is_idempotent_and_preserves_secrets(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registrations.json").write_text(json.dumps([asdict(self.registration())]))
            (root / "commands.json").write_text(
                json.dumps({"http://consumer.invalid/a": [asdict(CommandSpec("alpha", "A"))]})
            )
            first = migrate_state(root, self.url)
            second = migrate_state(root, self.url)
        self.assertEqual(first["registrations"]["source_count"], 1)
        self.assertEqual(first["commands"]["source_count"], 1)
        self.assertEqual(second["registrations"]["rows_inserted"], 0)
        self.assertEqual(second["commands"]["rows_inserted"], 0)
        self.assertEqual(
            first["registrations"]["destination_checksum"],
            second["registrations"]["destination_checksum"],
        )
        self.assertEqual(Registry(database_url=self.url).get(10), self.registration())
        self.assertEqual(
            CommandStore(database_url=self.url).all(),
            {"http://consumer.invalid/a": [CommandSpec("alpha", "A")]},
        )

    def test_database_command_conflict_rolls_back_replacement(self):
        first = CommandStore(database_url=self.url)
        stale = CommandStore(database_url=self.url)
        a, b = "http://consumer.invalid/a", "http://consumer.invalid/b"
        first.set_project_commands(a, [CommandSpec("alpha", "A")])
        first.set_project_commands(b, [CommandSpec("beta", "B")])
        with self.assertRaises(CommandNameTakenError):
            stale.set_project_commands(b, [CommandSpec("alpha", "Taken")])
        self.assertEqual(CommandStore(database_url=self.url).all()[b], [CommandSpec("beta", "B")])

    def test_missing_json_is_empty_and_later_commands_do_not_merge(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = migrate_state(root, self.url)
            self.assertEqual(missing["registrations"]["source_count"], 0)
            self.assertTrue(missing["commands"]["source_matches_destination"])
            url = "http://consumer.invalid/a"
            (root / "commands.json").write_text(
                json.dumps({url: [asdict(CommandSpec("new", "New"))]})
            )
            migrate_state(root, self.url)
            (root / "commands.json").write_text(
                json.dumps({url: [asdict(CommandSpec("old", "Old"))]})
            )
            later = migrate_state(root, self.url)
            self.assertFalse(later["commands"]["source_matches_destination"])
            self.assertEqual(
                CommandStore(database_url=self.url).all()[url], [CommandSpec("new", "New")]
            )


if __name__ == "__main__":
    unittest.main()
