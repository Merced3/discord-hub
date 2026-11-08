"""Channel registrations in crash-safe local JSON or transactional Postgres."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)


@dataclass
class Registration:
    channel_id: int
    callback_url: str
    display_name: str | None = None
    avatar_url: str | None = None
    webhook_url: str | None = None
    allow_thread_deletion: bool = False
    voice_events: bool = False
    reaction_events: bool = False
    allow_forum_moderation: bool = False
    forum_events: bool = False


class ChannelTakenError(RuntimeError):
    """Raised when registering a channel another project already owns."""


class Registry:
    def __init__(self, path: Path | None = None, *, database_url: str | None = None) -> None:
        if database_url is None and path is None:
            raise ValueError("a JSON path or database URL is required")
        self._path = path
        self._database_url = database_url
        self._by_channel: dict[int, Registration] = {}
        if database_url:
            from . import migrations

            migrations.upgrade(database_url)
        self._load()

    def _load(self) -> None:
        if self._database_url:
            with psycopg.connect(self._database_url) as conn:
                rows = conn.execute("SELECT registration FROM channel_registrations").fetchall()
            for (item,) in rows:
                reg = Registration(**item)
                self._by_channel[reg.channel_id] = reg
        elif self._path and self._path.exists():
            for item in json.loads(self._path.read_text(encoding="utf-8")):
                reg = Registration(**item)
                self._by_channel[reg.channel_id] = reg
        logger.info("loaded %d registration(s)", len(self._by_channel))

    def _save(self) -> None:
        if self._database_url:
            raise RuntimeError("Postgres writes use per-operation transactions")
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(r) for r in self._by_channel.values()]
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(self._path)

    def get(self, channel_id: int) -> Registration | None:
        return self._by_channel.get(channel_id)

    def all(self) -> list[Registration]:
        return list(self._by_channel.values())

    def add(self, reg: Registration) -> None:
        existing = self._by_channel.get(reg.channel_id)
        if existing is not None:
            raise ChannelTakenError(f"channel {reg.channel_id} is already registered")
        if self._database_url:
            try:
                with psycopg.connect(self._database_url) as conn:
                    conn.execute(
                        "INSERT INTO channel_registrations VALUES (%s, %s)",
                        (reg.channel_id, Jsonb(asdict(reg))),
                    )
            except psycopg.errors.UniqueViolation as exc:
                raise ChannelTakenError(f"channel {reg.channel_id} is already registered") from exc
            self._by_channel[reg.channel_id] = reg
            return
        self._by_channel[reg.channel_id] = reg
        try:
            self._save()
        except Exception:
            del self._by_channel[reg.channel_id]
            raise

    def remove(self, channel_id: int) -> Registration | None:
        reg = self._by_channel.get(channel_id)
        if reg is None:
            return None
        if self._database_url:
            with psycopg.connect(self._database_url) as conn:
                conn.execute(
                    "DELETE FROM channel_registrations WHERE channel_id = %s", (channel_id,)
                )
        else:
            self._by_channel.pop(channel_id)
            try:
                self._save()
            except Exception:
                self._by_channel[channel_id] = reg
                raise
            return reg
        del self._by_channel[channel_id]
        return reg
