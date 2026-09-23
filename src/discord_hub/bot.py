"""The Discord connection itself — the only file that knows discord.py."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord

from .commands import CommandSpec, CommandStore, build_command
from .config import Settings
from .registry import ChannelTakenError, Registration, Registry

logger = logging.getLogger(__name__)

_MESSAGE_LIMIT = 1_900  # Discord's is 2_000; leave margin
_INTERACTION_TTL_S = 14 * 60  # interaction tokens live 15 minutes
_CALLBACK_BUDGET_S = 2.5  # Discord allows 3s for the initial response


def _chunks(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    content = text.strip() or "(No text response.)"
    return [content[i : i + limit] for i in range(0, len(content), limit)]


class HubBot(discord.Client):
    """A minimal Discord client. Features are added here as methods the
    API layer can call, keeping all Discord knowledge in this class."""

    def __init__(
        self, settings: Settings, registry: Registry, command_store: CommandStore
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.registry = registry
        self.command_store = command_store
        self.tree = discord.app_commands.CommandTree(self)
        self.ready_event: asyncio.Event  # assigned in main before startup
        self._http: aiohttp.ClientSession  # created in setup_hook
        self._interactions: dict[str, tuple[discord.Interaction, float]] = {}

    async def setup_hook(self) -> None:
        self._http = aiohttp.ClientSession()
        self.tree.on_error = self._on_command_error
        self._rebuild_commands()
        guild = discord.Object(id=self.settings.discord_guild_id)
        synced = await self.tree.sync(guild=guild)
        logger.info("Synchronized %d command(s) to the guild.", len(synced))

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
        payload: dict[str, Any] = {
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

    async def send_message(
        self, channel_id: int, text: str, reply_to_message_id: int | None = None
    ) -> dict:
        """Send a text message to a channel or thread, chunked to Discord's
        limit. If it (or its parent channel) is registered, send through the
        project's webhook so the message carries its name and avatar.
        A reply reference forces the plain bot path (webhooks can't reply).
        """
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise LookupError(f"Channel {channel_id} is not a text channel or thread")

        thread = channel if isinstance(channel, discord.Thread) else None
        reg = self.registry.get(channel_id)
        if reg is None and thread is not None:
            reg = self.registry.get(thread.parent_id)

        if reply_to_message_id is None and reg is not None and reg.webhook_url:
            webhook = discord.Webhook.from_url(reg.webhook_url, client=self)
            kwargs: dict[str, Any] = {"wait": True}
            if reg.display_name:
                kwargs["username"] = reg.display_name
            if reg.avatar_url:
                kwargs["avatar_url"] = reg.avatar_url
            if thread is not None:
                kwargs["thread"] = thread
            first_id = None
            for chunk in _chunks(text):
                message = await webhook.send(chunk, **kwargs)
                first_id = first_id or message.id
            return {"id": str(first_id), "channel_id": str(channel_id), "via": "webhook"}

        parts = _chunks(text)
        if reply_to_message_id is not None:
            reference = await channel.fetch_message(reply_to_message_id)
            first = await reference.reply(parts[0], mention_author=False)
        else:
            first = await channel.send(parts[0])
        for chunk in parts[1:]:
            await channel.send(chunk)
        return {"id": str(first.id), "channel_id": str(channel.id), "via": "bot"}

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

    async def trigger_typing(self, channel_id: int) -> None:
        """Show 'bot is typing...' in a channel for ~10 seconds."""
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        await self.http.send_typing(channel.id)

    def channel_permissions(self, channel_id: int) -> dict:
        """The bot's granted permissions in a channel (preflight helper)."""
        guild = self.get_guild(self.settings.discord_guild_id)
        channel = self.get_channel(channel_id)
        if guild is None or channel is None:
            raise LookupError(f"Unknown channel {channel_id}")
        perms = channel.permissions_for(guild.me)
        # Normalize discord.py's internal alias to the name Discord users
        # (and projects) actually know.
        granted = sorted(
            {"read_messages": "view_channel"}.get(name, name)
            for name, value in perms
            if value
        )
        return {"channel_id": str(channel_id), "permissions": granted}

    # -- project-owned slash commands (ADR 0003) --------------------------

    def _rebuild_commands(self) -> None:
        guild = discord.Object(id=self.settings.discord_guild_id)
        self.tree.clear_commands(guild=guild)
        for callback_url, specs in self.command_store.all().items():
            for spec in specs:
                self.tree.add_command(
                    build_command(spec, self._invoke_command), guild=guild
                )

    async def set_commands(self, callback_url: str, specs: list[CommandSpec]) -> int:
        self.command_store.set_project_commands(callback_url, specs)
        self._rebuild_commands()
        guild = discord.Object(id=self.settings.discord_guild_id)
        synced = await self.tree.sync(guild=guild)
        return len(synced)

    async def _invoke_command(
        self, spec: CommandSpec, interaction: discord.Interaction, options: dict[str, Any]
    ) -> None:
        """Deliver a command invocation to its owning project. The project's
        HTTP response IS the interaction response (ADR 0003)."""
        interaction_id = str(interaction.id)
        self._interactions[interaction_id] = (interaction, time.monotonic())
        callback_url = self.command_store.owner_of(spec.name)
        if callback_url is None:
            await interaction.response.send_message(
                "This command is not currently routed.", ephemeral=True
            )
            return
        event = {
            "type": "command",
            "interaction_id": interaction_id,
            "command": spec.name,
            "options": options,
            "channel_id": str(interaction.channel_id),
            "user": {"id": str(interaction.user.id), "name": interaction.user.display_name},
        }
        try:
            async with self._http.post(
                callback_url,
                json=event,
                timeout=aiohttp.ClientTimeout(total=_CALLBACK_BUDGET_S),
            ) as resp:
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError):
            logger.exception("command callback to %s failed; auto-deferring", callback_url)
            body = {"defer": True}

        try:
            if body.get("defer"):
                await interaction.response.defer(
                    ephemeral=bool(body.get("ephemeral", False)), thinking=True
                )
            else:
                await interaction.response.send_message(
                    body.get("text", "(no response)"),
                    ephemeral=bool(body.get("ephemeral", False)),
                )
        except discord.HTTPException:
            logger.exception("could not answer interaction %s", interaction_id)

    async def interaction_followup(
        self, interaction_id: str, text: str, ephemeral: bool = False
    ) -> dict:
        entry = self._interactions.get(interaction_id)
        if entry is None or time.monotonic() - entry[1] > _INTERACTION_TTL_S:
            self._interactions.pop(interaction_id, None)
            raise LookupError(f"Unknown or expired interaction {interaction_id}")
        interaction, _ = entry
        message = await interaction.followup.send(text, ephemeral=ephemeral)
        return {"id": str(message.id)}

    async def _on_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        logger.exception("Application command failed.", exc_info=error)
        message = "The command failed. See the hub log for details."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

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
