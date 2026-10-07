"""Server-tier capabilities (ADR 0008).

Discord grants limits by the guild's boost tier, and the bot can read the
current tier live (`guild.premium_tier`). Features never hardcode a limit
and never cache the tier: when they need to check a limit, they ask here,
so a boost (or an expired boost) takes effect on the very next request —
no restart, no per-feature edits.

Adding a new limit is one entry in TIER_LIMITS plus asking for it by name.
Tiers not listed for a limit inherit the base (tier 0) value, which is
also the fallback when the guild isn't known yet.
"""

from __future__ import annotations

_MIB = 1024 * 1024

# name -> {tier: value}. Tiers are Discord's premium tier integers (0-3).
TIER_LIMITS: dict[str, dict[int, int]] = {
    # Max bytes Discord accepts for one upload. Tier 1 does NOT raise it.
    "attachment_bytes": {0: 10 * _MIB, 2: 50 * _MIB, 3: 100 * _MIB},
    # NOTE: bot message text length is 2000 chars on EVERY tier (the
    # 4000-char perk is Nitro for human users, not bots), so it is
    # deliberately not here — _MESSAGE_LIMIT in bot.py stays constant.
}


def tier_limit(tier: int, name: str) -> int:
    """The limit `name` for the given boost tier. Unknown or unlisted
    tiers fall back to the tier-0 value; unknown names raise KeyError."""
    table = TIER_LIMITS[name]
    return table.get(tier, table[0])
