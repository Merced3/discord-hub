"""Registrations and command declarations; archive tables land with P4a.

Revision ID: 0001_hub_state
Revises:
"""

from alembic import op

revision = "0001_hub_state"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE channel_registrations (
            channel_id BIGINT PRIMARY KEY,
            registration JSONB NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE command_specs (
            command_name TEXT PRIMARY KEY,
            callback_url TEXT NOT NULL,
            spec JSONB NOT NULL,
            UNIQUE (callback_url, command_name)
        )
        """
    )
    op.execute("CREATE INDEX idx_command_specs_callback ON command_specs (callback_url)")


def downgrade() -> None:
    op.execute("DROP TABLE command_specs")
    op.execute("DROP TABLE channel_registrations")
