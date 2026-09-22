"""The Discord connection itself — the only file that knows discord.py."""

from __future__ import annotations

import asyncio
import logging

import discord

from .config import Settings

logger = logging.getLogger(__name__)


class HubBot(discord.Client):
    """A minimal Discord client. Features are added here as methods the
    API layer can call, keeping all Discord knowledge in this class."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.ready_event: asyncio.Event  # assigned in main before startup

    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s", self.user)
        self.ready_event.set()

    def server_structure(self) -> dict:
        """A plain-dict snapshot of the guild's layout (no Discord types)."""
        guild = self.get_guild(self.settings.discord_guild_id)
        if guild is None:
            raise LookupError(f"Not a member of guild {self.settings.discord_guild_id}")

        categories = []
        for category, channels in guild.by_category():
            categories.append(
                {
                    "name": category.name if category else "(no category)",
                    "channels": [
                        {
                            "id": str(ch.id),
                            "name": ch.name,
                            "type": "voice"
                            if isinstance(ch, discord.VoiceChannel)
                            else "text",
                        }
                        for ch in channels
                        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel))
                    ],
                }
            )
        return {
            "guild": guild.name,
            "categories": categories,
            "roles": [r.name for r in guild.roles if r.name != "@everyone"],
        }
