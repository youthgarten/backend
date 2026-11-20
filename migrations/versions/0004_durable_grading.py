"""Persist grading work, attempt content, and revision evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempt", sa.Column("task_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "answer_revision", sa.Column("after_message_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_answer_revision_after_message_id_chat_message"),
        "answer_revision",
        "chat_message",
        ["after_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "attempt_score", sa.Column("evidence_revision_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_attempt_score_evidence_revision_id_answer_revision"),
        "attempt_score",
        "answer_revision",
        ["evidence_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "grading_job",
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_token", sa.CHAR(36), nullable=True),
        sa.Column("leased_until", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
            name=op.f("fk_grading_job_attempt_id_attempt"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_grading_job")),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index(
        op.f("ix_grading_job_available_at"), "grading_job", ["available_at"]
    )
    op.execute(
        "INSERT INTO grading_job (attempt_id, available_at, attempt_count) "
        "SELECT id, COALESCE(submitted_at, started_at), 0 FROM attempt "
        "WHERE status = 'GRADING'"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_grading_job_available_at"), table_name="grading_job")
    op.drop_table("grading_job")
    op.drop_constraint(
        op.f("fk_attempt_score_evidence_revision_id_answer_revision"),
        "attempt_score",
        type_="foreignkey",
    )
    op.drop_column("attempt_score", "evidence_revision_id")
    op.drop_constraint(
        op.f("fk_answer_revision_after_message_id_chat_message"),
        "answer_revision",
        type_="foreignkey",
    )
    op.drop_column("answer_revision", "after_message_id")
    op.drop_column("attempt", "task_snapshot")
