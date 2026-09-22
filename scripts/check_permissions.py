"""Report which Discord permissions the hub's bot has — and which it's missing.

Permissions are granted as features land (see AGENTS.md). This script shows,
per feature, whether the bot currently holds the permissions that feature
needs. Run it after changing anything in the Discord Developer Portal:

    python scripts/check_permissions.py
"""

from __future__ import annotations

from _connect import connected_bot, run

# Feature -> permissions it needs. Mirrors docs/roadmap.md ordering.
FEATURES: dict[str, list[str]] = {
    "Read server structure (works now)": ["view_channel"],
    "Send messages (works now)": ["view_channel", "send_messages", "read_message_history"],
    "Per-project identity via webhooks (planned)": ["manage_webhooks"],
    "Voice conversations (planned)": ["connect", "speak"],
    "Caretaker edits to server structure (planned)": ["manage_channels", "manage_roles"],
}


async def main() -> None:
    async with connected_bot() as bot:
        guild = bot.get_guild(bot.settings.discord_guild_id)
        if guild is None:
            print(f"ERROR: bot is not a member of guild {bot.settings.discord_guild_id}")
            return
        me = guild.me
        perms = me.guild_permissions
        print(f"Bot: {bot.user}  |  Server: {guild.name}")
        print(f"Administrator: {perms.administrator}\n")

        all_good = True
        for feature, needed in FEATURES.items():
            missing = [p for p in needed if not getattr(perms, p)]
            mark = "OK " if not missing else "MISSING"
            if missing and "planned" not in feature:
                all_good = False
            print(f"[{mark:7}] {feature}")
            for p in missing:
                print(f"           - {p}")

        print()
        if all_good:
            print("Everything needed for the current roadmap phase is granted.")
        else:
            print("Fix the MISSING non-planned items in the Developer Portal / server roles.")
        print("Note: channel-level permission overrides can still block individual channels.")


if __name__ == "__main__":
    run(main())
