"""Report which Discord permissions the hub's bot has — and which it's missing.

Permissions are granted as features land (see AGENTS.md). This script shows,
per feature, whether the bot currently holds the permissions that feature
needs. Run it after changing anything in the Discord Developer Portal:

    python scripts/check_permissions.py

Guild-wide permissions are only half the story: category/channel overrides
can grant or deny per channel. Pass one or more channel or thread IDs to see
the bot's *effective* permissions in each, with forum needs checked
explicitly (a forum's posts are threads and inherit the forum's overrides):

    python scripts/check_permissions.py 1554203089832124506   # lab-forum
"""

from __future__ import annotations

import sys

import discord
from _connect import connected_bot, run

# Feature -> permissions it needs. Mirrors docs/roadmap.md ordering.
FEATURES: dict[str, list[str]] = {
    "Read server structure (works now)": ["view_channel"],
    "Send messages (works now)": ["view_channel", "send_messages", "read_message_history"],
    "Per-project identity via webhooks (works now)": ["manage_webhooks"],
    "Threads for per-session conversations": ["create_public_threads"],
    "Thread deletion (opt-in per registration)": ["manage_threads"],
    "Forums: read posts & comments": ["view_channel", "read_message_history"],
    "Forums: create posts": ["send_messages"],
    "Forums: comment in posts": ["send_messages_in_threads"],
    "Forums: attachments": ["attach_files"],
    "Forums: moderation (retag/rename/archive/lock/pin/delete posts)": ["manage_threads"],
    "Forums: tag-set administration (caretaker)": ["manage_channels"],
    "Voice conversations (planned)": ["connect", "speak"],
    "Caretaker edits to server structure (planned)": ["manage_channels", "manage_roles"],
}

# The needs worth auditing per-channel: everything forums touch, plus the
# basics. Guild-wide grants and channel overrides both resolve into these.
CHANNEL_AUDIT = [
    "view_channel",
    "read_message_history",
    "send_messages",
    "send_messages_in_threads",
    "attach_files",
    "embed_links",
    "add_reactions",
    "manage_messages",
    "manage_threads",
    "manage_channels",
    "manage_webhooks",
]


def report_feature_table(perms: discord.Permissions, scope: str) -> bool:
    all_good = True
    for feature, needed in FEATURES.items():
        missing = [p for p in needed if not getattr(perms, p)]
        mark = "OK " if not missing else "MISSING"
        if missing and "planned" not in feature:
            all_good = False
        print(f"[{mark:7}] {feature}")
        for p in missing:
            print(f"           - {p}")
    if scope == "guild":
        print()
    return all_good


async def report_channel(guild: discord.Guild, channel_id: int) -> None:
    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            print(f"\n=== {channel_id}: NOT FOUND in {guild.name} ===")
            return
    kind = type(channel).__name__
    parent = f"  (under {channel.parent.name})" if getattr(channel, "parent", None) else ""
    print(f"\n=== #{channel.name} [{kind}]{parent} — effective permissions ===")
    perms = channel.permissions_for(guild.me)
    for name in CHANNEL_AUDIT:
        mark = "yes" if getattr(perms, name) else "NO "
        print(f"  [{mark}] {name}")
    print()
    report_feature_table(perms, scope="channel")


async def main(channel_ids: list[int]) -> None:
    async with connected_bot() as bot:
        guild = bot.get_guild(bot.settings.discord_guild_id)
        if guild is None:
            print(f"ERROR: bot is not a member of guild {bot.settings.discord_guild_id}")
            return
        me = guild.me
        perms = me.guild_permissions
        print(f"Bot: {bot.user}  |  Server: {guild.name}")
        print(f"Administrator: {perms.administrator}\n")

        all_good = report_feature_table(perms, scope="guild")

        if all_good:
            print("Everything needed for the current roadmap phase is granted guild-wide.")
        else:
            print("Fix the MISSING non-planned items in the Developer Portal / server roles.")
        print("Note: channel-level permission overrides can still block individual channels.")

        for channel_id in channel_ids:
            await report_channel(guild, channel_id)


if __name__ == "__main__":
    run(main([int(arg) for arg in sys.argv[1:]]))
