from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ChatRequest(Base):
    __tablename__ = "chat_request"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempt.id", ondelete="CASCADE"), primary_key=True
    )
    request_key: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin"), primary_key=True
    )
    user_message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_message.id", ondelete="CASCADE")
    )
    assistant_message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_message.id", ondelete="CASCADE")
    )
