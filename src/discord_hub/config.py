"""Settings for discord-hub, loaded from environment / .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_token: str
    discord_guild_id: int
    hub_host: str = "127.0.0.1"
    hub_port: int = 8100
    # Not used yet; kept here so .env can hold them for future features.
    discord_application_id: int | None = None
    discord_public_key: str | None = None
