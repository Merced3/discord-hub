"""Dev-only callback listener: prints every hub callback payload to stdout.

Used for manual live testing (e.g. the testing channel's marker-watch
registration points at http://127.0.0.1:8199/discord). Not part of the
hub itself; run it only while testing:

    .venv/Scripts/python scripts/dev_callback_listener.py
"""
from __future__ import annotations

import json

from aiohttp import web


async def handle(request: web.Request) -> web.Response:
    payload = await request.json()
    print("=" * 60)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return web.json_response({"ok": True})


app = web.Application()
app.router.add_post("/discord", handle)

if __name__ == "__main__":
    print("Listening on http://127.0.0.1:8199/discord — send/edit messages now.")
    web.run_app(app, host="127.0.0.1", port=8199, print=None)
