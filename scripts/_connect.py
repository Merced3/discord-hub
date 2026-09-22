"""Shared helper for scripts: connect the bot, wait until ready, yield it.

Usage:
    async with connected_bot() as bot:
        ...  # bot is connected and ready
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from discord_hub.bot import HubBot
from discord_hub.config import Settings


@asynccontextmanager
async def connected_bot() -> AsyncIterator[HubBot]:
    settings = Settings()  # type: ignore[call-arg]  # values come from .env
    bot = HubBot(settings)
    bot.ready_event = asyncio.Event()
    task = asyncio.create_task(bot.start(settings.discord_token))
    try:
        await asyncio.wait_for(bot.ready_event.wait(), timeout=30)
        yield bot
    finally:
        await bot.close()
        await asyncio.gather(task, return_exceptions=True)


def run(coro) -> None:
    # Windows consoles default to cp1252; server names may contain emoji.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        sys.exit(130)
