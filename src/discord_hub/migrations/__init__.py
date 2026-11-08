"""Packaged Alembic migrations for hub state."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent))
    config.attributes["database_url"] = database_url
    return config


def upgrade(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")
