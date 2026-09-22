"""Print the server's structure as a readable tree, with channel IDs.

The IDs are what you paste into API calls (e.g. POST /messages) and .env
files, so they're shown inline.

    python scripts/show_structure.py
"""

from __future__ import annotations

from _connect import connected_bot, run


async def main() -> None:
    async with connected_bot() as bot:
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
