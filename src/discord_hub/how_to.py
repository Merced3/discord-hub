"""Courier-owned #how-to rendering and reconciliation.

Only authenticated, redacted command declarations enter this module. It never
sees callback/webhook URLs and never invents project-specific usage guidance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import discord
import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

_MESSAGE_LIMIT = 1_900
_INTRO_KEY = "__intro__"
_INITIALIZED_KEY = "__initialized__"
_INTRO = """📖 HOW TO USE THIS SERVER'S AGENTS

This channel is generated automatically by Courier from each project's declared
slash commands. Reference only — don't chat here.

Each project appears below with its commands and options. The owning project
supplies the descriptions; any declaration change updates its entry."""


def _project_title(project_id: str) -> str:
    return project_id.replace("-", " ").replace("_", " ").title()


def _field(spec: Any, name: str, default: Any = None) -> Any:
    if isinstance(spec, dict):
        return spec.get(name, default)
    return getattr(spec, name, default)


def _number(value: Any) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def render_project(project_id: str, specs: list[Any]) -> list[str]:
    """Render one project into bounded messages without losing declarations."""
    title = _project_title(project_id)
    lines = [f"## {title}", f"`{project_id}`", ""]
    ordered = sorted(specs, key=lambda spec: _field(spec, "name", ""))
    for index, spec in enumerate(ordered):
        if index:
            lines.append("")
        lines.append(f"`/{_field(spec, 'name')}` — {_field(spec, 'description')}")
        for option in _field(spec, "options", []):
            qualifiers = [
                _field(option, "type", "string"),
                "required" if _field(option, "required", False) else "optional",
            ]
            minimum, maximum = _field(option, "min_value"), _field(option, "max_value")
            if minimum is not None or maximum is not None:
                lower = _number(minimum) if minimum is not None else "…"
                upper = _number(maximum) if maximum is not None else "…"
                qualifiers.append(f"{lower}..{upper}")
            if _field(option, "autocomplete", False):
                qualifiers.append("autocomplete")
            lines.append(
                f"• `{_field(option, 'name')}` ({', '.join(qualifiers)}): "
                f"{_field(option, 'description', '')}"
            )

    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= _MESSAGE_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Discord descriptions/options are individually bounded well below this
        # limit; the hard split is a defensive guarantee for persisted old data.
        while len(line) > _MESSAGE_LIMIT:
            chunks.append(line[:_MESSAGE_LIMIT])
            line = line[_MESSAGE_LIMIT:]
        current = line
    if current:
        chunks.append(current)

    # Continuation headers are accounted for after packing, then repacked if
    # their addition ever crosses the limit.
    if len(chunks) > 1:
        headed = [chunks[0]]
        for chunk in chunks[1:]:
            continuation = f"## {title} (continued)\n{chunk}"
            if len(continuation) > _MESSAGE_LIMIT:
                overflow = continuation[_MESSAGE_LIMIT:]
                continuation = continuation[:_MESSAGE_LIMIT]
                headed.append(continuation)
                chunk = overflow
            else:
                headed.append(continuation)
                chunk = ""
            if chunk:
                headed.append(chunk)
        chunks = headed
    return chunks


def _fingerprint(chunks: list[str]) -> str:
    serialized = json.dumps(chunks, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


class HowToStateStore:
    """Generated message IDs and content fingerprints, in JSON or Postgres."""

    def __init__(self, path: Path | None = None, *, database_url: str | None = None) -> None:
        if database_url is None and path is None:
            raise ValueError("a JSON path or database URL is required")
        self._path = path
        self._database_url = database_url
        if database_url:
            from . import migrations

            migrations.upgrade(database_url)

    def _connect(self):
        return psycopg.connect(self._database_url, connect_timeout=5)

    def all(self) -> dict[str, dict[str, Any]]:
        if self._database_url:
            with self._connect() as conn:
                conn.execute("SET statement_timeout = 5000")
                rows = conn.execute(
                    "SELECT state_key, fingerprint, message_ids FROM how_to_state"
                ).fetchall()
            return {
                key: {"fingerprint": fingerprint, "message_ids": list(message_ids)}
                for key, fingerprint, message_ids in rows
            }
        if self._path is None or not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def put(self, key: str, fingerprint: str, message_ids: list[str]) -> None:
        if self._database_url:
            with self._connect() as conn, conn.transaction():
                conn.execute("SET LOCAL statement_timeout = 5000")
                conn.execute(
                    "INSERT INTO how_to_state (state_key, fingerprint, message_ids) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (state_key) DO UPDATE SET fingerprint=EXCLUDED.fingerprint, "
                    "message_ids=EXCLUDED.message_ids",
                    (key, fingerprint, Jsonb(message_ids)),
                )
            return
        data = self.all()
        data[key] = {"fingerprint": fingerprint, "message_ids": message_ids}
        self._save(data)

    def delete(self, key: str) -> None:
        if self._database_url:
            with self._connect() as conn, conn.transaction():
                conn.execute("SET LOCAL statement_timeout = 5000")
                conn.execute("DELETE FROM how_to_state WHERE state_key = %s", (key,))
            return
        data = self.all()
        data.pop(key, None)
        self._save(data)

    def clear(self) -> None:
        if self._database_url:
            with self._connect() as conn, conn.transaction():
                conn.execute("SET LOCAL statement_timeout = 5000")
                conn.execute("DELETE FROM how_to_state")
            return
        self._save({})

    def _save(self, data: dict[str, Any]) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self._path)


class HowToManager:
    """Reconcile generated messages; state advances only after Discord success."""

    def __init__(self, channel: discord.TextChannel, store: HowToStateStore) -> None:
        self.channel = channel
        self.store = store
        self._lock = asyncio.Lock()

    async def _all_state(self) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self.store.all)

    async def _delete_message(self, message_id: str) -> None:
        try:
            message = await self.channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            pass

    async def _purge_legacy(self) -> None:
        """One-time, explicitly enabled cutover from hand-maintained content."""
        before = None
        while True:
            batch = []
            async for message in self.channel.history(
                limit=100, before=before, oldest_first=False
            ):
                batch.append(message)
            for message in batch:
                await message.delete()
            if len(batch) < 100:
                break
            before = discord.Object(id=batch[-1].id)
        await asyncio.to_thread(self.store.clear)

    async def _apply(self, key: str, chunks: list[str], entry: dict[str, Any]) -> None:
        fingerprint = _fingerprint(chunks)
        old_ids = list(entry.get("message_ids", []))
        if entry.get("fingerprint") == fingerprint and old_ids:
            try:
                for message_id in old_ids:
                    await self.channel.fetch_message(int(message_id))
                return
            except discord.NotFound:
                # A manually deleted generated message is rebuilt below.
                old_ids = []

        new_ids: list[str] = []
        for index, content in enumerate(chunks):
            if index < len(old_ids):
                message_id = int(old_ids[index])
                try:
                    message = await self.channel.fetch_message(message_id)
                    await message.edit(content=content)
                except discord.NotFound:
                    message = await self.channel.send(content)
                    message_id = message.id
            else:
                message = await self.channel.send(content)
                message_id = message.id
            new_ids.append(str(message_id))
        for message_id in old_ids[len(chunks) :]:
            await self._delete_message(message_id)
        await asyncio.to_thread(self.store.put, key, fingerprint, new_ids)

    async def reconcile(self, projects: list[dict[str, Any]]) -> None:
        """Make #how-to match a redacted operator command view exactly."""
        async with self._lock:
            state = await self._all_state()
            if _INITIALIZED_KEY not in state:
                await self._purge_legacy()
                state = {}

            desired: dict[str, list[str]] = {_INTRO_KEY: [_INTRO]}
            for project in sorted(projects, key=lambda item: item["project_id"]):
                specs = project.get("commands", [])
                if specs:
                    desired[project["project_id"]] = render_project(
                        project["project_id"], specs
                    )

            for key, chunks in desired.items():
                await self._apply(key, chunks, state.get(key, {}))

            for key, entry in state.items():
                if key in desired or key == _INITIALIZED_KEY:
                    continue
                for message_id in entry.get("message_ids", []):
                    await self._delete_message(message_id)
                await asyncio.to_thread(self.store.delete, key)

            await asyncio.to_thread(self.store.put, _INITIALIZED_KEY, "1", [])
