"""Entry point: runs the hub (Discord bot + local API) inside the
automation-harness, which owns the lifecycle — single-instance lock,
graceful shutdown, supervision with restart, structured logs, status.json.
"""

from __future__ import annotations

import asyncio
import logging
import time

import uvicorn
from automation_harness import Harness, HarnessConfig, ServiceContext

from .api import create_api
from .bot import HubBot
from .commands import CommandStore
from .config import Settings
from .registry import Registry

logger = logging.getLogger(__name__)


async def hub_service(ctx: ServiceContext) -> None:
    """The hub as a harness-supervised service: runs until the harness
    requests shutdown via ctx.stop_event."""
    settings = Settings()  # type: ignore[call-arg]  # values come from env
    registry = Registry(settings.data_dir / "registrations.json")
    command_store = CommandStore(settings.data_dir / "commands.json")
    bot = HubBot(settings, registry, command_store)
    bot.ready_event = asyncio.Event()

    api = create_api(bot, started_at=time.time())
    server = uvicorn.Server(
        uvicorn.Config(api, host=settings.hub_host, port=settings.hub_port, log_level="info")
    )

    async def shutdown_when_asked() -> None:
        await ctx.stop_event.wait()
        logger.info("shutdown requested by harness")
        server.should_exit = True
        await bot.close()

    logger.info("Starting hub API on %s:%s", settings.hub_host, settings.hub_port)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(bot.start(settings.discord_token))
        tg.create_task(server.serve())
        tg.create_task(shutdown_when_asked())


def main() -> None:
    harness = Harness(HarnessConfig(data_dir="data"), name="discord-hub")
    harness.add_service("hub", hub_service)
    harness.run()  # async runtime: harness owns the event loop


if __name__ == "__main__":
    main()
