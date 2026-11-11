"""Backfill archive event read scope for databases that applied early 0003.

Revision ID: 0004_archive_event_scope
Revises: 0003_message_archive
"""

from alembic import op

revision = "0004_archive_event_scope"
down_revision = "0003_message_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The first live 0003 predates the merged thread-scope hardening. Alembic
    # cannot retroactively edit that applied revision; move existing rows forward.
    op.execute("ALTER TABLE archive_events ADD COLUMN IF NOT EXISTS scope_channel_id BIGINT")
    op.execute("""
        UPDATE archive_events e SET scope_channel_id=COALESCE(
            (SELECT m.parent_channel_id FROM archive_messages m
             WHERE m.message_id=e.message_id), e.channel_id)
    """)
    op.execute("ALTER TABLE archive_events ALTER COLUMN scope_channel_id SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS idx_archive_events_channel")
    op.execute(
        "CREATE INDEX idx_archive_events_channel ON archive_events (scope_channel_id, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_archive_events_channel")
    op.execute("CREATE INDEX idx_archive_events_channel ON archive_events (channel_id, id)")
    op.execute("ALTER TABLE archive_events DROP COLUMN scope_channel_id")
