import json
from copy import deepcopy
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.api.dependencies import CurrentUser, Database
from app.api.schemas import (
    AttemptStarted,
    ChatLine,
    ChatTurn,
    CurrentAttempt,
    InGrading,
    ResultDetail,
    ResultNotice,
    ResultsPage,
    SavedAnswer,
)
from app.api.tasks import own_task, require_lecture
from app.errors import ApiError
from app.models import AnswerRevision, Attempt, AttemptEvent, ChatMessage, Task
from app.models.chat import ChatRequest
from app.ops import limit_attempt_action
from app.services.ai import ProviderError
from app.services.attempts import (
    GRACE,
    RESULT_STATES,
    expire_active,
    iso,
    last_revision,
    lock_owner,
    make_task_snapshot,
    own_attempt,
    queue_grading,
    require_writable,
    task_snapshot,
    transition,
)
from app.services.chat_turns import replay
from app.services.input_limits import (
    MAX_FINAL_ANSWER_BYTES,
    json_size,
    require_history_capacity,
)
from app.services.results import result_info
from app.services.rubrics import public_criteria, public_requirements

router = APIRouter(prefix="/attempts", tags=["attempts"])
MAX_CHAT_TURNS = 40
MAX_REVISIONS = 1500
MAX_EVENTS = 600


class StartAttempt(BaseModel):
    task_id: int


class Answer(BaseModel):
    content: str = Field(max_length=20000)
    expected_revision_id: Annotated[int | None, Field(gt=0)] = None


class Submission(BaseModel):
    answer: str = Field(max_length=20000)

    @field_validator("answer")
    @classmethod
    def answer_fits_model_input(cls, value: str) -> str:
        if json_size(value) > MAX_FINAL_ANSWER_BYTES:
            raise ValueError("The answer exceeds the model input budget.")
        return value


class Message(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class Event(BaseModel):
    type: Literal["PASTE", "BLUR", "FOCUS"]
    payload: dict | None = None

    @field_validator("payload")
    @classmethod
    def payload_limit(cls, payload):
        if (
            payload is not None
            and len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            )
            > 1024
        ):
            raise ValueError("payload must fit in 1024 UTF-8 bytes")
        return payload


def enqueue(request: Request, background: BackgroundTasks, attempt_id: int | None):
    worker = getattr(request.app.state, "grading_worker", None)
    if attempt_id is not None and (worker is None or not worker.enabled):
        background.add_task(request.app.state.grade_attempt, attempt_id)


def attempt_info(session, attempt: Attempt, task: Task, now):
    snapshot = task_snapshot(session, attempt)
    return {
        "id": attempt.id,
        "status": attempt.status,
        "started_at": iso(attempt.started_at),
        "deadline_at": iso(attempt.deadline_at),
        "remaining_sec": max(0, int((attempt.deadline_at - now).total_seconds())),
        "task": {
            **snapshot,
            "criteria": public_criteria(attempt.grading_rubric_snapshot),
            "pass_requirements": public_requirements(attempt.grading_rubric_snapshot),
        },
    }


@router.post("", status_code=201, response_model=AttemptStarted)
def start(
    body: StartAttempt,
    user: CurrentUser,
    session: Database,
    request: Request,
    background: BackgroundTasks,
):
    task = own_task(session, user, body.task_id)
    require_lecture(session, user.id, task)
    lock_owner(session, user.id)
    now = request.app.state.clock()
    active, expired = expire_active(session, user.id, now)
    if active is not None:
        raise ApiError("ATTEMPT_ALREADY_IN_PROGRESS")
    attempt = Attempt(
        user_id=user.id,
        task_id=task.id,
        status="IN_PROGRESS",
        started_at=now,
        deadline_at=now + timedelta(minutes=task.time_limit_min),
        grading_rubric_snapshot=deepcopy(task.grading_rubric),
        task_snapshot=make_task_snapshot(session, task),
    )
    session.add(attempt)
    session.flush()
    response = attempt_info(session, attempt, task, now)
    session.commit()
    enqueue(request, background, expired)
    return response


@router.get(
    "/current",
    response_model=CurrentAttempt | ResultNotice,
    responses={204: {"description": "No attempt"}},
)
def current(
    user: CurrentUser, session: Database, request: Request, background: BackgroundTasks
):
    lock_owner(session, user.id)
    now = request.app.state.clock()
    active, expired = expire_active(session, user.id, now)
    if active is not None:
        current_answer = last_revision(session, active.id, lock=True)
        data = {
            **attempt_info(session, active, session.get(Task, active.task_id), now),
            "answer": current_answer.content if current_answer else "",
            "answer_revision_id": current_answer.id if current_answer else None,
        }
    else:
        pending = session.scalar(
            select(Attempt)
            .where(
                Attempt.user_id == user.id,
                Attempt.result_seen_at.is_(None),
                Attempt.status.in_(RESULT_STATES),
            )
            .order_by(Attempt.submitted_at.desc(), Attempt.id.desc())
            .limit(1)
            .with_for_update()
        )
        data = (
            {
                "id": pending.id,
                "status": pending.status,
                "auto_submitted": pending.submitted_at > pending.deadline_at + GRACE,
            }
            if pending
            else Response(status_code=204)
        )
    session.commit()
    enqueue(request, background, expired)
    return data


@router.delete("/{attempt_id}", status_code=204)
def abandon(attempt_id: int, user: CurrentUser, session: Database, request: Request):
    attempt = own_attempt(session, user.id, attempt_id, lock=True)
    require_writable(attempt, request.app.state.clock())
    session.delete(attempt)
    session.commit()


def serialized_message(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "created_at": iso(message.created_at),
    }


def messages_for(session, attempt_id):
    return session.scalars(
        select(ChatMessage)
        .where(ChatMessage.attempt_id == attempt_id)
        .order_by(ChatMessage.id)
    ).all()


@router.get("/{attempt_id}/messages", response_model=list[ChatLine])
def messages(attempt_id: int, user: CurrentUser, session: Database):
    own_attempt(session, user.id, attempt_id)
    return [serialized_message(item) for item in messages_for(session, attempt_id)]


@router.post("/{attempt_id}/messages", status_code=201, response_model=ChatTurn)
def chat(
    attempt_id: int,
    body: Message,
    user: CurrentUser,
    session: Database,
    request: Request,
    request_key: Annotated[
        str | None, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    ] = None,
):
    user_id = user.id
    attempt = own_attempt(session, user_id, attempt_id)
    limit_attempt_action(request, "messages", user_id)
    with request.app.state.chat_slots.reserve(attempt_id):
        if request_key is not None:
            previous = replay(session, attempt_id, request_key, body.content)
            if previous is not None:
                return chat_response(*previous)
        return record_chat(session, request, attempt, user_id, body.content, request_key)


def chat_response(asked: ChatMessage, answered: ChatMessage) -> dict:
    return {
        "user_message": serialized_message(asked),
        "assistant_message": serialized_message(answered),
    }


def record_chat(session, request, attempt, user_id, content, request_key):
    attempt_id = attempt.id
    clock = request.app.state.clock
    require_writable(attempt, clock())
    previous = messages_for(session, attempt_id)
    if len(previous) >= MAX_CHAT_TURNS * 2:
        raise ApiError("ATTEMPT_INPUT_LIMIT")
    require_history_capacity(session, attempt_id, content)
    history = [
        {
            "role": "system",
            "content": "실무 과정의 AI 도우미로서 사용자의 업무 질문에 답한다.",
        }
    ]
    history.extend(
        {"role": item.role.lower(), "content": item.content}
        for item in previous
    )
    history.append({"role": "user", "content": content})
    session.rollback()
    try:
        reply = request.app.state.provider.chat(history)
    except ProviderError as error:
        raise ApiError("AI_UNAVAILABLE") from error
    if not isinstance(reply, str) or not reply.strip() or len(reply.encode()) > 65535:
        raise ApiError("AI_UNAVAILABLE")
    attempt = own_attempt(session, user_id, attempt_id, lock=True)
    now = clock()
    require_writable(attempt, now)
    require_history_capacity(session, attempt_id, content, reply)
    asked = ChatMessage(
        attempt_id=attempt_id, role="USER", content=content, created_at=now
    )
    answered = ChatMessage(
        attempt_id=attempt_id, role="ASSISTANT", content=reply, created_at=now
    )
    session.add_all((asked, answered))
    session.flush()
    if request_key is not None:
        session.add(ChatRequest(
            attempt_id=attempt_id, request_key=request_key,
            user_message_id=asked.id, assistant_message_id=answered.id,
        ))
    data = chat_response(asked, answered)
    session.commit()
    return data


@router.put("/{attempt_id}/answer", response_model=SavedAnswer)
def save_answer(
    attempt_id: int,
    body: Answer,
    user: CurrentUser,
    session: Database,
    request: Request,
):
    attempt = own_attempt(session, user.id, attempt_id, lock=True)
    now = request.app.state.clock()
    require_writable(attempt, now)
    previous = last_revision(session, attempt_id, lock=True)
    if previous is not None and previous.content == body.content:
        revision = previous
    else:
        if (
            "expected_revision_id" in body.model_fields_set
            and body.expected_revision_id != (previous.id if previous else None)
        ):
            raise ApiError("ANSWER_REVISION_CONFLICT")
        if session.scalar(
            select(func.count()).select_from(AnswerRevision)
            .where(AnswerRevision.attempt_id == attempt_id)
        ) >= MAX_REVISIONS:
            raise ApiError("ATTEMPT_INPUT_LIMIT")
        require_history_capacity(session, attempt_id, body.content)
        after_message_id = session.scalar(
            select(ChatMessage.id)
            .where(
                ChatMessage.attempt_id == attempt_id,
                ChatMessage.role == "ASSISTANT",
            )
            .order_by(ChatMessage.id.desc())
            .limit(1)
        )
        revision = AnswerRevision(
            attempt_id=attempt_id, content=body.content, created_at=now,
            after_message_id=after_message_id,
        )
        session.add(revision)
        session.flush()
    data = {"saved_at": iso(revision.created_at), "revision_id": revision.id}
    session.commit()
    return data


@router.post("/{attempt_id}/events", status_code=204)
def log_event(
    attempt_id: int, body: Event, user: CurrentUser, session: Database, request: Request
):
    attempt = own_attempt(session, user.id, attempt_id, lock=True)
    now = request.app.state.clock()
    require_writable(attempt, now)
    if session.scalar(
        select(func.count()).select_from(AttemptEvent).where(AttemptEvent.attempt_id == attempt_id)
    ) >= MAX_EVENTS:
        raise ApiError("ATTEMPT_INPUT_LIMIT")
    session.add(
        AttemptEvent(
            attempt_id=attempt_id, type=body.type, payload=body.payload, created_at=now
        )
    )
    session.commit()


@router.post("/{attempt_id}/submit", status_code=202, response_model=InGrading)
def submit(
    attempt_id: int,
    body: Submission,
    user: CurrentUser,
    session: Database,
    request: Request,
    background: BackgroundTasks,
):
    attempt = own_attempt(session, user.id, attempt_id, lock=True)
    if not transition(session, attempt, body.answer, request.app.state.clock()):
        raise ApiError("ATTEMPT_NOT_IN_PROGRESS")
    session.commit()
    enqueue(request, background, attempt_id)
    return {"id": attempt_id, "status": "GRADING"}


@router.post("/{attempt_id}/regrade", status_code=202, response_model=InGrading)
def regrade(
    attempt_id: int,
    user: CurrentUser,
    session: Database,
    request: Request,
    background: BackgroundTasks,
):
    attempt = own_attempt(session, user.id, attempt_id, lock=True)
    limit_attempt_action(request, "regrade", user.id)
    if attempt.status != "GRADING_FAILED":
        raise ApiError("ATTEMPT_NOT_REGRADABLE")
    attempt.status = "GRADING"
    attempt.grading_failure = None
    queue_grading(session, attempt.id, request.app.state.clock())
    session.commit()
    enqueue(request, background, attempt_id)
    return {"id": attempt_id, "status": "GRADING"}


@router.get("", response_model=ResultsPage)
def previous(
    user: CurrentUser,
    session: Database,
    request: Request,
    background: BackgroundTasks,
    task_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    lock_owner(session, user.id)
    _, expired = expire_active(session, user.id, request.app.state.clock())
    filters = [Attempt.user_id == user.id, Attempt.status.in_(RESULT_STATES)]
    if task_id is not None:
        filters.append(Attempt.task_id == task_id)
    total = session.scalar(select(func.count()).select_from(Attempt).where(*filters))
    rows = session.scalars(
        select(Attempt)
        .where(*filters)
        .order_by(Attempt.submitted_at.desc(), Attempt.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = [
        {
            "id": attempt.id,
            "task": {
                "id": attempt.task_id,
                "title": task_snapshot(session, attempt)["title"],
            },
            "status": attempt.status,
            "submitted_at": iso(attempt.submitted_at),
            "total_score": attempt.total_score,
            "passed": attempt.passed,
        }
        for attempt in rows
    ]
    session.commit()
    enqueue(request, background, expired)
    return {"items": items, "page": page, "size": size, "total": total}


@router.get("/{attempt_id}", response_model=ResultDetail)
def result(
    attempt_id: int,
    user: CurrentUser,
    session: Database,
    request: Request,
    background: BackgroundTasks,
):
    own_attempt(session, user.id, attempt_id)
    lock_owner(session, user.id)
    now = request.app.state.clock()
    _, expired = expire_active(session, user.id, now)
    attempt = own_attempt(session, user.id, attempt_id, lock=True)
    first = attempt.status == "GRADED" and attempt.result_seen_at is None
    if first:
        attempt.result_seen_at = now
    session.refresh(user, with_for_update=True)
    data = result_info(session, attempt, user.level, first)
    session.commit()
    enqueue(request, background, expired)
    return data
