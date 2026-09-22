"""The local HTTP API — the only face other projects ever see."""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException

from .bot import HubBot


def create_api(bot: HubBot, started_at: float) -> FastAPI:
    app = FastAPI(title="discord-hub", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "discord_connected": bot.is_ready(),
            "uptime_seconds": int(time.time() - started_at),
        }

    @app.get("/server/structure")
    async def server_structure() -> dict:
        try:
            return bot.server_structure()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
