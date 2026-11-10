"""The local HTTP API — the only face other projects ever see."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict

import discord
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import LEGACY_PRINCIPAL, AuthenticationError, Principal
from .bot import HubBot
from .commands import CommandNameTakenError, CommandSpec
from .registry import ChannelTakenError
from .voice import NotVoiceOwnerError, StreamTakenError, VoiceBusyError

logger = logging.getLogger(__name__)


class SendMessageRequest(BaseModel):
    channel_id: int
    text: str
    reply_to_message_id: int | None = None


class EditMessageRequest(BaseModel):
    channel_id: int
    text: str


class CreateThreadRequest(BaseModel):
    channel_id: int
    name: str


class ArchiveThreadRequest(BaseModel):
    archived: bool = True
    locked: bool = False


class CreateForumPostRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1)
    tag_ids: list[int] = []


class RegisterRequest(BaseModel):
    channel_id: int
    callback_url: str
    project_id: str | None = None  # operator restores only; projects use their token
    display_name: str | None = None
    avatar_url: str | None = None
    allow_thread_deletion: bool = False
    voice_events: bool = False
    reaction_events: bool = False
    allow_forum_moderation: bool = False
    forum_events: bool = False


class UpdateForumPostRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    tag_ids: list[int] | None = None
    archived: bool | None = None
    locked: bool | None = None
    pinned: bool | None = None


class CreateForumTagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    moderated: bool = False


class UpdateForumTagRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=20)
    moderated: bool | None = None


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

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        try:
            request.state.principal = bot.auth.authenticate(
                request.headers.get("Authorization")
            )
        except AuthenticationError:
            return JSONResponse(status_code=401, content={"detail": "valid bearer token required"})
        return await call_next(request)

    def _principal(request: Request) -> Principal:
        return getattr(request.state, "principal", LEGACY_PRINCIPAL)

    def _project(request: Request) -> str | None:
        return _principal(request).project_id

    async def _authorize_channel(request: Request, channel_id: int) -> None:
        try:
            await bot.require_channel_access(channel_id, _project(request))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "discord_connected": bot.is_ready(),
            "uptime_seconds": int(time.time() - started_at),
            "interactions": bot.interaction_stats,
        }

    @app.post("/messages", status_code=201)
    async def send_message(body: SendMessageRequest, request: Request) -> dict:
        await _authorize_channel(request, body.channel_id)
        try:
            return await bot.send_message(body.channel_id, body.text, body.reply_to_message_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403, detail="Bot lacks permission in that channel"
            ) from exc

    @app.patch("/messages/{message_id}")
    async def edit_message(message_id: int, body: EditMessageRequest, request: Request) -> dict:
        """Edit a message the hub previously sent. The caller supplies the
        message id it stored from the send response; the hub keeps no
        message history."""
        await _authorize_channel(request, body.channel_id)
        try:
            return await bot.edit_message(body.channel_id, message_id, body.text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.NotFound as exc:
            raise HTTPException(
                status_code=404, detail="Message not found in that channel"
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403, detail="Bot lacks permission in that channel"
            ) from exc

    @app.post("/messages/attachments", status_code=201)
    async def send_attachment(request: Request) -> dict:
        """Attach one file to a message (ADR 0007). Body is the raw file
        bytes; channel_id and filename are query parameters; optional
        text becomes the message content. Oversize files are rejected
        before Discord is touched, and the rejection is logged."""
        channel_id = request.query_params.get("channel_id")
        filename = request.query_params.get("filename")
        text = request.query_params.get("text")
        if not channel_id or not filename:
            raise HTTPException(
                status_code=422, detail="channel_id and filename query parameters required"
            )
        try:
            parsed_channel_id = int(channel_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid channel_id") from exc
        data = await request.body()
        if not data:
            raise HTTPException(status_code=422, detail="empty file body")
        await _authorize_channel(request, parsed_channel_id)
        # Adaptive cap (ADR 0008): looked up live from the guild's boost
        # tier, so a boost raises the limit with no restart or code edit.
        cap = bot.tier_limit("attachment_bytes")
        if len(data) > cap:
            logger.warning(
                "attachment rejected: %r for channel %s is %d bytes, over the %d-byte "
                "cap (server boost tier %d)",
                filename,
                channel_id,
                len(data),
                cap,
                bot.server_tier(),
            )
            raise HTTPException(
                status_code=413,
                detail=(
                    f"file too big: {len(data)} bytes exceeds the {cap // (1024 * 1024)} MiB cap"
                ),
            )
        try:
            return await bot.send_attachment(int(channel_id), filename, data, text)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403, detail="Bot lacks permission in that channel"
            ) from exc

    @app.post("/threads", status_code=201)
    async def create_thread(body: CreateThreadRequest, request: Request) -> dict:
        await _authorize_channel(request, body.channel_id)
        try:
            return await bot.create_thread(body.channel_id, body.name)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(
                status_code=403, detail="Bot lacks Create Public Threads in that channel"
            ) from exc

    @app.delete("/threads/{thread_id}", status_code=204)
    async def delete_thread(thread_id: int, request: Request) -> Response:
        await _authorize_channel(request, thread_id)
        try:
            await bot.delete_thread(thread_id)
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Thread not found or not a thread") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not complete thread deletion"
            ) from exc
        return Response(status_code=204)

    @app.post("/threads/{thread_id}/archive")
    async def archive_thread(
        thread_id: int, request: Request, body: ArchiveThreadRequest | None = None
    ) -> dict:
        await _authorize_channel(request, thread_id)
        archived = True if body is None else body.archived
        locked = False if body is None else body.locked
        try:
            return await bot.archive_thread(thread_id, archived=archived, locked=locked)
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Thread not found or not a thread") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not complete thread archive"
            ) from exc

    @app.get("/forums/{channel_id}/posts")
    async def forum_posts(
        request: Request,
        channel_id: int,
        include_archived: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict:
        await _authorize_channel(request, channel_id)
        try:
            return await bot.forum_posts(channel_id, include_archived, limit)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not list forum posts"
            ) from exc

    @app.post("/forums/{channel_id}/posts", status_code=201)
    async def create_forum_post(
        channel_id: int, body: CreateForumPostRequest, request: Request
    ) -> dict:
        await _authorize_channel(request, channel_id)
        try:
            return await bot.create_forum_post(channel_id, body.title, body.text, body.tag_ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not create the post"
            ) from exc

    @app.patch("/forums/posts/{post_id}")
    async def update_forum_post(
        post_id: int, body: UpdateForumPostRequest, request: Request
    ) -> dict:
        await _authorize_channel(request, post_id)
        try:
            return await bot.update_forum_post(
                post_id, body.title, body.tag_ids, body.archived, body.locked, body.pinned
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Forum post not found") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not update the post"
            ) from exc

    @app.post("/forums/{channel_id}/tags", status_code=201)
    async def create_forum_tag(
        channel_id: int, body: CreateForumTagRequest, request: Request
    ) -> dict:
        await _authorize_channel(request, channel_id)
        try:
            return await bot.create_forum_tag(channel_id, body.name, body.moderated)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not create the tag") from exc

    @app.patch("/forums/{channel_id}/tags/{tag_id}")
    async def update_forum_tag(
        channel_id: int, tag_id: int, body: UpdateForumTagRequest, request: Request
    ) -> dict:
        await _authorize_channel(request, channel_id)
        try:
            return await bot.update_forum_tag(channel_id, tag_id, body.name, body.moderated)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not update the tag") from exc

    @app.delete("/forums/{channel_id}/tags/{tag_id}", status_code=204)
    async def delete_forum_tag(channel_id: int, tag_id: int, request: Request) -> Response:
        await _authorize_channel(request, channel_id)
        try:
            await bot.delete_forum_tag(channel_id, tag_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not delete the tag") from exc
        return Response(status_code=204)

    @app.get("/forums/posts/{post_id}")
    async def forum_post(post_id: int, request: Request) -> dict:
        await _authorize_channel(request, post_id)
        try:
            return await bot.forum_post(post_id)
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Forum post not found") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not read the forum post"
            ) from exc

    @app.get("/channels/{channel_id}/history")
    async def channel_history(
        request: Request,
        channel_id: int,
        limit: int = Query(default=50, ge=1, le=100),
        before: int | None = Query(default=None, gt=0),
    ) -> dict:
        await _authorize_channel(request, channel_id)
        try:
            return await bot.channel_history(channel_id, limit, before)
        except (LookupError, discord.NotFound) as exc:
            raise HTTPException(status_code=404, detail="Text channel or thread not found") from exc
        except (PermissionError, discord.Forbidden) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except discord.HTTPException as exc:
            raise HTTPException(
                status_code=502, detail="Discord could not read channel history"
            ) from exc

    @app.post("/channels/{channel_id}/typing", status_code=202)
    async def trigger_typing(channel_id: int, request: Request) -> Response:
        await _authorize_channel(request, channel_id)
        try:
            await bot.trigger_typing(channel_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=202)

    @app.get("/channels/{channel_id}/permissions")
    async def channel_permissions(channel_id: int, request: Request) -> dict:
        await _authorize_channel(request, channel_id)
        try:
            return bot.channel_permissions(channel_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/commands")
    async def set_commands(request: Request, body: SetCommandsRequest) -> dict:
        specs = [
            CommandSpec(
                name=c.name,
                description=c.description,
                options=[o.model_dump(exclude_none=True) for o in c.options],
            )
            for c in body.commands
        ]
        try:
            synced = await bot.set_commands(body.callback_url, specs, _project(request))
        except CommandNameTakenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
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
    async def interaction_followup(
        interaction_id: str, body: FollowupRequest, request: Request
    ) -> dict:
        try:
            return await bot.interaction_followup(
                interaction_id, body.text, body.ephemeral, _project(request)
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/registrations")
    async def list_registrations(request: Request) -> list[dict]:
        principal = _principal(request)
        registrations = bot.registry.all()
        if principal.project_id is not None:
            registrations = [
                reg for reg in registrations if reg.project_id == principal.project_id
            ]
        return [
            {key: value for key, value in asdict(reg).items() if key != "webhook_url"}
            for reg in registrations
        ]

    @app.post("/registrations", status_code=201)
    async def register(body: RegisterRequest, request: Request) -> dict:
        principal = _principal(request)
        project_id = principal.project_id
        if principal.operator:
            project_id = body.project_id
            if project_id is None:
                raise HTTPException(
                    status_code=422, detail="operator registration requires project_id"
                )
            if project_id not in bot.settings.hub_project_tokens:
                raise HTTPException(status_code=422, detail="unknown project_id")
        try:
            reg = await bot.register(
                body.channel_id,
                body.callback_url,
                body.display_name,
                body.avatar_url,
                body.allow_thread_deletion,
                body.voice_events,
                body.reaction_events,
                body.allow_forum_moderation,
                body.forum_events,
                project_id,
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
    async def deregister(channel_id: int, request: Request) -> Response:
        principal = _principal(request)
        reg = bot.registry.get(channel_id)
        if (
            principal.project_id is not None
            and reg is not None
            and reg.project_id != principal.project_id
        ):
            raise HTTPException(status_code=403, detail="channel registered to another project")
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
    async def voice_join(body: VoiceJoinRequest, request: Request) -> dict:
        try:
            return await bot.voice_join(body.channel_id, body.owner, _project(request))
        except (VoiceBusyError, NotVoiceOwnerError, LookupError) as exc:
            raise _voice_errors(exc) from exc
        except discord.HTTPException as exc:
            raise HTTPException(status_code=502, detail="Discord could not connect voice") from exc

    @app.post("/voice/leave", status_code=204)
    async def voice_leave(body: VoiceOwnerRequest, request: Request) -> Response:
        try:
            await bot.voice.leave(body.owner, _project(request))
        except (VoiceBusyError, NotVoiceOwnerError, LookupError) as exc:
            raise _voice_errors(exc) from exc
        return Response(status_code=204)

    @app.post("/voice/stop", status_code=204)
    async def voice_stop(body: VoiceOwnerRequest, request: Request) -> Response:
        try:
            await bot.voice.stop(body.owner, _project(request))
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
            await bot.voice.play_pcm(owner, pcm, _project(request))
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
        try:
            principal = bot.auth.authenticate(websocket.headers.get("Authorization"))
        except AuthenticationError:
            await websocket.close(code=1008, reason="valid bearer token required")
            return
        owner = websocket.query_params.get("owner")
        if not owner:
            await websocket.close(code=1008, reason="owner query parameter required")
            return
        try:
            stream = await bot.voice.open_stream(owner, principal.project_id)
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
