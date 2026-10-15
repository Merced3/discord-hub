"""Standalone voice-receive diagnostic: joins a voice channel directly
(bypassing the hub), prints DAVE session state and decode stats.

Usage: python scripts/diag_voice_recv.py [channel_id]
"""

from __future__ import annotations

import asyncio
import sys

from _connect import connected_bot, run
from discord.ext import voice_recv

CHANNEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 1554202973784113212
counts = {"frames": 0, "no_user": 0, "no_pcm": 0}


async def main() -> None:
    async with connected_bot() as bot:
        channel = bot.get_channel(CHANNEL_ID)
        vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        print("connected:", vc.is_connected())

        def on_audio(user, data: voice_recv.VoiceData) -> None:
            counts["frames"] += 1
            if user is None:
                counts["no_user"] += 1
            if data.pcm is None:
                counts["no_pcm"] += 1

        vc.listen(voice_recv.BasicSink(on_audio))

        for i in range(17):
            await asyncio.sleep(2)
            state = getattr(vc, "_connection", None)
            session = getattr(state, "dave_session", None)
            print(
                f"t={2 * (i + 1)}s dave_v={getattr(state, 'dave_protocol_version', None)} "
                f"ready={getattr(session, 'ready', None)} frames={counts} "
                f"ssrc_map={vc._ssrc_to_id}"
            )

        vc.stop_listening()
        await vc.disconnect()


run(main())
