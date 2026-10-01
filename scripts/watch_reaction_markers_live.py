"""Live reaction-marker check in the LAB category.

Registers lab-text with a stand-in consumer that opts into reaction_events,
then prints every reaction_marker callback the hub routes. React with a
configured emoji (REACTION_MARKERS in .env) on any message in the server —
old messages included — and you should see the event here. Removing the
reaction prints the matching "removed" event.

Usage:
    python scripts/watch_reaction_markers_live.py [channel_id]

Default channel: LAB's lab-text. Stop with Ctrl+C (deregisters on exit).
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from aiohttp import web

BASE_URL = "http://127.0.0.1:8100"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8199
LAB_TEXT_CHANNEL_ID = 1554202925012877373


async def main() -> None:
    channel_id = int(sys.argv[1]) if len(sys.argv) > 1 else LAB_TEXT_CHANNEL_ID

    async def on_callback(request: web.Request) -> web.Response:
        payload = await request.json()
        print("event:", json.dumps(payload, ensure_ascii=False), flush=True)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/discord", on_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, CALLBACK_HOST, CALLBACK_PORT).start()
    callback_url = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/discord"

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as http:
        resp = await http.post("/registrations", json={
            "channel_id": channel_id,
            "callback_url": callback_url,
            "display_name": "marker-watch",
            "reaction_events": True,
        })
        if resp.status_code == 409:
            print(f"channel {channel_id} already registered to another project —")
            print("deregister it first (DELETE /registrations/{channel_id}) or pass a different channel_id")
            return
        resp.raise_for_status()
        print(f"registered channel {channel_id} with reaction_events=true", flush=True)
        print("react with a configured emoji on any message in the server...", flush=True)
        try:
            await asyncio.Event().wait()  # run until Ctrl+C
        finally:
            await http.delete(f"/registrations/{channel_id}")
            print("deregistered", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
