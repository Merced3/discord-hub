"""Settings for discord-hub, loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_token: str
    discord_guild_id: int
    hub_host: str = "127.0.0.1"
    hub_port: int = 8100
    data_dir: Path = Path("data")
    # JSON remains the local default; production selects transactional Postgres.
    discord_hub_database_url: str | None = None
    # Not used yet; kept here so .env can hold it for future features.
    discord_application_id: int | None = None
    # Reaction markers: emoji -> marker name, as a JSON object in the env var
    # (REACTION_MARKERS='{"🔄": "belief_change", "🔒": "private"}').
    # Registrations opt in with reaction_events: true and receive a callback
    # whenever a configured emoji is added to / removed from any message.
    reaction_markers: dict[str, str] = {}

    # Cross-host caller identity. Disabled preserves the pre-ADR-0011 API;
    # optional accepts legacy and authenticated callers; required is the
    # final enforcement state. Dict values are secrets supplied through env.
    hub_auth_mode: Literal["disabled", "optional", "required"] = "disabled"
    hub_project_tokens: dict[str, str] = {}
    hub_operator_token: str | None = None
    # One-time, explicit ownership assignment for pre-auth persisted state.
    # Keys are exact callback URLs; values are non-secret project ids.
    hub_legacy_callback_projects: dict[str, str] = {}

    @model_validator(mode="after")
    def validate_auth_configuration(self):
        if self.hub_auth_mode != "disabled" and not self.hub_project_tokens:
            raise ValueError("HUB_PROJECT_TOKENS must name at least one project")
        if any(
            not project.strip() or not token
            for project, token in self.hub_project_tokens.items()
        ):
            raise ValueError("project ids and API tokens must be non-empty")
        tokens = list(self.hub_project_tokens.values())
        if len(set(tokens)) != len(tokens):
            raise ValueError("each project must have a distinct API token")
        if self.hub_operator_token in tokens:
            raise ValueError("operator and project API tokens must be distinct")
        if self.hub_operator_token is not None and not self.hub_operator_token:
            raise ValueError("HUB_OPERATOR_TOKEN must be non-empty when set")
        unknown = set(self.hub_legacy_callback_projects.values()) - set(self.hub_project_tokens)
        if unknown:
            raise ValueError(f"legacy callback mapping names unknown projects: {sorted(unknown)}")
        return self
