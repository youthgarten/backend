"""Keep refresh-token rotations in the same login session."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("family_id", sa.CHAR(64), nullable=True))
    op.execute(
        "UPDATE refresh_token SET family_id = token_hash WHERE family_id IS NULL"
    )
    op.alter_column(
        "refresh_token", "family_id", existing_type=sa.CHAR(64), nullable=False
    )
    op.create_index("ix_refresh_token_family_id", "refresh_token", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_token_family_id", table_name="refresh_token")
    op.drop_column("refresh_token", "family_id")
