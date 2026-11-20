from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import ChatMessage
from app.models.chat import ChatRequest


class ChatSlots:
    """Keep one turn in flight per attempt in the single API process."""

    def __init__(self):
        self.active: set[int] = set()
        self.lock = Lock()

    @contextmanager
    def reserve(self, attempt_id: int) -> Iterator[None]:
        with self.lock:
            if attempt_id in self.active:
                raise ApiError("CHAT_IN_PROGRESS")
            if len(self.active) >= 4:
                raise ApiError("AI_BUSY")
            self.active.add(attempt_id)
        try:
            yield
        finally:
            with self.lock:
                self.active.remove(attempt_id)


def replay(
    session: Session, attempt_id: int, key: str, content: str
) -> tuple[ChatMessage, ChatMessage] | None:
    turn = session.get(ChatRequest, (attempt_id, key))
    if turn is None:
        return None
    asked = session.get(ChatMessage, turn.user_message_id)
    answered = session.get(ChatMessage, turn.assistant_message_id)
    if asked.content != content:
        raise ApiError("IDEMPOTENCY_CONFLICT")
    return asked, answered
