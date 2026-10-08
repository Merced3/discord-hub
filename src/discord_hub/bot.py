"""The Discord connection itself — the only file that knows discord.py."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord

from .capabilities import tier_limit as _tier_limit
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

    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """Fires only when the pre-edit message is in the local cache;
        on_raw_message_edit covers the rest."""
        await self._route_message_edit(after, before_text=before.content)

    async def on_raw_message_edit(self, event: discord.RawMessageUpdateEvent) -> None:
        """Fires for every edit. When the original was cached,
        on_message_edit already handled it — skip those to avoid double
        delivery. For uncached originals, fetch the current state and
        report the edit with before_text=None (unknown prior state)."""
        if event.cached_message is not None:
            return
        channel = self.get_channel(event.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(event.channel_id)
            except (discord.NotFound, discord.Forbidden):
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        try:
            message = await channel.fetch_message(event.message_id)
        except (discord.NotFound, discord.Forbidden):
            return  # deleted or unreadable between the edit and our fetch
        await self._route_message_edit(message, before_text=None)

    async def _route_message_edit(
        self, message: discord.Message, before_text: str | None
    ) -> None:
        """Deliver every edit to the channel's registered project, like
        on_message. The hub is stateless about edits: each one is its own
        event, so an archiver can replay the full before/after chain."""
        if message.author.bot:  # same rule as message callbacks
            return
        thread_info = None
        reg = self.registry.get(message.channel.id)
        if reg is None and isinstance(message.channel, discord.Thread):
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
            "type": "message_edit",
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "author": {"id": str(message.author.id), "name": message.author.display_name},
            "before_text": before_text,
            "after_text": message.content,
            "edited_at": message.edited_at.isoformat() if message.edited_at else None,
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
                        "edit delivery to %s failed: HTTP %s", reg.callback_url, resp.status
                    )
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("edit callback failed for channel %s; not retried", message.channel.id)

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

    # -- forums: lifecycle events (ADR 0009) -------------------------------

    async def _deliver_forum_event(self, reg: Registration, payload: dict) -> None:
        try:
            async with self._http.post(
                reg.callback_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=_MESSAGE_CALLBACK_BUDGET_S),
                allow_redirects=False,
            ) as resp:
                if not 200 <= resp.status < 300:
                    logger.warning(
                        "forum-event delivery to %s failed: HTTP %s",
                        reg.callback_url, resp.status,
                    )
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("forum-event callback failed for %s; not retried", reg.channel_id)

    def _forum_events_reg(self, thread: discord.Thread) -> Registration | None:
        """The registration governing a forum post's lifecycle events, if
        the parent forum is registered and opted in."""
        if not isinstance(thread.parent, discord.ForumChannel):
            return None
        if thread.guild is None or thread.guild.id != self.settings.discord_guild_id:
            return None
        reg = self.registry.get(thread.parent_id)
        return reg if reg is not None and reg.forum_events else None

    async def on_thread_create(self, thread: discord.Thread) -> None:
        reg = self._forum_events_reg(thread)
        if reg is None:
            return
        payload = {"type": "forum.post_created", **self._post_summary(thread)}
        payload["forum_channel_id"] = str(thread.parent_id)
        await self._deliver_forum_event(reg, payload)

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        reg = self._forum_events_reg(after)
        if reg is None:
            return
        changed = []
        if before.name != after.name:
            changed.append("name")
        if {t.id for t in before.applied_tags} != {t.id for t in after.applied_tags}:
            changed.append("tags")
        if before.archived != after.archived:
            changed.append("archived")
        if before.locked != after.locked:
            changed.append("locked")
        if before.flags.pinned != after.flags.pinned:
            changed.append("pinned")
        if not changed:
            return
        payload = {"type": "forum.post_updated", "changed": changed,
                   **self._post_summary(after)}
        payload["forum_channel_id"] = str(after.parent_id)
        await self._deliver_forum_event(reg, payload)

    async def on_thread_delete(self, thread: discord.Thread) -> None:
        reg = self._forum_events_reg(thread)
        if reg is None:
            return
        payload = {"type": "forum.post_deleted", "id": str(thread.id),
                   "title": thread.name,
                   "forum_channel_id": str(thread.parent_id)}
        await self._deliver_forum_event(reg, payload)

    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        if not isinstance(after, discord.ForumChannel):
            return
        if after.guild.id != self.settings.discord_guild_id:
            return
        reg = self.registry.get(after.id)
        if reg is None or not reg.forum_events:
            return
        before_forum = before if isinstance(before, discord.ForumChannel) else None
        changed = []
        if before.name != after.name:
            changed.append("name")
        if before_forum is not None and (
            {t.id for t in before_forum.available_tags}
            != {t.id for t in after.available_tags}
            or [t.name for t in before_forum.available_tags]
            != [t.name for t in after.available_tags]
        ):
            changed.append("available_tags")
        if getattr(before, "slowmode_delay", None) != after.slowmode_delay:
            changed.append("slowmode")
        if not changed:
            return
        payload = {
            "type": "forum.channel_updated",
            "changed": changed,
            "id": str(after.id),
            "name": after.name,
            "available_tags": [
                {"id": str(t.id), "name": t.name} for t in after.available_tags
            ],
        }
        await self._deliver_forum_event(reg, payload)

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

    async def send_attachment(
        self, channel_id: int, filename: str, data: bytes, text: str | None = None
    ) -> dict:
        """Attach one file to a message (ADR 0007). Bytes arrive from the
        consumer; the hub never reads a consumer's disk. Size was already
        enforced at the API boundary. Registered channels keep their
        webhook identity — webhooks support file uploads."""
        import io

        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise LookupError(f"Channel {channel_id} is not a text channel or thread")

        thread = channel if isinstance(channel, discord.Thread) else None
        reg = self.registry.get(channel_id)
        if reg is None and thread is not None:
            reg = self.registry.get(thread.parent_id)

        def make_file() -> discord.File:
            return discord.File(io.BytesIO(data), filename=filename)

        if reg is not None and reg.webhook_url:
            webhook = discord.Webhook.from_url(reg.webhook_url, client=self)
            kwargs: dict[str, Any] = {"wait": True}
            if reg.display_name:
                kwargs["username"] = reg.display_name
            if reg.avatar_url:
                kwargs["avatar_url"] = reg.avatar_url
            if thread is not None:
                kwargs["thread"] = thread
            message = await webhook.send(content=text, file=make_file(), **kwargs)
            return {"id": str(message.id), "channel_id": str(channel_id), "via": "webhook"}

        message = await channel.send(content=text, file=make_file())
        return {"id": str(message.id), "channel_id": str(channel.id), "via": "bot"}

    async def edit_message(
        self, channel_id: int, message_id: int, text: str
    ) -> dict:
        """Edit a message the hub previously sent. Discord only lets us
        edit our own messages: bot-sent ones via Message.edit, webhook-
        sent ones via the project's webhook. Anything else raises
        PermissionError. Unlike send, an edit is a single message — no
        chunking — so text must fit Discord's limit.
        """
        if not text.strip():
            raise ValueError("edited text must not be empty")
        if len(text) > 2_000:
            raise ValueError("edited text exceeds Discord's 2000-character limit")
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise LookupError(f"Channel {channel_id} is not a text channel or thread")

        thread = channel if isinstance(channel, discord.Thread) else None
        message = await channel.fetch_message(message_id)  # 404 -> discord.NotFound
        if self.user is not None and message.author.id == self.user.id:
            edited = await message.edit(content=text)
            via = "bot"
        elif message.webhook_id is not None:
            reg = self.registry.get(channel_id)
            if reg is None and thread is not None:
                reg = self.registry.get(thread.parent_id)
            if reg is None or not reg.webhook_url:
                raise PermissionError(
                    "message was sent via webhook but no registration with a "
                    "webhook_url covers this channel"
                )
            webhook = discord.Webhook.from_url(reg.webhook_url, client=self)
            kwargs: dict[str, Any] = {}
            if thread is not None:
                kwargs["thread"] = thread
            edited = await webhook.edit_message(message_id, content=text, **kwargs)
            via = "webhook"
        else:
            raise PermissionError("the hub can only edit messages it sent")
        return {
            "id": str(message_id),
            "channel_id": str(channel.id),
            "edited_at": edited.edited_at.isoformat() if edited.edited_at else None,
            "via": via,
        }

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

    # -- forums (ADR 0009) --------------------------------------------------

    def _get_forum(self, channel_id: int) -> discord.ForumChannel:
        guild = self.get_guild(self.settings.discord_guild_id)
        channel = self.get_channel(channel_id)
        if guild is None or not isinstance(channel, discord.ForumChannel):
            raise LookupError(f"Channel {channel_id} is not a forum in the configured server")
        if channel.guild.id != guild.id:
            raise PermissionError("Forum is outside the configured server")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            raise PermissionError("Reading a forum requires View Channel and Read Message History")
        return channel

    @staticmethod
    def _post_summary(thread: discord.Thread) -> dict:
        return {
            "id": str(thread.id),
            "title": thread.name,
            "author": {"id": str(thread.owner_id), "name": thread.owner.display_name}
            if thread.owner else None,
            "tags": [{"id": str(t.id), "name": t.name} for t in thread.applied_tags],
            "created_at": thread.created_at.isoformat(),
            "comment_count": thread.message_count,
            "pinned": thread.flags.pinned,
            "locked": thread.locked,
            "archived": thread.archived,
        }

    async def forum_posts(
        self, channel_id: int, include_archived: bool = False, limit: int = 50
    ) -> dict:
        """Active posts from cache; archived only on request, and bounded
        (ADR 0009: no request may trigger an unbounded crawl)."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        forum = self._get_forum(channel_id)
        posts = [self._post_summary(t) for t in forum.threads]
        truncated = False
        if include_archived:
            remaining = limit - len(posts)
            if remaining > 0:
                async for thread in forum.archived_threads(limit=remaining):
                    posts.append(self._post_summary(thread))
            truncated = len(posts) >= limit
        return {
            "channel_id": str(channel_id),
            "posts": posts[:limit],
            # True means more archived posts likely exist beyond the limit.
            "truncated": truncated,
        }

    async def create_forum_post(
        self, channel_id: int, title: str, text: str, tag_ids: list[int] | None = None
    ) -> dict:
        """Create a post in a forum channel. Long bodies chunk like regular
        sends: the first chunk is the post body, the rest become comments.
        Bot identity until the webhook path lands (ADR 0009, phase 3)."""
        forum = self._get_forum(channel_id)
        if not forum.permissions_for(forum.guild.me).send_messages:
            raise PermissionError("Creating posts requires Send Messages (Create Posts)")
        available = {t.id: t for t in forum.available_tags}
        unknown = [t for t in (tag_ids or []) if t not in available]
        if unknown:
            raise ValueError(f"unknown tag id(s) for this forum: {unknown}")
        kwargs: dict[str, Any] = {}
        if tag_ids:
            kwargs["applied_tags"] = [available[t] for t in tag_ids]
        chunks = _chunks(text)
        chunks = _chunks(text)
        # Webhook identity (ADR 0002/0009): in a registered forum, posts go
        # through the project's webhook. Discord can't apply tags on a
        # webhook execute, so tag_ids force the bot path.
        reg = self.registry.get(channel_id)
        if not tag_ids and reg is not None and reg.webhook_url:
            webhook = discord.Webhook.from_url(reg.webhook_url, client=self)
            wkwargs: dict[str, Any] = {"wait": True}
            if reg.display_name:
                wkwargs["username"] = reg.display_name
            if reg.avatar_url:
                wkwargs["avatar_url"] = reg.avatar_url
            try:
                starter = await webhook.send(chunks[0], thread_name=title, **wkwargs)
                thread = await self.fetch_channel(starter.id)
                for chunk in chunks[1:]:
                    await webhook.send(chunk, thread=thread, **wkwargs)
                post = self._post_summary(thread)
                post["forum_channel_id"] = str(channel_id)
                post["via"] = "webhook"
                logger.info("created forum post %s in %s via webhook", thread.id, channel_id)
                return post
            except discord.HTTPException:
                logger.warning(
                    "webhook post in forum %s failed; falling back to bot identity",
                    channel_id,
                )
        result = await forum.create_thread(name=title, content=chunks[0], **kwargs)
        # Newer discord.py returns ThreadWithMessage; older returned Thread.
        thread = getattr(result, "thread", result)
        for chunk in chunks[1:]:
            await thread.send(chunk)
        post = self._post_summary(thread)
        post["forum_channel_id"] = str(channel_id)
        post["via"] = "bot"
        logger.info("created forum post %s in %s", thread.id, channel_id)
        return post

    def _forum_moderation_reg(self, forum_id: int) -> Registration:
        """Gate for moderation-class forum actions (ADR 0009): the forum's
        registration must have opted in. Same declared-intent pattern as
        ADR 0004."""
        reg = self.registry.get(forum_id)
        if reg is None or not reg.allow_forum_moderation:
            raise PermissionError(
                "Forum moderation is not enabled for this channel's registration"
            )
        return reg

    async def _get_post(self, post_id: int) -> discord.Thread:
        channel = self.get_channel(post_id) or await self.fetch_channel(post_id)
        if not isinstance(channel, discord.Thread) or not isinstance(
            channel.parent, discord.ForumChannel
        ):
            raise LookupError(f"{post_id} is not a forum post")
        self._get_forum(channel.parent_id)
        return channel

    async def update_forum_post(
        self,
        post_id: int,
        title: str | None = None,
        tag_ids: list[int] | None = None,
        archived: bool | None = None,
        locked: bool | None = None,
        pinned: bool | None = None,
    ) -> dict:
        """Moderation-class edits to a post, gated on the parent forum's
        allow_forum_moderation registration flag (ADR 0009)."""
        thread = await self._get_post(post_id)
        self._forum_moderation_reg(thread.parent_id)
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["name"] = title
        if tag_ids is not None:
            available = {t.id: t for t in thread.parent.available_tags}
            unknown = [t for t in tag_ids if t not in available]
            if unknown:
                raise ValueError(f"unknown tag id(s) for this forum: {unknown}")
            kwargs["applied_tags"] = [available[t] for t in tag_ids]
        if archived is not None:
            kwargs["archived"] = archived
        if locked is not None:
            kwargs["locked"] = locked
        if pinned is not None:
            kwargs["pinned"] = pinned
        if not kwargs:
            raise ValueError("nothing to update")
        thread = await thread.edit(**kwargs)
        logger.info("updated forum post %s: %s", post_id, sorted(kwargs))
        post = self._post_summary(thread)
        post["forum_channel_id"] = str(thread.parent_id)
        return post

    async def _fresh_forum(self, channel_id: int) -> discord.ForumChannel:
        """Validated forum, re-fetched over HTTP so available_tags is current
        (tag creation returns before the channel cache observes it)."""
        self._get_forum(channel_id)
        forum = await self.fetch_channel(channel_id)
        if not isinstance(forum, discord.ForumChannel):
            raise LookupError(f"Channel {channel_id} is not a forum in the configured server")
        return forum

    async def create_forum_tag(
        self, channel_id: int, name: str, moderated: bool = False
    ) -> dict:
        """Add to the forum's available tag set (caretaker, ADR 0009)."""
        forum = self._get_forum(channel_id)
        self._forum_moderation_reg(channel_id)
        if not forum.permissions_for(forum.guild.me).manage_channels:
            raise PermissionError("Tag administration requires Manage Channels")
        tag = await forum.create_tag(name=name, moderated=moderated)
        logger.info("created tag %s (%s) in forum %s", tag.name, tag.id, channel_id)
        return {"id": str(tag.id), "name": tag.name, "moderated": tag.moderated}

    async def update_forum_tag(
        self, channel_id: int, tag_id: int,
        name: str | None = None, moderated: bool | None = None,
    ) -> dict:
        forum = await self._fresh_forum(channel_id)
        self._forum_moderation_reg(channel_id)
        if not forum.permissions_for(forum.guild.me).manage_channels:
            raise PermissionError("Tag administration requires Manage Channels")
        tag = next((t for t in forum.available_tags if t.id == tag_id), None)
        if tag is None:
            raise LookupError(f"Tag {tag_id} not found in forum {channel_id}")
        if name is None and moderated is None:
            raise ValueError("nothing to update")
        updated = discord.ForumTag(
            name=name if name is not None else tag.name,
            moderated=moderated if moderated is not None else tag.moderated,
        )
        # ForumTag's constructor takes no id; assign it so Discord treats
        # this as an edit of the existing tag, not a new one.
        updated.id = tag_id
        tags = [updated if t.id == tag_id else t for t in forum.available_tags]
        await forum.edit(available_tags=tags)
        logger.info("updated tag %s in forum %s", tag_id, channel_id)
        return {"id": str(tag_id),
                "name": name if name is not None else tag.name,
                "moderated": moderated if moderated is not None else tag.moderated}

    async def delete_forum_tag(self, channel_id: int, tag_id: int) -> None:
        forum = await self._fresh_forum(channel_id)
        self._forum_moderation_reg(channel_id)
        if not forum.permissions_for(forum.guild.me).manage_channels:
            raise PermissionError("Tag administration requires Manage Channels")
        tags = [t for t in forum.available_tags if t.id != tag_id]
        if len(tags) == len(forum.available_tags):
            raise LookupError(f"Tag {tag_id} not found in forum {channel_id}")
        await forum.edit(available_tags=tags)
        logger.info("deleted tag %s from forum %s", tag_id, channel_id)

    async def forum_post(self, post_id: int) -> dict:
        """One post: summary plus the starter message body."""
        channel = self.get_channel(post_id) or await self.fetch_channel(post_id)
        if not isinstance(channel, discord.Thread) or not isinstance(
            channel.parent, discord.ForumChannel
        ):
            raise LookupError(f"{post_id} is not a forum post")
        self._get_forum(channel.parent_id)  # guild + permission checks
        post = self._post_summary(channel)
        post["forum_channel_id"] = str(channel.parent_id)
        try:
            starter = await channel.fetch_message(channel.id)
        except discord.NotFound:
            starter = None
        post["body"] = starter.content if starter else None
        if starter is not None:
            post["attachments"] = [
                {"id": str(a.id), "filename": a.filename, "size": a.size,
                 "content_type": a.content_type, "url": a.url}
                for a in starter.attachments
            ]
        return post

    def server_tier(self) -> int:
        """The guild's current boost tier, read live (ADR 0008). Returns
        0 before the guild is cached, which is also the base tier."""
        guild = self.get_guild(self.settings.discord_guild_id)
        return guild.premium_tier if guild is not None else 0

    def tier_limit(self, name: str) -> int:
        """The current value of a tier-scaled limit, looked up at call
        time — never cached, so boosts take effect on the next request."""
        return _tier_limit(self.server_tier(), name)

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
        allow_forum_moderation: bool = False,
        forum_events: bool = False,
    ) -> Registration:
        async with self._registrations_lock:
            return await self._register_channel(
                channel_id, callback_url, display_name, avatar_url,
                allow_thread_deletion, voice_events, reaction_events,
                allow_forum_moderation, forum_events,
            )

    async def _register_channel(
        self, channel_id: int, callback_url: str,
        display_name: str | None, avatar_url: str | None,
        allow_thread_deletion: bool, voice_events: bool = False,
        reaction_events: bool = False,
        allow_forum_moderation: bool = False, forum_events: bool = False,
    ) -> Registration:
        if self.registry.get(channel_id) is not None:
            raise ChannelTakenError(f"channel {channel_id} is already registered")
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise LookupError(f"Channel {channel_id} is not a text or forum channel")
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
            allow_forum_moderation=allow_forum_moderation,
            forum_events=forum_events,
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
                        {
                            "id": str(ch.id), "name": ch.name, "type": "forum",
                            "topic": ch.topic or "",
                            "available_tags": [
                                {
                                    "id": str(t.id), "name": t.name,
                                    "emoji": str(t.emoji) if t.emoji else None,
                                    "moderated": t.moderated,
                                }
                                for t in ch.available_tags
                            ],
                        }
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
