"""The local HTTP API — the only face other projects ever see."""

from __future__ import annotations

import time
from dataclasses import asdict

import discord
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from .bot import HubBot
from .registry import ChannelTakenError


class SendMessageRequest(BaseModel):
    channel_id: int
    text: str


class RegisterRequest(BaseModel):
    channel_id: int
    callback_url: str
    display_name: str | None = None
    avatar_url: str | None = None


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
            return await bot.send_message(body.channel_id, body.text)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except discord.Forbidden as exc:
            raise HTTPException(status_code=403, detail="Bot lacks permission in that channel") from exc

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
