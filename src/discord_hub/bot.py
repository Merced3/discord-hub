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
from .voice import VoiceManager

logger = logging.getLogger(__name__)

_MESSAGE_LIMIT = 1_900  # Discord's is 2_000; leave margin
_INTERACTION_TTL_S = 14 * 60  # interaction tokens live 15 minutes
_CALLBACK_BUDGET_S = 2.5  # Discord allows 3s for the initial response
_MESSAGE_CALLBACK_BUDGET_S = 5.0  # acknowledge promptly; do work in the consumer


class _MdState:
    """Markdown state at a given position in the original text."""

    __slots__ = ("in_fence", "fence_lang", "inline_stack")

    def __init__(self) -> None:
        self.in_fence = False
        self.fence_lang = ""
        self.inline_stack: list[str] = []  # open inline markers, in order


def _scan_state(text: str) -> _MdState:
    """Walk text and return the markdown state at the end of it.

    Handles backslash escapes. Fences are line-oriented (``` toggles),
    everything else is a simple toggle stack (** before *, so bold wins).
    """
    state = _MdState()
    i, n = 0, len(text)
    at_line_start = True
    while i < n:
        ch = text[i]
        if ch == "\\":  # escaped char: skip both
            i += 2
            at_line_start = False
            continue
        if at_line_start and text.startswith("```", i):
            if not state.in_fence:
                eol = text.find("\n", i)
                eol = n if eol == -1 else eol
                state.in_fence = True
                state.fence_lang = text[i + 3 : eol].strip()
                state.inline_stack.clear()  # nothing inline inside a fence
                i = eol
                continue
            state.in_fence = False
            state.fence_lang = ""
            i += 3
            at_line_start = False
            continue
        if ch == "\n":
            at_line_start = True
            i += 1
            continue
        at_line_start = False
        if state.in_fence:
            i += 1
            continue
        for marker in ("**", "~~", "||", "*", "`", "_"):
            if text.startswith(marker, i):
                if state.inline_stack and state.inline_stack[-1] == marker:
                    state.inline_stack.pop()
                else:
                    state.inline_stack.append(marker)
                i += len(marker)
                break
        else:
            i += 1
    return state


def _wrappers(state: _MdState) -> tuple[str, str]:
    """(suffix to close a chunk, prefix to reopen the next) for a state."""
    suffix = "".join(reversed(state.inline_stack))
    prefix = "".join(state.inline_stack)
    if state.in_fence:
        suffix += "\n```"
        prefix = "```" + state.fence_lang + "\n" + prefix
    return suffix, prefix


def _find_break(window: str, min_len: int = 1) -> int:
    """Best split index within window. 0 means 'no good break found'.

    Breaks yielding a piece shorter than min_len are rejected, so we don't
    emit tiny chunks (e.g. a lone ``` fence opener) when a later separator
    or a hard cut would fill the message better.
    """
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep, 1)  # don't break at 0
        keep = len(sep) if sep != " " else 1  # separator stays with prev chunk
        if idx > 0 and idx + keep >= min_len:
            return idx + keep
    return 0


def _chunks(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """Split text into Discord-safe messages, losslessly.

    Break preference: paragraph > line > space > hard cut, so words and list
    items are never severed when avoidable. Code fences and inline markdown
    (**, *, `, ~~, ||, _) that span a break are closed at the end of one
    chunk and reopened at the start of the next. Concatenating the chunks
    minus those synthetic wrappers reproduces the input exactly.
    """
    return [chunk for chunk, _, _ in _chunks_with_meta(text, limit)]


def _chunks_with_meta(
    text: str, limit: int = _MESSAGE_LIMIT
) -> list[tuple[str, str, str]]:
    """Like _chunks, but each item is (chunk, added_prefix, added_suffix).

    The prefix/suffix are the synthetic reopeners/closers, so tests can
    verify losslessness exactly: strip them, join, compare to the input.
    """
    content = text.strip() or "(No text response.)"
    out: list[tuple[str, str, str]] = []
    pos = 0
    pending_prefix = ""
    while pos < len(content):
        remaining = content[pos:]
        take = min(max(limit - len(pending_prefix), 1), len(remaining))

        while True:
            window = remaining[:take]
            min_len = max(len(window) // 4, 1)
            piece = window
            if len(remaining) > take:
                brk = _find_break(window, min_len)
                if brk > 0:
                    piece = remaining[:brk]

            state_start = _scan_state(content[:pos])
            state_end = _scan_state(content[: pos + len(piece)])

            # Prefer not to open a fence at the very end of a chunk — but only
            # when breaking before the fence still gives a decent-sized piece.
            # Otherwise the close/reopen wrappers handle it fine.
            if state_end.in_fence and not state_start.in_fence:
                cut = piece.rfind("\n```")
                if cut >= min_len:
                    piece = piece[:cut]
                    state_end = _scan_state(content[: pos + len(piece)])

            if not piece:  # safety net: never emit an empty message
                piece = remaining[:1]
                state_end = _scan_state(content[: pos + len(piece)])

            suffix, next_prefix = _wrappers(state_end)

            if len(pending_prefix) + len(piece) + len(suffix) <= limit:
                break  # fits — done
            if len(piece) <= 1:
                # pathological limit (wrappers alone exceed it): emit raw
                # text with no wrappers at all, but always PROGRESS
                piece = remaining[:1]
                suffix = next_prefix = ""
                pending_prefix = ""
                break
            # shrink and re-evaluate (break choice + state may both change);
            # clamp to >= 1: a negative slice would wrap and never converge
            take = max(
                len(piece) - (len(pending_prefix) + len(piece) + len(suffix) - limit), 1
            )

        out.append((pending_prefix + piece + suffix, pending_prefix, suffix))
        pending_prefix = next_prefix
        pos += len(piece)  # piece is always non-empty — no infinite loop
    return out


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
        intents.voice_states = True  # required for voice handshake (session id)
        intents.reactions = True  # reaction markers (raw add/remove events)
        super().__init__(intents=intents)
        self.settings = settings
        self.registry = registry
        self.command_store = command_store
        self.tree = discord.app_commands.CommandTree(self)
        self.ready_event: asyncio.Event  # assigned in main before startup
        self._http: aiohttp.ClientSession  # created in setup_hook
        self._interactions: dict[str, tuple[discord.Interaction, float]] = {}
        self.interaction_stats: dict[str, int] = {
            "received": 0,
            "delivered": 0,
            "callback_failed": 0,
            "unrouted": 0,
        }
        self._commands_lock = asyncio.Lock()
        self._registrations_lock = asyncio.Lock()
        self.voice = VoiceManager(settings.discord_guild_id)

    async def setup_hook(self) -> None:
        self._http = aiohttp.ClientSession()
        self.tree.on_error = self._on_command_error
        self._rebuild_commands()
        guild = discord.Object(id=self.settings.discord_guild_id)
        synced = await self.tree.sync(guild=guild)
        logger.info("Synchronized %d command(s) to the guild.", len(synced))

    async def close(self) -> None:
        await self.voice.force_disconnect()
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
            async with self._http.post(
                reg.callback_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=_MESSAGE_CALLBACK_BUDGET_S),
                allow_redirects=False,
            ) as resp:
                if not 200 <= resp.status < 300:
                    logger.warning(
                        "delivery to %s failed: HTTP %s", reg.callback_url, resp.status
                    )
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("message callback failed for channel %s; not retried", message.channel.id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Route voice join/leave/move events to registered projects that
        opted in (`voice_events` on their registration). The hub only
        routes; what a join *means* (start a session, ignore it, page the
        owner) is the project's policy. Bot users are not reported, same
        rule as message callbacks."""
        if member.bot:
            return
        before_id = before.channel.id if before.channel else None
        after_id = after.channel.id if after.channel else None
        if before_id == after_id:
            return  # mute/deafen/video toggles are not join/leave/move
        payload: dict[str, Any] = {
            "type": "voice_state",
            "user": {"id": str(member.id), "name": member.display_name},
            "before_channel_id": str(before_id) if before_id else None,
            "after_channel_id": str(after_id) if after_id else None,
        }
        # Deliver once per project even if it registered several channels.
        callback_urls = {
            reg.callback_url for reg in self.registry.all() if reg.voice_events
        }
        for callback_url in callback_urls:
            try:
                async with self._http.post(
                    callback_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=_MESSAGE_CALLBACK_BUDGET_S),
                    allow_redirects=False,
                ) as resp:
                    if not 200 <= resp.status < 300:
                        logger.warning(
                            "voice-state delivery to %s failed: HTTP %s",
                            callback_url, resp.status,
                        )
            except (aiohttp.ClientError, TimeoutError):
                logger.warning("voice-state callback failed for %s; not retried", callback_url)

    async def on_raw_reaction_add(self, event: discord.RawReactionActionEvent) -> None:
        await self._route_reaction_marker(event, "added")

    async def on_raw_reaction_remove(self, event: discord.RawReactionActionEvent) -> None:
        await self._route_reaction_marker(event, "removed")

    async def _route_reaction_marker(
        self, event: discord.RawReactionActionEvent, action: str
    ) -> None:
        """Route configured-emoji reactions to projects that opted in
        (`reaction_events` on their registration). Raw events fire for any
        message in the server regardless of cache age, so marks work on
        years-old messages. The emoji -> marker mapping is hub config; what
        a marker means and whose reaction counts is the project's policy."""
        # Normalize U+FE0F variation selectors out of both sides: Discord
        # sends some emojis with it (\u2764\ufe0f) and config authors won't.
        emoji = str(event.emoji).replace("\ufe0f", "")
        marker = next(
            (name for key, name in self.settings.reaction_markers.items()
             if key.replace("\ufe0f", "") == emoji),
            None,
        )
        if marker is None:
            logger.debug("reaction %s %r: not a configured marker", action, str(event.emoji))
            return
        if event.guild_id != self.settings.discord_guild_id:
            return
        if self.user is not None and event.user_id == self.user.id:
            return  # the hub's own reactions are not marks
        payload: dict[str, Any] = {
            "type": "reaction_marker",
            "action": action,
            "emoji": str(event.emoji),
            "marker": marker,
            "message_id": str(event.message_id),
            "channel_id": str(event.channel_id),
            "user_id": str(event.user_id),
        }
        # Deliver once per project even if it registered several channels.
        callback_urls = {
            reg.callback_url for reg in self.registry.all() if reg.reaction_events
        }
        logger.info("reaction marker %s %r (%s) -> %d project(s)", action, str(event.emoji), marker, len(callback_urls))
        for callback_url in callback_urls:
            try:
                async with self._http.post(
                    callback_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=_MESSAGE_CALLBACK_BUDGET_S),
                    allow_redirects=False,
                ) as resp:
                    if not 200 <= resp.status < 300:
                        logger.warning(
                            "reaction-marker delivery to %s failed: HTTP %s",
                            callback_url, resp.status,
                        )
            except (aiohttp.ClientError, TimeoutError):
                logger.warning("reaction-marker callback failed for %s; not retried", callback_url)

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
        thread = await channel.create_thread(
            name=name, type=discord.ChannelType.public_thread
        )
        return {
            "id": str(thread.id),
            "name": thread.name,
            "parent_channel_id": str(channel.id),
        }

    async def delete_thread(self, thread_id: int) -> None:
        """Delete a thread whose parent channel's registration opted in to
        thread deletion (ADR 0004). Never deletes a parent channel."""
        channel = self.get_channel(thread_id) or await self.fetch_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            raise LookupError(f"Channel {thread_id} is not a thread")
        if channel.guild.id != self.settings.discord_guild_id:
            raise PermissionError("Thread is outside the configured server")
        reg = self.registry.get(channel.parent_id)
        if reg is None or not reg.allow_thread_deletion:
            raise PermissionError(
                "Thread deletion is not enabled for this channel's registration"
            )
        # Discord enforces Manage Threads; do not edit permissions automatically.
        await channel.delete(reason="Hub cleanup request in operator-approved parent channel")
        logger.info("deleted thread %s under parent %s", thread_id, channel.parent_id)

    async def channel_history(
        self, channel_id: int, limit: int = 50, before: int | None = None
    ) -> dict:
        """One newest-first page, not an archive or a recursive thread export."""
        if not 1 <= limit <= 100 or (before is not None and before <= 0):
            raise ValueError("limit must be 1..100; before must be a positive message ID")
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise LookupError(f"Channel {channel_id} is not a text channel or thread")
        if channel.guild.id != self.settings.discord_guild_id:
            raise PermissionError("Channel is outside the configured server")
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            raise PermissionError("History requires View Channel and Read Message History")
        messages = []
        async for message in channel.history(
            limit=limit, before=discord.Object(id=before) if before else None,
            oldest_first=False,
        ):
            messages.append({
                "id": str(message.id), "channel_id": str(channel_id),
                "author": {"id": str(message.author.id), "name": message.author.display_name,
                           "bot": message.author.bot},
                "created_at": message.created_at.isoformat(),
                "edited_at": message.edited_at.isoformat() if message.edited_at else None,
                "text": message.content,
                "attachments": [{
                    "id": str(a.id), "filename": a.filename, "size": a.size,
                    "content_type": a.content_type, "url": a.url,
                } for a in message.attachments],
            })
        return {
            "channel_id": str(channel_id), "messages": messages,
            # A full final page may yield an empty next page; no extra fetch just
            # to discover whether an older message exists.
            "next_before": messages[-1]["id"] if len(messages) == limit else None,
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
        # Guild command sync replaces the entire guild set. Serialize updates
        # and keep every other project's definitions in the candidate tree.
        async with self._commands_lock:
            self.command_store.validate_project_commands(callback_url, specs)
            candidate = {**self.command_store.all(), callback_url: specs}
            built = [build_command(spec, self._invoke_command)
                     for project_specs in candidate.values() for spec in project_specs]
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.clear_commands(guild=guild)
            try:
                for command in built:
                    self.tree.add_command(command, guild=guild)
                synced = await self.tree.sync(guild=guild)
                self.command_store.set_project_commands(callback_url, specs)
            except Exception:
                self._rebuild_commands()
                # A network timeout can leave Discord's remote result unknown;
                # the next successful full sync reconciles it. Local routes stay old.
                raise
            return len(synced)

    async def _invoke_command(
        self, spec: CommandSpec, interaction: discord.Interaction, options: dict[str, Any]
    ) -> None:
        """Deliver a command invocation to its owning project. The project's
        HTTP response IS the interaction response (ADR 0003)."""
        interaction_id = str(interaction.id)
        self._interactions[interaction_id] = (interaction, time.monotonic())
        self.interaction_stats["received"] += 1
        callback_url = self.command_store.owner_of(spec.name)
        logger.info(
            "interaction %s received: /%s by %s in channel %s -> %s",
            interaction_id,
            spec.name,
            interaction.user.display_name,
            interaction.channel_id,
            callback_url or "UNROUTED",
        )
        if callback_url is None:
            self.interaction_stats["unrouted"] += 1
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
        started = time.monotonic()
        try:
            async with self._http.post(
                callback_url,
                json=event,
                timeout=aiohttp.ClientTimeout(total=_CALLBACK_BUDGET_S),
            ) as resp:
                status = resp.status
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError):
            self.interaction_stats["callback_failed"] += 1
            logger.exception(
                "interaction %s (/%s) callback to %s failed; auto-deferring",
                interaction_id,
                spec.name,
                callback_url,
            )
            body = {"defer": True}
        else:
            self.interaction_stats["delivered"] += 1
            logger.info(
                "interaction %s (/%s) callback %s -> HTTP %s in %dms (defer=%s)",
                interaction_id,
                spec.name,
                callback_url,
                status,
                round((time.monotonic() - started) * 1000),
                body.get("defer", False),
            )

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
        first_id = None
        for chunk in _chunks(text):
            message = await interaction.followup.send(chunk, ephemeral=ephemeral)
            first_id = first_id or message.id
        return {"id": str(first_id)}

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

    # -- voice (ADR 0005) --------------------------------------------------

    async def voice_join(self, channel_id: int, owner: str) -> dict:
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise LookupError(f"Unknown channel {channel_id}") from exc
        if not isinstance(channel, discord.VoiceChannel):
            raise LookupError(f"Channel {channel_id} is not a voice channel")
        return await self.voice.join(channel, owner)

    # -- registrations ----------------------------------------------------

    async def register(
        self,
        channel_id: int,
        callback_url: str,
        display_name: str | None = None,
        avatar_url: str | None = None,
        allow_thread_deletion: bool = False,
        voice_events: bool = False,
        reaction_events: bool = False,
    ) -> Registration:
        async with self._registrations_lock:
            return await self._register_channel(
                channel_id, callback_url, display_name, avatar_url,
                allow_thread_deletion, voice_events, reaction_events,
            )

    async def _register_channel(
        self, channel_id: int, callback_url: str,
        display_name: str | None, avatar_url: str | None,
        allow_thread_deletion: bool, voice_events: bool = False,
        reaction_events: bool = False,
    ) -> Registration:
        if self.registry.get(channel_id) is not None:
            raise ChannelTakenError(f"channel {channel_id} is already registered")
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
            allow_thread_deletion=allow_thread_deletion,
            voice_events=voice_events,
            reaction_events=reaction_events,
        )
        try:
            self.registry.add(reg)
        except Exception:
            try:
                await webhook.delete(reason="registration could not be saved")
            except discord.HTTPException:
                logger.warning("could not clean up webhook for channel %s", channel_id)
            raise
        logger.info("registered channel %s -> %s", channel_id, callback_url)
        return reg

    async def deregister(self, channel_id: int) -> Registration | None:
        async with self._registrations_lock:
            return await self._deregister_channel(channel_id)

    async def _deregister_channel(self, channel_id: int) -> Registration | None:
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
