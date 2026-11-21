"""Persist completed idempotent AI requests with their two messages."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_request",
        sa.Column("attempt_id", sa.BigInteger(), nullable=False),
        sa.Column("request_key", sa.String(64, collation="utf8mb4_bin"), nullable=False),
        sa.Column("user_message_id", sa.BigInteger(), nullable=False),
        sa.Column("assistant_message_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["attempt.id"], name="fk_chat_request_attempt_id_attempt",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_message_id"], ["chat_message.id"],
            name="fk_chat_request_user_message_id_chat_message", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"], ["chat_message.id"],
            name="fk_chat_request_assistant_message_id_chat_message", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id", "request_key", name="pk_chat_request"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("chat_request")
