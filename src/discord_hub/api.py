"""The local HTTP API — the only face other projects ever see."""

from __future__ import annotations

import time

import discord
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .bot import HubBot


class SendMessageRequest(BaseModel):
    channel_id: int
    text: str


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

    @app.get("/server/structure")
    async def server_structure() -> dict:
        try:
            return bot.server_structure()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
