"""Append-only Discord capture, private read model, and resumable backfill.

Revision ID: 0003_message_archive
Revises: 0002_command_project_identity
"""

from alembic import op

revision = "0003_message_archive"
down_revision = "0002_command_project_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE archive_messages (
            message_id BIGINT PRIMARY KEY,
            channel_id BIGINT NOT NULL,
            parent_channel_id BIGINT,
            author JSONB NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            edited_at TIMESTAMPTZ,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            private BOOLEAN NOT NULL DEFAULT FALSE,
            attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
            raw JSONB NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX idx_archive_messages_channel "
        "ON archive_messages (channel_id, message_id DESC)"
    )
    op.execute("""
        CREATE TABLE archive_events (
            id BIGSERIAL PRIMARY KEY,
            event_type TEXT NOT NULL,
            channel_id BIGINT NOT NULL,
            message_id BIGINT,
            actor_id BIGINT,
            emoji TEXT,
            payload JSONB NOT NULL,
            raw JSONB NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            dedupe_key TEXT,
            CONSTRAINT archive_event_type CHECK (event_type IN
                ('message_created','message_edited','message_deleted',
                 'reaction_added','reaction_removed','thread_created',
                 'thread_updated','thread_deleted'))
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX idx_archive_events_dedupe "
        "ON archive_events (dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    op.execute("CREATE INDEX idx_archive_events_channel ON archive_events (channel_id, id)")
    op.execute("CREATE INDEX idx_archive_events_message ON archive_events (message_id, id)")
    op.execute("""
        CREATE TABLE archive_backfills (
            channel_id BIGINT PRIMARY KEY,
            next_before BIGINT,
            complete BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE archive_backfills")
    op.execute("DROP TABLE archive_events")
    op.execute("DROP TABLE archive_messages")
