"""Voice connection management: one owned voice connection per guild.

The hub sends audio (raw PCM) and — since Stage 2 — receives it too,
via the ``discord-ext-voice-recv`` package with inbound DAVE decryption
(see ADR 0005). This module enforces ownership so two projects can never
fight over the single connection, and exposes one owner-only duplex
stream: per-speaker PCM frames and speaking events flow to the owner,
PCM frames flow back for playback.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import io
import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import voice_recv

from .voice_recv_compat import apply_voice_recv_patches

# Harden the pinned fork's receive path before any listener can be created
# (patches bind when PacketDecryptor/AudioReader are constructed).
apply_voice_recv_patches()

logger = logging.getLogger(__name__)

_PCM_FRAME_BYTES = 3840  # 20 ms of s16le 48 kHz stereo
_STREAM_QUEUE_MAX = 500  # ~10 s of audio; drop oldest on overload


class VoiceBusyError(RuntimeError):
    """The voice connection is owned by another project."""


class NotVoiceOwnerError(PermissionError):
    """The caller does not own the active voice connection."""


class StreamTakenError(RuntimeError):
    """Another stream is already attached to the voice connection."""


class StreamAudioSource(discord.AudioSource):
    """Continuous PCM playback fed by queueing frames from a websocket.

    ``read()`` is called from discord.py's voice-send thread every 20 ms;
    it returns silence when the owner has nothing buffered so playback
    never terminates mid-conversation.
    """

    def __init__(self) -> None:
        self._frames: collections.deque[bytes] = collections.deque()
        self._lock = __import__("threading").Lock()

    def push(self, pcm: bytes) -> None:
        with self._lock:
            for i in range(0, len(pcm) - _PCM_FRAME_BYTES + 1, _PCM_FRAME_BYTES):
                self._frames.append(pcm[i : i + _PCM_FRAME_BYTES])
            while len(self._frames) > _STREAM_QUEUE_MAX:
                self._frames.popleft()

    def read(self) -> bytes:
        with self._lock:
            if self._frames:
                return self._frames.popleft()
        return b"\x00" * _PCM_FRAME_BYTES

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._lock:
            self._frames.clear()


@dataclass
class VoiceSession:
    owner: str
    channel_id: int
    client: voice_recv.VoiceRecvClient
    stream: VoiceStream | None = None


class VoiceStream:
    """One owner-only duplex stream bound to the active voice connection.

    Received audio and speaking events are produced on voice-recv's router
    thread; they are handed to the asyncio loop via ``call_soon_threadsafe``
    and consumed by the websocket forwarder in the API layer.
    """

    def __init__(self, client: voice_recv.VoiceRecvClient, loop: asyncio.AbstractEventLoop) -> None:
        self._client = client
        self._loop = loop
        self.events: asyncio.Queue[dict] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX)
        self.outbound = StreamAudioSource()
        self._closed = False

    # -- receive side (router thread -> queue) ---------------------------

    def _enqueue(self, event: dict) -> None:
        def put() -> None:
            if self.events.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.events.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.events.put_nowait(event)

        self._loop.call_soon_threadsafe(put)

    def on_audio(
        self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData
    ) -> None:
        if data.pcm is None or user is None or user.bot:
            return
        self._enqueue(
            {
                "type": "audio",
                "user_id": str(user.id),
                "pcm": base64.b64encode(data.pcm).decode("ascii"),
                "timestamp": time.time(),
            }
        )

    def on_speaking_start(self, member: discord.Member) -> None:
        if member.bot:
            return
        self._enqueue({"type": "speaking", "user_id": str(member.id), "state": "started"})

    def on_speaking_stop(self, member: discord.Member) -> None:
        if member.bot:
            return
        self._enqueue({"type": "speaking", "user_id": str(member.id), "state": "stopped"})

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        stream = self

        class _StreamSink(voice_recv.AudioSink):
            def wants_opus(self) -> bool:
                return False

            def write(self, user, data: voice_recv.VoiceData) -> None:
                stream.on_audio(user, data)

            def cleanup(self) -> None:
                pass

            @voice_recv.AudioSink.listener("voice_member_speaking_start")
            def _speaking_start(self, member: discord.Member) -> None:
                stream.on_speaking_start(member)

            @voice_recv.AudioSink.listener("voice_member_speaking_stop")
            def _speaking_stop(self, member: discord.Member) -> None:
                stream.on_speaking_stop(member)

        self._sink = _StreamSink()
        self._client.listen(self._sink)
        self._client.play(self.outbound)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.stop_listening()
        except Exception:
            logger.warning("stop_listening failed during stream close", exc_info=True)
        with contextlib.suppress(Exception):
            self._client.stop()
        self.outbound.cleanup()


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
                    raise VoiceBusyError(f"voice connection is owned by {self._session.owner}")
                if self._session.channel_id != channel.id:
                    await self._session.client.move_to(channel)
                    self._session.channel_id = channel.id
            else:
                client = await channel.connect(cls=voice_recv.VoiceRecvClient)
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
            if session.stream is not None:
                session.stream.close()
                session.stream = None
            await session.client.disconnect()
            self._session = None
            logger.info("voice left (owner %s)", owner)

    async def play_pcm(self, owner: str, pcm: bytes) -> None:
        session = self._require_owner(owner)
        if session.stream is not None:
            raise StreamTakenError(
                "a duplex stream is attached; send audio through the stream instead"
            )
        if session.client.is_playing():
            session.client.stop()
        source = discord.PCMAudio(io.BytesIO(pcm))
        session.client.play(source)
        logger.info("playing %d PCM bytes for %s", len(pcm), owner)

    async def stop(self, owner: str) -> None:
        session = self._require_owner(owner)
        session.client.stop()

    # -- duplex stream (Stage 2, ADR 0005) ---------------------------------

    async def open_stream(self, owner: str) -> VoiceStream:
        """Attach the one allowed duplex stream to the active connection."""
        async with self._lock:
            session = self._require_owner(owner)
            if session.stream is not None:
                raise StreamTakenError("a stream is already attached to this voice connection")
            loop = asyncio.get_running_loop()
            stream = VoiceStream(session.client, loop)
            stream.start()
            session.stream = stream
            logger.info("voice stream opened for %s", owner)
            return stream

    async def close_stream(self, owner: str) -> None:
        async with self._lock:
            session = self._require_owner(owner)
            if session.stream is not None:
                session.stream.close()
                session.stream = None
                logger.info("voice stream closed for %s", owner)

    def stream_closed(self, session_stream: VoiceStream) -> None:
        """Called when the websocket drops: close the stream and free the slot."""
        session = self._session
        if session is not None and session.stream is session_stream:
            session_stream.close()
            session.stream = None

    async def force_disconnect(self) -> None:
        """Shutdown path: drop the connection regardless of ownership."""
        async with self._lock:
            if self._session is not None:
                if self._session.stream is not None:
                    self._session.stream.close()
                    self._session.stream = None
                if self._session.client.is_connected():
                    await self._session.client.disconnect(force=True)
            self._session = None
