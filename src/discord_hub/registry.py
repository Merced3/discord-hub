"""Channel registrations: which project owns which channel, and how to reach it.

A registration says "messages in channel X belong to project Y" — deliver
them to Y's callback URL, and send Y's outbound messages through a webhook
that carries Y's name and avatar (ADR 0002). Registrations persist to a
JSON file so they survive restarts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Registration:
    channel_id: int
    callback_url: str
    display_name: str | None = None
    avatar_url: str | None = None
    webhook_url: str | None = None
    # Declared intent: threads under this channel may be deleted on request.
    # Default False; consumers opt in and still confirm each deletion with
    # the owner (ADR 0004).
    allow_thread_deletion: bool = False
    # Declared intent: this project wants voice-state events (user joined /
    # left / moved voice channels, guild-wide) POSTed to its callback URL.
    # Default False; consumers opt in. Used for triggers like "start a
    # session when the owner joins a voice channel" — the policy stays in
    # the project, the hub only routes.
    voice_events: bool = False
    # Declared intent: this project wants reaction-marker events (a configured
    # emoji added to / removed from any message in the server) POSTed to its
    # callback URL. Default False; consumers opt in. The emoji -> marker
    # mapping is hub config; what a marker *means* is the project's policy.
    reaction_events: bool = False


class ChannelTakenError(RuntimeError):
    """Raised when registering a channel another project already owns."""


class Registry:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._by_channel: dict[int, Registration] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        for item in data:
            reg = Registration(**item)
            self._by_channel[reg.channel_id] = reg
        logger.info("loaded %d registration(s) from %s", len(self._by_channel), self._path)

    def _save(self) -> None:
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
            raise ChannelTakenError(
                f"channel {reg.channel_id} is already registered to {existing.callback_url}"
            )
        self._by_channel[reg.channel_id] = reg
        try:
            self._save()
        except Exception:
            del self._by_channel[reg.channel_id]
            raise

    def remove(self, channel_id: int) -> Registration | None:
        reg = self._by_channel.pop(channel_id, None)
        if reg is not None:
            try:
                self._save()
            except Exception:
                self._by_channel[channel_id] = reg
                raise
        return reg
