import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import AnswerRevision, ChatMessage

MAX_CATALOG_BYTES = 32 * 1024
MAX_HISTORY_BYTES = 64 * 1024
MAX_FINAL_ANSWER_BYTES = 96 * 1024
RECORD_OVERHEAD = 160


def json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def require_history_capacity(session: Session, attempt_id: int, *contents: str) -> None:
    used = sum(json_size(text) + RECORD_OVERHEAD for text in contents)
    for model in (ChatMessage, AnswerRevision):
        for text in session.scalars(select(model.content).where(model.attempt_id == attempt_id)):
            used += json_size(text) + RECORD_OVERHEAD
            if used > MAX_HISTORY_BYTES:
                raise ApiError("ATTEMPT_INPUT_LIMIT")
    if used > MAX_HISTORY_BYTES:
        raise ApiError("ATTEMPT_INPUT_LIMIT")
