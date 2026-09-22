"""The Discord connection itself — the only file that knows discord.py."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import discord

from .config import Settings
from .registry import ChannelTakenError, Registration, Registry

logger = logging.getLogger(__name__)


class HubBot(discord.Client):
    """A minimal Discord client. Features are added here as methods the
    API layer can call, keeping all Discord knowledge in this class."""

    def __init__(self, settings: Settings, registry: Registry) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.registry = registry
        self.ready_event: asyncio.Event  # assigned in main before startup
        self._http: aiohttp.ClientSession  # created in setup_hook

    async def setup_hook(self) -> None:
        self._http = aiohttp.ClientSession()

    async def close(self) -> None:
        await self._http.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s", self.user)
        self.ready_event.set()

    # -- inbound: Discord -> registered project --------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:  # ignore bots and our own webhook sends
            return
        thread_info = None
        reg = self.registry.get(message.channel.id)
        if reg is None and isinstance(message.channel, discord.Thread):
            # A thread inherits its parent channel's registration; the
            # payload tells the project which thread (session) this was.
            reg = self.registry.get(message.channel.parent_id)
            if reg is not None:
                thread_info = {
                    "id": str(message.channel.id),
                    "name": message.channel.name,
                    "parent_channel_id": str(message.channel.parent_id),
                }
        if reg is None:
            return
        payload = {
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "author": {"id": str(message.author.id), "name": message.author.display_name},
            "text": message.content,
            "created_at": message.created_at.isoformat(),
        }
        if thread_info is not None:
            payload["thread"] = thread_info
        try:
            async with self._http.post(reg.callback_url, json=payload) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "delivery to %s failed: HTTP %s", reg.callback_url, resp.status
                    )
        except aiohttp.ClientError:
            logger.exception("delivery to %s failed", reg.callback_url)

    # -- outbound: project -> Discord ------------------------------------

    async def send_message(self, channel_id: int, text: str) -> dict:
        """Send a text message to a channel or thread. If it (or its parent
        channel) is registered, send through the project's webhook so the
        message carries its name and avatar."""
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        thread = channel if isinstance(channel, discord.Thread) else None
        reg = self.registry.get(channel_id)
        if reg is None and thread is not None:
            reg = self.registry.get(thread.parent_id)

        if reg is not None and reg.webhook_url:
            webhook = discord.Webhook.from_url(reg.webhook_url, client=self)
            kwargs: dict = {"wait": True}
            if reg.display_name:
                kwargs["username"] = reg.display_name
            if reg.avatar_url:
                kwargs["avatar_url"] = reg.avatar_url
            if thread is not None:
                kwargs["thread"] = thread
            message = await webhook.send(text, **kwargs)
            return {"id": str(message.id), "channel_id": str(channel_id), "via": "webhook"}

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise LookupError(f"Channel {channel_id} is not a text channel or thread")
        message = await channel.send(text)
        return {"id": str(message.id), "channel_id": str(channel.id), "via": "bot"}

    async def create_thread(self, channel_id: int, name: str) -> dict:
        """Create a public thread in a text channel (e.g. one per session)."""
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise LookupError(f"Channel {channel_id} is not a text channel")
        thread = await channel.create_thread(name=name)
        return {
            "id": str(thread.id),
            "name": thread.name,
            "parent_channel_id": str(channel.id),
        }

    # -- registrations ----------------------------------------------------

    async def register(
        self,
        channel_id: int,
        callback_url: str,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> Registration:
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise LookupError(f"Channel {channel_id} is not a text channel")
        webhook = await channel.create_webhook(name=f"hub: {display_name or 'project'}")
        reg = Registration(
            channel_id=channel_id,
            callback_url=callback_url,
            display_name=display_name,
            avatar_url=avatar_url,
            webhook_url=webhook.url,
        )
        try:
            self.registry.add(reg)
        except ChannelTakenError:
            await webhook.delete(reason="registration failed: channel already taken")
            raise
        logger.info("registered channel %s -> %s", channel_id, callback_url)
        return reg

    async def deregister(self, channel_id: int) -> Registration | None:
        reg = self.registry.remove(channel_id)
        if reg is not None and reg.webhook_url:
            webhook = discord.Webhook.from_url(reg.webhook_url, client=self)
            try:
                await webhook.delete(reason="channel deregistered")
            except discord.HTTPException:
                logger.warning("could not delete webhook for channel %s", channel_id)
        return reg

    # -- caretaker views --------------------------------------------------

    def server_structure(self) -> dict:
        """A plain-dict snapshot of the guild's layout (no Discord types)."""
        guild = self.get_guild(self.settings.discord_guild_id)
        if guild is None:
            raise LookupError(f"Not a member of guild {self.settings.discord_guild_id}")

        categories = []
        for category, channels in guild.by_category():
            entries = []
            for ch in channels:
                if isinstance(ch, discord.TextChannel):
                    entries.append(
                        {"id": str(ch.id), "name": ch.name, "type": "text", "topic": ch.topic or ""}
                    )
                elif isinstance(ch, discord.ForumChannel):
                    entries.append(
                        {"id": str(ch.id), "name": ch.name, "type": "forum", "topic": ch.topic or ""}
                    )
                elif isinstance(ch, discord.VoiceChannel):
                    entries.append({"id": str(ch.id), "name": ch.name, "type": "voice"})
            categories.append(
                {
                    "name": category.name if category else "(no category)",
                    "channels": entries,
                }
            )
        return {
            "guild": guild.name,
            "categories": categories,
            "roles": [r.name for r in guild.roles if r.name != "@everyone"],
        }
