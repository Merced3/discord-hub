"""Entry point: runs the Discord bot and the local API in one process."""

from __future__ import annotations

import asyncio
import logging
import time

import uvicorn

from .api import create_api
from .bot import HubBot
from .config import Settings

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()  # type: ignore[call-arg]  # values come from env
    bot = HubBot(settings)
    bot.ready_event = asyncio.Event()

    api = create_api(bot, started_at=time.time())
    server = uvicorn.Server(
        uvicorn.Config(api, host=settings.hub_host, port=settings.hub_port, log_level="info")
    )

    logger.info("Starting hub API on %s:%s", settings.hub_host, settings.hub_port)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(bot.start(settings.discord_token))
            tg.create_task(server.serve())
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
