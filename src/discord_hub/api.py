"""The local HTTP API — the only face other projects ever see."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

import discord
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .bot import HubBot
from .commands import CommandNameTakenError, CommandSpec
from .registry import ChannelTakenError
from .voice import NotVoiceOwnerError, StreamTakenError, VoiceBusyError


class SendMessageRequest(BaseModel):
    channel_id: int
    text: str
    reply_to_message_id: int | None = None


class CreateThreadRequest(BaseModel):
    channel_id: int
    name: str


class RegisterRequest(BaseModel):
    channel_id: int
    callback_url: str
    display_name: str | None = None
    avatar_url: str | None = None
    allow_thread_deletion: bool = False
    voice_events: bool = False
    reaction_events: bool = False


class CommandOption(BaseModel):
    name: str
    description: str = ""
    type: str = "string"  # integer | string | boolean | number
    required: bool = False
    min_value: float | None = None
    max_value: float | None = None


class CommandDecl(BaseModel):
    name: str
    description: str
    options: list[CommandOption] = []


class SetCommandsRequest(BaseModel):
    callback_url: str
    commands: list[CommandDecl]


class FollowupRequest(BaseModel):
    text: str
    ephemeral: bool = False


class VoiceJoinRequest(BaseModel):
    channel_id: int
    owner: str  # the project's callback URL, consistent with registrations


class VoiceOwnerRequest(BaseModel):
    owner: str


def create_api(bot: HubBot, started_at: float) -> FastAPI:
    app = FastAPI(title="discord-hub", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "discord_connected": bot.is_ready(),
            "uptime_seconds": int(time.time() - started_at),
            "interactions": bot.interaction_stats,
        }

    @app.post("/messages", status_code=201)
    async def send_message(body: SendMessageRequest) -> dict:
        try:
            return await bot.send_message(
                body.channel_id, body.text, body.reply_to_message_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail="Bot lacks permission in that channel") from exc

    @app.post("/threads", status_code=201)
    async def create_thread(body: CreateThreadRequest) -> dict:
        try:
            return await bot.create_thread(body.channel_id, body.name)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403, detail="Bot lacks Create Public Threads in that channel"
            ) from exc

    @app.delete("/threads/{thread_id}", status_code=204)
    async def delete_thread(thread_id: int) -> Response:
        try:
            await bot.delete_thread(thread_id)
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Thread not found or not a thread") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not complete thread deletion") from exc
        return Response(status_code=204)

    @app.get("/channels/{channel_id}/history")
    async def channel_history(
        channel_id: int, limit: int = Query(default=50, ge=1, le=100),
        before: int | None = Query(default=None, gt=0),
    ) -> dict:
        try:
            return await bot.channel_history(channel_id, limit, before)
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Text channel or thread not found") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not read channel history") from exc

    @app.post("/channels/{channel_id}/typing", status_code=202)
    async def trigger_typing(channel_id: int) -> Response:
        try:
            await bot.trigger_typing(channel_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=202)

    @app.get("/channels/{channel_id}/permissions")
    async def channel_permissions(channel_id: int) -> dict:
        try:
            return bot.channel_permissions(channel_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/commands")
    async def set_commands(body: SetCommandsRequest) -> dict:
        specs = [
            CommandSpec(
                name=c.name,
                description=c.description,
                options=[o.model_dump(exclude_none=True) for o in c.options],
            )
            for c in body.commands
        ]
        try:
            synced = await bot.set_commands(body.callback_url, specs)
        except CommandNameTakenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403,
                detail="Command sync failed; the bot's invite likely lacks the "
                "applications.commands scope",
            ) from exc
        return {"synced": synced}

    @app.post("/interactions/{interaction_id}/followups", status_code=201)
    async def interaction_followup(interaction_id: str, body: FollowupRequest) -> dict:
        try:
            return await bot.interaction_followup(interaction_id, body.text, body.ephemeral)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/registrations")
    async def list_registrations() -> list[dict]:
        return [asdict(r) for r in bot.registry.all()]

    @app.post("/registrations", status_code=201)
    async def register(body: RegisterRequest) -> dict:
        try:
            reg = await bot.register(
                body.channel_id, body.callback_url, body.display_name, body.avatar_url,
                body.allow_thread_deletion, body.voice_events, body.reaction_events,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ChannelTakenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403, detail="Bot lacks Manage Webhooks in that channel"
            ) from exc
        return asdict(reg)

    @app.delete("/registrations/{channel_id}", status_code=204)
    async def deregister(channel_id: int) -> Response:
        reg = await bot.deregister(channel_id)
        if reg is None:
            raise HTTPException(status_code=404, detail="channel not registered")
        return Response(status_code=204)

    # -- voice (ADR 0005) --------------------------------------------------

    def _voice_errors(exc: Exception) -> HTTPException:
        if isinstance(exc, (VoiceBusyError, NotVoiceOwnerError, StreamTakenError)):
            status = 403 if isinstance(exc, NotVoiceOwnerError) else 409
            return HTTPException(status_code=status, detail=str(exc))
        if isinstance(exc, LookupError):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=502, detail="Discord voice operation failed")

    @app.get("/voice/status")
    async def voice_status() -> dict:
        return bot.voice.status()

    @app.post("/voice/join")
    async def voice_join(body: VoiceJoinRequest) -> dict:
        try:
            return await bot.voice_join(body.channel_id, body.owner)
        except (VoiceBusyError, NotVoiceOwnerError, LookupError) as exc:
            raise _voice_errors(exc) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not connect voice") from exc

    @app.post("/voice/leave", status_code=204)
    async def voice_leave(body: VoiceOwnerRequest) -> Response:
        try:
            await bot.voice.leave(body.owner)
        except (VoiceBusyError, NotVoiceOwnerError, LookupError) as exc:
            raise _voice_errors(exc) from exc
        return Response(status_code=204)

    @app.post("/voice/stop", status_code=204)
    async def voice_stop(body: VoiceOwnerRequest) -> Response:
        try:
            await bot.voice.stop(body.owner)
        except (VoiceBusyError, NotVoiceOwnerError, LookupError) as exc:
            raise _voice_errors(exc) from exc
        return Response(status_code=204)

    @app.post("/voice/play", status_code=202)
    async def voice_play(request: Request) -> Response:
        """Body: raw PCM s16le 48kHz stereo. Owner comes from ?owner=..."""
        owner = request.query_params.get("owner")
        if not owner:
            raise HTTPException(status_code=422, detail="owner query parameter required")
        pcm = await request.body()
        if not pcm:
            raise HTTPException(status_code=422, detail="empty audio body")
        try:
            await bot.voice.play_pcm(owner, pcm)
        except (VoiceBusyError, NotVoiceOwnerError, LookupError, StreamTakenError) as exc:
            raise _voice_errors(exc) from exc
        return Response(status_code=202)

    @app.websocket("/voice/stream")
    async def voice_stream(websocket: WebSocket) -> None:
        """Owner-only duplex stream (ADR 0005, Stage 2).

        Hub -> owner: JSON messages, either
          {"type": "audio", "user_id": str, "pcm": base64 s16le 48kHz stereo,
           "timestamp": float}
          {"type": "speaking", "user_id": str, "state": "started"|"stopped"}
        Owner -> hub: binary messages of raw PCM s16le 48kHz stereo,
        played continuously (silence-filled when the buffer is empty).
        One stream per voice connection; a second attach is refused.
        """
        owner = websocket.query_params.get("owner")
        if not owner:
            await websocket.close(code=1008, reason="owner query parameter required")
            return
        try:
            stream = await bot.voice.open_stream(owner)
        except NotVoiceOwnerError:
            await websocket.close(code=1008, reason="voice connection owned by another project")
            return
        except StreamTakenError:
            await websocket.close(code=1008, reason="a stream is already attached")
            return
        except LookupError:
            await websocket.close(code=1008, reason="no active voice connection")
            return

        await websocket.accept()

        async def forward_events() -> None:
            while True:
                event = await stream.events.get()
                await websocket.send_json(event)

        async def receive_audio() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect
                data = message.get("bytes")
                if data:
                    stream.outbound.push(data)

        import asyncio

        forwarder = asyncio.create_task(forward_events())
        try:
            await receive_audio()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            forwarder.cancel()
            bot.voice.stream_closed(stream)

    @app.get("/server/structure")
    async def server_structure() -> dict:
        try:
            return bot.server_structure()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
