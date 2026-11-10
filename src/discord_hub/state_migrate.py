"""Read-only, idempotent JSON to Postgres copy with secret-safe verification.

Stop the old hub before the final copy: its stores cache records in memory.
The replacement hub must start only after this command completes.
"""

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


def _read_source(path: Path, empty: Any) -> tuple[Any, str]:
    if not path.exists():
        return empty, hashlib.sha256(b"").hexdigest()
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def migrate_state(data_dir: str | Path, database_url: str) -> dict[str, Any]:
    """Copy without overwriting rows; the first source is authoritative."""
    root = Path(data_dir)
    migrations.upgrade(database_url)
    result: dict[str, Any] = {}
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            reg_path = root / "registrations.json"
            raw_registrations, source_file_hash = _read_source(reg_path, [])
            source_regs = sorted(
                (asdict(Registration(**item)) for item in raw_registrations),
                key=lambda item: item["channel_id"],
            )
            inserted = 0
            for item in source_regs:
                cursor = conn.execute(
                    "INSERT INTO channel_registrations VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (item["channel_id"], Jsonb(item)),
                )
                inserted += cursor.rowcount
            source_ids = [item["channel_id"] for item in source_regs]
            dest_regs = [
                r[0]
                for r in conn.execute(
                    "SELECT registration FROM channel_registrations "
                    "WHERE channel_id = ANY(%s) ORDER BY channel_id",
                    (source_ids,),
                )
            ]
            source_checksum = _checksum(source_regs)
            destination_checksum = _checksum(dest_regs)
            result["registrations"] = {
                "source_count": len(source_regs),
                "source_sha256": source_file_hash,
                "source_checksum": source_checksum,
                "rows_inserted": inserted,
                "destination_count": conn.execute(
                    "SELECT count(*) FROM channel_registrations"
                ).fetchone()[0],
                "destination_checksum": destination_checksum,
                "source_matches_destination": source_checksum == destination_checksum,
            }
        with conn.transaction():
            command_path = root / "commands.json"
            raw_commands, source_file_hash = _read_source(command_path, {})
            inserted = 0
            for callback_url, specs in raw_commands.items():
                # PUT /commands replaces a whole set. Never merge an older set
                # for a callback already supplied by the authoritative source.
                exists = conn.execute(
                    "SELECT 1 FROM command_specs WHERE callback_url = %s LIMIT 1",
                    (callback_url,),
                ).fetchone()
                if exists:
                    continue
                for item in specs:
                    spec = CommandSpec(**item)
                    cursor = conn.execute(
                        "INSERT INTO command_specs (command_name, callback_url, spec) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (spec.name, callback_url, Jsonb(asdict(spec))),
                    )
                    inserted += cursor.rowcount
            source_commands = sorted(
                (
                    {"callback_url": url, "spec": asdict(CommandSpec(**item))}
                    for url, specs in raw_commands.items()
                    for item in specs
                ),
                key=lambda item: (item["callback_url"], item["spec"]["name"]),
            )
            source_urls = list(raw_commands)
            dest_commands = [
                r[0]
                for r in conn.execute(
                    "SELECT jsonb_build_object('callback_url', callback_url, 'spec', spec) "
                    "FROM command_specs WHERE callback_url = ANY(%s) "
                    "ORDER BY callback_url, command_name",
                    (source_urls,),
                )
            ]
            source_checksum = _checksum(source_commands)
            destination_checksum = _checksum(dest_commands)
            result["commands"] = {
                "source_count": len(source_commands),
                "source_projects": len(raw_commands),
                "source_sha256": source_file_hash,
                "source_checksum": source_checksum,
                "rows_inserted": inserted,
                "destination_count": conn.execute("SELECT count(*) FROM command_specs").fetchone()[
                    0
                ],
                "destination_checksum": destination_checksum,
                "source_matches_destination": source_checksum == destination_checksum,
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
