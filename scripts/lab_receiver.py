"""Throwaway callback receiver for live LAB verification.

Runs in a container on the compose network; the hub registers a LAB
channel with callback http://lab-receiver:9800/cb and this logs every
event the hub delivers. Not part of the product; used by live sessions.
"""

import json

from aiohttp import web


async def cb(request: web.Request) -> web.Response:
    body = await request.json()
    print("EVENT " + json.dumps(body, ensure_ascii=False), flush=True)
    t = body.get("type")
    if t == "command":
        return web.json_response({"text": "lab receiver ack", "ephemeral": True})
    if t == "autocomplete":
        return web.json_response({"choices": [{"name": "ack", "value": "ack"}]})
    return web.json_response({"ok": True})


app = web.Application()
app.router.add_post("/cb", cb)
web.run_app(app, host="0.0.0.0", port=9800, print=None)
