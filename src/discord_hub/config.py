"""Settings for discord-hub, loaded from environment / .env file."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_token: str
    discord_guild_id: int
    hub_host: str = "127.0.0.1"
    hub_port: int = 8100
    data_dir: Path = Path("data")
    # Explicit operator approval: only threads under these parents may be deleted.
    thread_deletion_allowed_parent_ids: list[int] = Field(default_factory=list)
    # Not used yet; kept here so .env can hold it for future features.
    discord_application_id: int | None = None
