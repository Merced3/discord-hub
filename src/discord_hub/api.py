"""The local HTTP API — the only face other projects ever see."""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

import discord
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from .bot import HubBot
from .commands import CommandNameTakenError, CommandSpec
from .registry import ChannelTakenError


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


def create_api(bot: HubBot, started_at: float) -> FastAPI:
    app = FastAPI(title="discord-hub", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "discord_connected": bot.is_ready(),
            "uptime_seconds": int(time.time() - started_at),
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
                body.channel_id, body.callback_url, body.display_name, body.avatar_url
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

    @app.get("/server/structure")
    async def server_structure() -> dict:
        try:
            return bot.server_structure()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
