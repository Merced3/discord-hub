"""Read-only, idempotent JSON to Postgres copy with secret-safe verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import migrations
from .commands import CommandSpec
from .registry import Registration


def _checksum(records: list[dict[str, Any]]) -> str:
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def migrate_state(data_dir: str | Path, database_url: str) -> dict[str, Any]:
    """Copy records without overwriting an already migrated (authoritative) row."""
    root = Path(data_dir)
    migrations.upgrade(database_url)
    result: dict[str, Any] = {}
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            reg_path = root / "registrations.json"
            raw_registrations = json.loads(reg_path.read_text(encoding="utf-8"))
            inserted = 0
            for item in raw_registrations:
                reg = Registration(**item)
                cursor = conn.execute(
                    "INSERT INTO channel_registrations VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (reg.channel_id, Jsonb(asdict(reg))),
                )
                inserted += cursor.rowcount
            dest_regs = [
                r[0]
                for r in conn.execute(
                    "SELECT registration FROM channel_registrations ORDER BY channel_id"
                )
            ]
            result["registrations"] = {
                "source_count": len(raw_registrations),
                "source_sha256": hashlib.sha256(reg_path.read_bytes()).hexdigest(),
                "rows_inserted": inserted,
                "destination_count": len(dest_regs),
                "destination_checksum": _checksum(dest_regs),
            }
        with conn.transaction():
            command_path = root / "commands.json"
            raw_commands = json.loads(command_path.read_text(encoding="utf-8"))
            inserted = 0
            for callback_url, specs in raw_commands.items():
                for item in specs:
                    spec = CommandSpec(**item)
                    cursor = conn.execute(
                        "INSERT INTO command_specs VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (spec.name, callback_url, Jsonb(asdict(spec))),
                    )
                    inserted += cursor.rowcount
            dest_commands = [
                r[0]
                for r in conn.execute(
                    "SELECT jsonb_build_object('callback_url', callback_url, 'spec', spec) "
                    "FROM command_specs ORDER BY callback_url, command_name"
                )
            ]
            result["commands"] = {
                "source_count": sum(len(specs) for specs in raw_commands.values()),
                "source_projects": len(raw_commands),
                "source_sha256": hashlib.sha256(command_path.read_bytes()).hexdigest(),
                "rows_inserted": inserted,
                "destination_count": len(dest_commands),
                "destination_checksum": _checksum(dest_commands),
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("database_url")
    args = parser.parse_args()
    print(json.dumps(migrate_state(args.data_dir, args.database_url), indent=2))


if __name__ == "__main__":
    main()
