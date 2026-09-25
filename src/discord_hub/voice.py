"""Voice connection management: one owned voice connection per guild.

The hub sends audio (raw PCM). Receive/streaming is Stage 2, blocked on
upstream DAVE-decryption support — see ADR 0005. This module enforces
ownership so two projects can never fight over the single connection.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass

import discord

logger = logging.getLogger(__name__)


class VoiceBusyError(RuntimeError):
    """The voice connection is owned by another project."""


class NotVoiceOwnerError(PermissionError):
    """The caller does not own the active voice connection."""


@dataclass
class VoiceSession:
    owner: str
    channel_id: int
    client: discord.VoiceClient


class VoiceManager:
    """Owns the (single) voice connection lifecycle."""

    def __init__(self, guild_id: int) -> None:
        self._guild_id = guild_id
        self._session: VoiceSession | None = None
        self._lock = asyncio.Lock()

    def status(self) -> dict:
        if self._session is None or not self._session.client.is_connected():
            return {"connected": False, "owner": None, "channel_id": None}
        return {
            "connected": True,
            "owner": self._session.owner,
            "channel_id": str(self._session.channel_id),
        }

    async def join(self, channel: discord.VoiceChannel, owner: str) -> dict:
        if channel.guild.id != self._guild_id:
            raise LookupError(f"Voice channel {channel.id} is outside the configured server")
        async with self._lock:
            if self._session is not None and self._session.client.is_connected():
                if self._session.owner != owner:
                    raise VoiceBusyError(
                        f"voice connection is owned by {self._session.owner}"
                    )
                if self._session.channel_id != channel.id:
                    await self._session.client.move_to(channel)
                    self._session.channel_id = channel.id
            else:
                client = await channel.connect()
                self._session = VoiceSession(owner=owner, channel_id=channel.id, client=client)
            logger.info("voice joined channel %s for %s", channel.id, owner)
            return self.status()

    def _require_owner(self, owner: str) -> VoiceSession:
        session = self._session
        if session is None or not session.client.is_connected():
            raise LookupError("No active voice connection")
        if session.owner != owner:
            raise NotVoiceOwnerError(f"voice connection is owned by {session.owner}")
        return session

    async def leave(self, owner: str) -> None:
        async with self._lock:
            session = self._require_owner(owner)
            await session.client.disconnect()
            self._session = None
            logger.info("voice left (owner %s)", owner)

    async def play_pcm(self, owner: str, pcm: bytes) -> None:
        session = self._require_owner(owner)
        if session.client.is_playing():
            session.client.stop()
        source = discord.PCMAudio(io.BytesIO(pcm))
        session.client.play(source)
        logger.info("playing %d PCM bytes for %s", len(pcm), owner)

    async def stop(self, owner: str) -> None:
        session = self._require_owner(owner)
        session.client.stop()

    async def force_disconnect(self) -> None:
        """Shutdown path: drop the connection regardless of ownership."""
        async with self._lock:
            if self._session is not None and self._session.client.is_connected():
                await self._session.client.disconnect(force=True)
            self._session = None
