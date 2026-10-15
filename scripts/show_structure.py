"""Print the server's structure as a readable tree, with channel IDs.

The IDs are what you paste into API calls (e.g. POST /messages) and .env
files, so they're shown inline.

    python scripts/show_structure.py

Pass a forum channel's ID for a detailed view of just that forum: its
available tag set and its posts (active and archived), with the status
flags and comment counts a caretaker cares about.

    python scripts/show_structure.py 1554203089832124506
"""

from __future__ import annotations

import sys

import discord
from _connect import connected_bot, run


async def show_forum(bot, channel_id: int) -> None:
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.ForumChannel):
        print(f"{channel_id} is not a forum channel (pass no ID for the full tree).")
        return
    print(f"Forum: {channel.name}  ({channel.id})")
    if channel.topic:
        print(f"topic: {channel.topic}\n")
    tags = channel.available_tags
    print(
        "Tags: "
        + (
            ", ".join(f"{t.name} ({t.id}){'[moderated]' if t.moderated else ''}" for t in tags)
            if tags
            else "none"
        )
    )
    listing = await bot.forum_posts(channel_id, include_archived=True, limit=100)
    print(
        f"Posts: {len(listing['posts'])}"
        + (" (truncated at limit)" if listing["truncated"] else "")
    )
    for p in listing["posts"]:
        flags = " ".join(
            f"[{f}]"
            for f in (
                "pinned" if p["pinned"] else "",
                "locked" if p["locked"] else "",
                "archived" if p["archived"] else "",
            )
            if f
        )
        tag_names = ", ".join(t["name"] for t in p["tags"])
        print(
            f"  {p['id']}  {p['title']!r}  comments={p['comment_count']}"
            f"{'  tags: ' + tag_names if tag_names else ''}{'  ' + flags if flags else ''}"
        )


async def main() -> None:
    async with connected_bot() as bot:
        if len(sys.argv) > 1:
            await show_forum(bot, int(sys.argv[1]))
            return
        structure = bot.server_structure()
        print(f"Server: {structure['guild']}\n")
        for category in structure["categories"]:
            print(category["name"])
            for ch in category["channels"]:
                icon = {"voice": "[voice]", "forum": "[forum]"}.get(ch["type"], "#      ")
                print(f"  {icon} {ch['name']:<24} {ch['id']}")
                if ch.get("topic"):
                    print(f"          topic: {ch['topic']}")
        print("\nRoles: " + ", ".join(structure["roles"]))


if __name__ == "__main__":
    run(main())
