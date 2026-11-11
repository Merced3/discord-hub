"""Documented Self capture: append-only events and a privacy-filtered read model.

This module knows nothing about Discord. The bot converts Discord payloads to
these records; consumers see sanitized records through the authenticated API.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(slots=True)
class ArchiveEvent:
    event_type: str
    channel_id: int
    occurred_at: datetime
    scope_channel_id: int | None = None
    message_id: int | None = None
    actor_id: int | None = None
    emoji: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None


def _json(value: Any) -> Any:
    """Make psycopg/in-memory representations behave identically."""
    return json.loads(json.dumps(value, default=str))


class MemoryArchiveStore:
    """Deterministic local/test store. Production selects PostgresArchiveStore."""

    def __init__(self) -> None:
        self._messages: dict[int, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._dedupe: set[str] = set()
        self._checkpoints: dict[int, dict[str, Any]] = {}
        self._next_event = 1

    async def upsert_message(
        self,
        *,
        message_id: int,
        channel_id: int,
        parent_channel_id: int | None,
        author: dict[str, Any],
        content: str,
        created_at: datetime,
        edited_at: datetime | None,
        raw: dict[str, Any],
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        old = self._messages.get(message_id, {})
        self._messages[message_id] = {
            "id": str(message_id),
            "channel_id": str(channel_id),
            "parent_channel_id": str(parent_channel_id) if parent_channel_id else None,
            "author": _json(author),
            "text": content,
            "created_at": created_at,
            "edited_at": edited_at,
            "raw": _json(raw),
            "private": self._is_private(message_id),
            "deleted": False,
            "attachments": _json(attachments or old.get("attachments", [])),
        }

    def _is_private(self, message_id: int) -> bool:
        locks: dict[int, bool] = {}
        for past in self._events:
            if (
                past["message_id"] == str(message_id)
                and past["emoji"]
                and past["emoji"].replace("\ufe0f", "") == "🔒"
            ):
                locks[int(past["actor_id"])] = past["event_type"] == "reaction_added"
        return any(locks.values())

    async def record(self, event: ArchiveEvent) -> int | None:
        if event.dedupe_key and event.dedupe_key in self._dedupe:
            return None
        if event.dedupe_key:
            self._dedupe.add(event.dedupe_key)
        event_id = self._next_event
        self._next_event += 1
        self._events.append(
            {
                "id": event_id,
                "event_type": event.event_type,
                "channel_id": str(event.channel_id),
                "scope_channel_id": str(event.scope_channel_id or event.channel_id),
                "message_id": str(event.message_id) if event.message_id else None,
                "actor_id": str(event.actor_id) if event.actor_id else None,
                "emoji": event.emoji,
                "payload": _json(event.payload),
                "raw": _json(event.raw),
                "occurred_at": event.occurred_at,
            }
        )
        message = self._messages.get(event.message_id) if event.message_id else None
        if message is not None and event.emoji and event.emoji.replace("\ufe0f", "") == "🔒":
            message["private"] = self._is_private(event.message_id)
        if message is not None and event.event_type == "message_deleted":
            message["deleted"] = True
        return event_id

    async def messages(
        self, channel_ids: list[int], *, limit: int = 50, before: int | None = None
    ) -> dict[str, Any]:
        wanted = {str(c) for c in channel_ids}
        rows = [
            m
            for m in self._messages.values()
            if wanted.intersection({m["channel_id"], m["parent_channel_id"]})
            and not m["private"]
            and (before is None or int(m["id"]) < before)
        ]
        rows.sort(key=lambda m: int(m["id"]), reverse=True)
        items = [_public_message(m) for m in rows[:limit]]
        return {"items": items, "next_before": int(items[-1]["id"]) if len(rows) > limit else None}

    async def events(
        self,
        channel_ids: list[int],
        *,
        limit: int = 100,
        before: int | None = None,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        private_ids = {int(mid) for mid, m in self._messages.items() if m["private"]}
        wanted = {str(c) for c in channel_ids}
        rows = [
            e
            for e in self._events
            if e["scope_channel_id"] in wanted
            and (include_private or not e["message_id"] or int(e["message_id"]) not in private_ids)
            and (before is None or e["id"] < before)
        ]
        return [{k: _json(v) for k, v in e.items() if k != "raw"} for e in rows[-limit:]]

    async def attachment(self, attachment_id: int) -> dict[str, Any] | None:
        for message in self._messages.values():
            if message["private"]:
                continue
            for attachment in message["attachments"]:
                if attachment["id"] == str(attachment_id):
                    return {
                        **attachment,
                        "channel_id": message["parent_channel_id"] or message["channel_id"],
                    }
        return None

    async def checkpoint(self, channel_id: int) -> dict[str, Any] | None:
        return self._checkpoints.get(channel_id)

    async def save_checkpoint(self, channel_id: int, before: int | None, complete: bool) -> None:
        self._checkpoints[channel_id] = {
            "channel_id": channel_id,
            "next_before": before,
            "complete": complete,
        }


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    return {k: _json(v) for k, v in message.items() if k not in {"raw", "private"}}


class PostgresArchiveStore:
    """Async facade; short psycopg transactions run outside the event loop."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    async def upsert_message(self, **values: Any) -> None:
        def write() -> None:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO archive_messages
                    (message_id, channel_id, parent_channel_id, author, content,
                     created_at, edited_at, raw, attachments, deleted_at)
                    VALUES (%(message_id)s, %(channel_id)s, %(parent_channel_id)s,
                     %(author)s, %(content)s, %(created_at)s, %(edited_at)s, %(raw)s,
                     %(attachments)s, NULL)
                    ON CONFLICT (message_id) DO UPDATE SET channel_id=EXCLUDED.channel_id,
                     parent_channel_id=EXCLUDED.parent_channel_id, author=EXCLUDED.author,
                     content=EXCLUDED.content, created_at=EXCLUDED.created_at,
                     edited_at=EXCLUDED.edited_at, raw=EXCLUDED.raw,
                     attachments=EXCLUDED.attachments, deleted_at=NULL""",
                    {
                        **values,
                        "author": Jsonb(values["author"]),
                        "raw": Jsonb(values["raw"]),
                        "attachments": Jsonb(values.get("attachments") or []),
                    },
                )
                cur.execute(
                    """UPDATE archive_messages SET private=EXISTS (
                    SELECT 1 FROM archive_events added
                    WHERE added.message_id=%s AND added.event_type='reaction_added'
                    AND replace(added.emoji, U&'\\FE0F', '')='🔒'
                    AND NOT EXISTS (SELECT 1 FROM archive_events removed
                      WHERE removed.message_id=added.message_id
                      AND removed.actor_id=added.actor_id
                      AND replace(removed.emoji, U&'\\FE0F', '')='🔒'
                      AND removed.event_type='reaction_removed' AND removed.id>added.id))
                    WHERE message_id=%s""",
                    (values["message_id"], values["message_id"]),
                )

        await asyncio.to_thread(write)

    async def record(self, event: ArchiveEvent) -> int | None:
        def write() -> int | None:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO archive_events
                    (event_type, channel_id, scope_channel_id, message_id, actor_id,
                     emoji, payload, raw, occurred_at, dedupe_key)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                    RETURNING id""",
                    (
                        event.event_type,
                        event.channel_id,
                        event.scope_channel_id or event.channel_id,
                        event.message_id,
                        event.actor_id,
                        event.emoji,
                        Jsonb(event.payload),
                        Jsonb(event.raw),
                        event.occurred_at,
                        event.dedupe_key,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                if event.message_id is not None:
                    if event.emoji and event.emoji.replace("\ufe0f", "") == "🔒":
                        cur.execute(
                            """UPDATE archive_messages SET private=EXISTS (
                            SELECT 1 FROM archive_events added
                            WHERE added.message_id=%s AND added.event_type='reaction_added'
                            AND replace(added.emoji, U&'\\FE0F', '')='🔒'
                            AND NOT EXISTS (SELECT 1 FROM archive_events removed
                              WHERE removed.message_id=added.message_id
                              AND removed.actor_id=added.actor_id
                              AND replace(removed.emoji, U&'\\FE0F', '')='🔒'
                              AND removed.event_type='reaction_removed'
                              AND removed.id>added.id)) WHERE message_id=%s""",
                            (event.message_id, event.message_id),
                        )
                    if event.event_type == "message_deleted":
                        cur.execute(
                            "UPDATE archive_messages SET deleted_at=%s WHERE message_id=%s",
                            (event.occurred_at, event.message_id),
                        )
                return row["id"]

        return await asyncio.to_thread(write)

    async def messages(
        self, channel_ids: list[int], *, limit: int = 50, before: int | None = None
    ) -> dict[str, Any]:
        if not channel_ids:
            return {"items": [], "next_before": None}

        def read() -> dict[str, Any]:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT message_id AS id, channel_id, parent_channel_id, author,
                     content AS text, created_at, edited_at, deleted_at, attachments
                     FROM archive_messages
                     WHERE (channel_id = ANY(%s) OR parent_channel_id = ANY(%s)) AND NOT private
                     AND (%s::bigint IS NULL OR message_id < %s)
                     ORDER BY message_id DESC LIMIT %s""",
                    (channel_ids, channel_ids, before, before, limit + 1),
                )
                rows = cur.fetchall()
                items = [_string_ids(row) for row in rows[:limit]]
                return {
                    "items": items,
                    "next_before": int(items[-1]["id"]) if len(rows) > limit else None,
                }

        return await asyncio.to_thread(read)

    async def events(
        self,
        channel_ids: list[int],
        *,
        limit: int = 100,
        before: int | None = None,
        include_private: bool = False,
    ) -> list[dict[str, Any]]:
        if not channel_ids:
            return []

        def read() -> list[dict[str, Any]]:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT e.id, e.event_type, e.channel_id, e.message_id, e.actor_id,
                     e.emoji, e.payload, e.occurred_at FROM archive_events e
                     LEFT JOIN archive_messages m ON m.message_id=e.message_id
                     WHERE e.scope_channel_id = ANY(%s)
                     AND (%s OR e.message_id IS NULL OR m.message_id IS NULL OR NOT m.private)
                     AND (%s::bigint IS NULL OR e.id < %s)
                     ORDER BY e.id DESC LIMIT %s""",
                    (channel_ids, include_private, before, before, limit),
                )
                return [_string_ids(row) for row in reversed(cur.fetchall())]

        return await asyncio.to_thread(read)

    async def attachment(self, attachment_id: int) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT COALESCE(m.parent_channel_id, m.channel_id) AS channel_id,
                     a.value AS attachment FROM archive_messages m
                    CROSS JOIN LATERAL jsonb_array_elements(m.attachments) a(value)
                    WHERE a.value->>'id'=%s AND NOT m.private""",
                    (str(attachment_id),),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {**row["attachment"], "channel_id": str(row["channel_id"])}

        return await asyncio.to_thread(read)

    async def checkpoint(self, channel_id: int) -> dict[str, Any] | None:
        def read() -> dict[str, Any] | None:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT channel_id, next_before, complete, updated_at "
                    "FROM archive_backfills WHERE channel_id=%s",
                    (channel_id,),
                )
                return cur.fetchone()

        return await asyncio.to_thread(read)

    async def save_checkpoint(self, channel_id: int, before: int | None, complete: bool) -> None:
        def write() -> None:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO archive_backfills (channel_id,next_before,complete,updated_at)
                    VALUES (%s,%s,%s,NOW()) ON CONFLICT (channel_id) DO UPDATE SET
                    next_before=EXCLUDED.next_before,complete=EXCLUDED.complete,updated_at=NOW()""",
                    (channel_id, before, complete),
                )

        await asyncio.to_thread(write)


def _string_ids(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("id", "channel_id", "parent_channel_id", "message_id", "actor_id"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return result
