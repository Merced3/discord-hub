"""End-to-end live check of forum support against the LAB forum (ADR 0009).

Covers all three phases in one run:
  1. reads — structure tags, list posts, get post
  2. writes — tagged/untagged/chunked post creation, comments, tag rejection
  3. identity & moderation — webhook posts, PATCH, tag administration,
     lifecycle events (incl. rename detection), moderation gating

Registers lab-forum with a local callback receiver for phase 3 and
deregisters at the end, restoring prior state. Test posts and tags it
creates are left in the forum (it's the test range) except temp tags.

    python scripts/check_forums.py
"""

from __future__ import annotations

import asyncio

import discord
from aiohttp import web
from _connect import connected_bot, run

LAB_FORUM = 1554203089832124506
CALLBACK_PORT = 8199

events: list[dict] = []
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


async def _recv(request: web.Request) -> web.Response:
    events.append(await request.json())
    return web.Response(status=204)


async def main() -> None:
    app = web.Application()
    app.router.add_post("/cb", _recv)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", CALLBACK_PORT).start()
    try:
        await _check()
    finally:
        await runner.cleanup()
    print()
    if failures:
        print(f"FAILURES: {failures}")
        raise SystemExit(1)
    print("All forum checks passed.")


async def _check() -> None:
    async with connected_bot() as bot:
        forum = bot.get_channel(LAB_FORUM)
        assert isinstance(forum, discord.ForumChannel)

        # -- phase 1: reads -------------------------------------------------
        print("phase 1: reads")
        structure = bot.server_structure()
        entry = next(
            c for cat in structure["categories"] if "LAB" in cat["name"]
            for c in cat["channels"] if c["type"] == "forum"
        )
        check("structure lists forum with available_tags",
              isinstance(entry.get("available_tags"), list))

        seeded = (await forum.create_thread(
            name="check-forums seed", content="seed body"
        )).thread
        posts = await bot.forum_posts(LAB_FORUM)
        check("forum_posts lists the seed",
              any(p["id"] == str(seeded.id) for p in posts["posts"]))
        one = await bot.forum_post(seeded.id)
        check("forum_post returns body", one["body"] == "seed body")
        await bot.forum_posts(LAB_FORUM, include_archived=True, limit=10)
        check("archived listing runs bounded", True)

        # -- phase 2: writes ------------------------------------------------
        print("phase 2: writes")
        existing = {t.name: t for t in forum.available_tags}
        if "test-red" not in existing:
            await forum.edit(available_tags=list(forum.available_tags) + [
                discord.ForumTag(name="test-red")
            ])
            forum = await bot.fetch_channel(LAB_FORUM)
            existing = {t.name: t for t in forum.available_tags}
        tag = existing["test-red"]

        tagged = await bot.create_forum_post(
            LAB_FORUM, "check-forums tagged", "body", tag_ids=[tag.id]
        )
        check("tagged post created (bot identity)",
              tagged["via"] == "bot" and tagged["tags"][0]["name"] == "test-red")

        long_post = await bot.create_forum_post(LAB_FORUM, "check-forums chunked", "x" * 4500)
        history = await bot.channel_history(int(long_post["id"]), limit=10)
        check("long body chunks into comments", len(history["messages"]) == 3)

        comment = await bot.send_message(int(tagged["id"]), "a comment")
        check("comment via send_message", comment["via"] in ("bot", "webhook"))

        try:
            await bot.create_forum_post(LAB_FORUM, "bad", "bad", tag_ids=[999999])
            check("unknown tag rejected", False, "no error raised")
        except ValueError:
            check("unknown tag rejected", True)

        # -- phase 3: identity, moderation, events --------------------------
        print("phase 3: identity, moderation, events")
        if bot.registry.get(LAB_FORUM) is not None:
            await bot.deregister(LAB_FORUM)
        reg = await bot.register(
            LAB_FORUM, f"http://127.0.0.1:{CALLBACK_PORT}/cb",
            display_name="Forum Check",
            allow_forum_moderation=True, forum_events=True,
        )
        check("forum registers with webhook", bool(reg.webhook_url))

        post_wh = await bot.create_forum_post(LAB_FORUM, "check-forums webhook", "hi")
        check("untagged post uses webhook identity", post_wh["via"] == "webhook")

        for t in forum.threads:  # forums allow few pinned posts (error 30047)
            if t.flags.pinned:
                await t.edit(pinned=False)
        updated = await bot.update_forum_post(
            int(post_wh["id"]), title="check-forums RENAMED", tag_ids=[tag.id], pinned=True
        )
        check("PATCH rename/retag/pin",
              updated["title"] == "check-forums RENAMED" and updated["pinned"])

        fresh = await bot.fetch_channel(LAB_FORUM)
        for t in list(fresh.available_tags):
            if t.name.startswith("check-forums-temp"):
                await bot.delete_forum_tag(LAB_FORUM, t.id)
        new_tag = await bot.create_forum_tag(LAB_FORUM, "check-forums-temp")
        renamed = await bot.update_forum_tag(LAB_FORUM, int(new_tag["id"]), name="check-forums-temp2")
        await bot.delete_forum_tag(LAB_FORUM, int(new_tag["id"]))
        check("tag admin create/rename/delete", renamed["name"] == "check-forums-temp2")

        try:
            bot._forum_moderation_reg(1550126472281198704)  # ideas: not registered
            check("moderation gate refuses unregistered forum", False)
        except PermissionError:
            check("moderation gate refuses unregistered forum", True)

        await asyncio.sleep(4)
        types = [e["type"] for e in events]
        check("event: forum.post_created", "forum.post_created" in types)
        rename_events = [
            e for e in events
            if e["type"] == "forum.post_updated" and "name" in e.get("changed", [])
        ]
        check("event: rename detected (changed=['name', ...])", bool(rename_events))
        check("event: forum.channel_updated (tag set)", "forum.channel_updated" in types)

        await bot.deregister(LAB_FORUM)
        check("deregistered (state restored)", bot.registry.get(LAB_FORUM) is None)


if __name__ == "__main__":
    run(main())
