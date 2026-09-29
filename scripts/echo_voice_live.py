"""Live full-duplex check in the LAB voice channel (ADR 0005, Stage 2).

Joins a voice channel via the hub, attaches the duplex stream, and ECHOES
any received audio straight back. If you join the channel and speak, you
should hear yourself — proving receive (DAVE decryption) and send at once.

Usage:
    python scripts/test_voice_stream_live.py [channel_id]

Default channel: LAB's lab-voice. Stop with Ctrl+C.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

import httpx
import websockets

BASE_URL = "http://127.0.0.1:8100"

OWNER = "http://localhost:8100/lab-echo-test"
LAB_VOICE_CHANNEL_ID = 1554202973784113212


async def main() -> None:
    channel_id = int(sys.argv[1]) if len(sys.argv) > 1 else LAB_VOICE_CHANNEL_ID
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as http:
        resp = await http.post("/voice/join", json={"channel_id": channel_id, "owner": OWNER})
        print("join:", resp.status_code, resp.json())
        resp.raise_for_status()

    ws_url = BASE_URL.replace("http", "ws") + f"/voice/stream?owner={OWNER}"
    speakers: dict[str, int] = {}
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            print("stream attached — speak in the channel; you should hear an echo")
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "speaking":
                    print(f"speaking {event['state']}: user {event['user_id']}")
                elif event["type"] == "audio":
                    pcm = base64.b64decode(event["pcm"])
                    speakers[event["user_id"]] = speakers.get(event["user_id"], 0) + len(pcm)
                    await ws.send(pcm)  # echo back: full-duplex proof
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print("audio bytes received per user:", speakers or "(none)")
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as http:
            await http.post("/voice/leave", json={"owner": OWNER})
        print("left voice")


if __name__ == "__main__":
    asyncio.run(main())
