"""Record immutable versions of published lecture and task content."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_release",
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("catalog_key", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lecture_id", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name="fk_catalog_release_job_id_job"),
        sa.ForeignKeyConstraint(
            ["lecture_id"], ["lecture.id"], name="fk_catalog_release_lecture_id_lecture"
        ),
        sa.PrimaryKeyConstraint("job_id", "catalog_key", "version", name="pk_catalog_release"),
        sa.UniqueConstraint("lecture_id", name="uq_catalog_release_lecture_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_catalog_release_job_key_active", "catalog_release", ["job_id", "catalog_key", "active"]
    )


def downgrade() -> None:
    op.drop_table("catalog_release")
