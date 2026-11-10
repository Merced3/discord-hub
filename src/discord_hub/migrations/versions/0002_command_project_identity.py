"""Add authenticated project identity to command routes.

Revision ID: 0002_command_project_identity
Revises: 0001_hub_state
"""

from alembic import op

revision = "0002_command_project_identity"
down_revision = "0001_hub_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE command_specs ADD COLUMN project_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE command_specs DROP COLUMN project_id")
