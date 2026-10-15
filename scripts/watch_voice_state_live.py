"""Live check for routed voice-state events.

Registers lab-text with voice_events=True and a local callback, then prints
every voice join/leave/move the hub routes to it. Hop between voice
channels (or join and leave) to see events. Stop with Ctrl+C; the
registration is removed on exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import httpx
from aiohttp import web

BASE_URL = "http://127.0.0.1:8100"
LAB_TEXT_CHANNEL_ID = 1554202925012877373
CALLBACK_PORT = 8199
CALLBACK_URL = f"http://127.0.0.1:{CALLBACK_PORT}/cb"


async def main() -> None:
    async def callback(request: web.Request) -> web.Response:
        event = await request.json()
        print("voice-state event:", event, flush=True)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/cb", callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", CALLBACK_PORT).start()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as http:
        resp = await http.post(
            "/registrations",
            json={
                "channel_id": LAB_TEXT_CHANNEL_ID,
                "callback_url": CALLBACK_URL,
                "display_name": "lab-voice-events-test",
                "voice_events": True,
            },
        )
        print("register:", resp.status_code, flush=True)
        if resp.status_code == 409:
            print("lab-text already registered — deregister it first", flush=True)
            return
        resp.raise_for_status()
        print("listening — join/leave/move voice channels now", flush=True)
        try:
            await asyncio.Event().wait()  # forever
        finally:
            await http.delete(f"/registrations/{LAB_TEXT_CHANNEL_ID}")
            print("deregistered", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
