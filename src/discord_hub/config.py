"""Settings for discord-hub, loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", populate_by_name=True
    )

    discord_token: str
    discord_guild_id: int
    hub_host: str = "127.0.0.1"
    hub_port: int = 8100
    data_dir: Path = Path("data")
    # JSON remains the local default; production selects transactional Postgres.
    discord_hub_database_url: str | None = None
    # Opt-in capture by stable channel/forum ID; threads inherit their parent.
    archive_channel_ids: list[int] = []
    # Non-secret project ID -> archive channel IDs it may read. No wildcard:
    # access to newly captured channels is always an explicit owner decision.
    archive_reader_channels: dict[str, list[int]] = {}
    archive_r2_account_id: str | None = Field(
        default=None, validation_alias="DISCORD_HUB_R2_ACCOUNT_ID"
    )
    archive_r2_access_key_id: str | None = Field(
        default=None, validation_alias="DISCORD_HUB_R2_ACCESS_KEY_ID"
    )
    archive_r2_secret_access_key: str | None = Field(
        default=None, validation_alias="DISCORD_HUB_R2_SECRET_ACCESS_KEY"
    )
    archive_r2_bucket: str | None = Field(default=None, validation_alias="DISCORD_HUB_R2_BUCKET")
    archive_r2_endpoint: str | None = Field(
        default=None, validation_alias="DISCORD_HUB_R2_ENDPOINT"
    )
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
        if self.archive_channel_ids:
            if not self.discord_hub_database_url:
                raise ValueError("archive capture requires DISCORD_HUB_DATABASE_URL")
            r2 = (
                self.archive_r2_account_id,
                self.archive_r2_access_key_id,
                self.archive_r2_secret_access_key,
                self.archive_r2_bucket,
            )
            if not all(r2):
                raise ValueError("archive capture requires the DISCORD_HUB_R2_* settings")
            unknown_readers = set(self.archive_reader_channels) - set(self.hub_project_tokens)
            if unknown_readers:
                raise ValueError(
                    f"archive readers name unknown projects: {sorted(unknown_readers)}"
                )
            if any(
                channel not in self.archive_channel_ids
                for channels in self.archive_reader_channels.values()
                for channel in channels
            ):
                raise ValueError("archive readers may only name captured channels")
        if self.hub_auth_mode != "disabled" and not self.hub_project_tokens:
            raise ValueError("HUB_PROJECT_TOKENS must name at least one project")
        if any(
            not project.strip() or not token for project, token in self.hub_project_tokens.items()
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
