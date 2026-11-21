"""Remember the time already credited by progress reports.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lecture_progress",
        sa.Column("watch_budget_used_until", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lecture_progress", "watch_budget_used_until")
