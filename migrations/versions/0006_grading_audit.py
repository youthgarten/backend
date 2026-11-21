"""Keep grading model, prompt, and terminal failure metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempt", sa.Column("grading_model", sa.String(100), nullable=True))
    op.add_column(
        "attempt", sa.Column("grading_prompt_version", sa.String(32), nullable=True)
    )
    op.add_column("attempt", sa.Column("grading_failure", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("attempt", "grading_failure")
    op.drop_column("attempt", "grading_prompt_version")
    op.drop_column("attempt", "grading_model")
