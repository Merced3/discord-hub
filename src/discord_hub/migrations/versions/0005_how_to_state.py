"""Generated #how-to message IDs and content fingerprints.

Revision ID: 0005_how_to_state
Revises: 0004_archive_event_scope
"""

from alembic import op

revision = "0005_how_to_state"
down_revision = "0004_archive_event_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE how_to_state (
            state_key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            message_ids JSONB NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE how_to_state")
