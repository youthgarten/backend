import logging

from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import (
    AnswerRevision,
    Attempt,
    AttemptEvent,
    AttemptScore,
    ChatMessage,
    User,
)
from app.models.catalog import conceptual_pass_count
from app.services.ai import (
    AIProvider,
    ProviderError,
    ProviderInputError,
    validate_verification_reference,
)
from app.services.attempts import iso, lock_owner, task_snapshot
from app.services.grade_output import GradeOutput
from app.services.grading_jobs import claim, owns_lease, retry_or_fail
from app.services.rubrics import CRITERIA

logger = logging.getLogger(__name__)
PROMPT_VERSION = "dual-v2-evidence-order"


def grade_context(session: Session, attempt: Attempt) -> dict:
    snapshot = task_snapshot(session, attempt)
    chat = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.attempt_id == attempt.id)
        .order_by(ChatMessage.id)
    )
    revisions = session.scalars(
        select(AnswerRevision)
        .where(AnswerRevision.attempt_id == attempt.id)
        .order_by(AnswerRevision.id)
    )
    events = session.scalars(
        select(AttemptEvent)
        .where(AttemptEvent.attempt_id == attempt.id)
        .order_by(AttemptEvent.id)
    )
    return {
        "rubric": attempt.grading_rubric_snapshot,
        "task": {
            "title": snapshot["title"],
            "situation": snapshot["situation"],
            "instruction": snapshot["instruction"],
        },
        "materials": snapshot["materials"],
        "answer": attempt.answer or "",
        "messages": [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "created_at": iso(row.created_at),
            }
            for row in chat
        ],
        "revisions": [
            {
                "id": row.id,
                "content": row.content,
                "created_at": iso(row.created_at),
                "after_message_id": row.after_message_id,
            }
            for row in revisions
        ],
        "events": [
            {
                "type": row.type,
                "payload": row.payload,
                "created_at": iso(row.created_at),
            }
            for row in events
        ],
    }


def validate_grade_output(context: dict, raw: dict) -> GradeOutput:
    output = GradeOutput.model_validate(raw)
    rubric = context["rubric"]
    maxima = rubric["criterion_max_scores"]
    checks = {item["check_id"] for item in rubric.get("required_checks", [])}
    dual = rubric["version"] == "DUAL_V1"
    if {item.criterion for item in output.scores} != set(CRITERIA) or len(
        output.scores
    ) != len(CRITERIA):
        raise ValueError("Missing, duplicate, or unknown scoring criteria.")
    if {item.check_id for item in output.required_checks} != checks or len(
        output.required_checks
    ) != len(checks):
        raise ValueError("Invalid required checks.")
    messages = {item["id"] for item in context["messages"]}
    revisions = {item["id"]: item["content"] for item in context["revisions"]}
    for score in output.scores:
        if not 0 <= score.score <= maxima[score.criterion]:
            raise ValueError("Score is outside its rubric range.")
        if (
            score.evidence_message_id is not None
            and score.evidence_message_id not in messages
        ):
            raise ValueError("Evidence message is outside the attempt.")
        if (
            score.evidence_revision_id is not None
            and score.evidence_revision_id not in revisions
        ):
            raise ValueError("Evidence revision is outside the attempt.")
        if (
            dual
            and score.criterion == "VERIFY"
            and score.score > 0
            and not validate_verification_reference(
                context, score.evidence_message_id, score.evidence_revision_id
            )
        ):
            raise ValueError("A positive verification score requires evidence.")
        if score.evidence_revision_id is not None:
            content = revisions[score.evidence_revision_id]
            excerpt = content[:240] + ("…" if len(content) > 240 else "")
            score.feedback += (
                f"\n답안 수정 기록 #{score.evidence_revision_id}: {excerpt}"
            )
    if not context["answer"].strip():
        for score in output.scores:
            if score.criterion == "RESULT":
                score.score = 0
                score.feedback = "최종 답안이 비어 있어 결과물을 평가할 수 없어요."
        for item in output.required_checks:
            item.met = False
            item.feedback = "최종 답안에서 필수 요건 충족을 확인할 수 없어요."
    return output


def calculate_result(
    rubric: dict, scores: dict[str, int], checks: list[dict]
) -> tuple[int, bool, list[dict] | None]:
    total = sum(scores.values())
    if rubric["version"] == "LEGACY_TOTAL_V1":
        return total, total >= rubric["min_total_score"], None
    maximum = rubric["criterion_max_scores"]
    result_min = rubric["task_solution_min_score"]
    ai_min = rubric["ai_use_min_score"]
    ai_points = scores["PROMPT"] + scores["VERIFY"] + scores["IMPROVE"]
    result_met = scores["RESULT"] >= result_min and all(
        check["met"] for check in checks
    )
    ai_met = ai_points >= ai_min and scores["VERIFY"] >= rubric["verify_min_score"]
    dimensions = [
        {
            "code": "TASK_SOLUTION",
            "name": "실무 과제 해결",
            "score": scores["RESULT"],
            "max_score": maximum["RESULT"],
            "min_score": result_min,
            "passed": result_met,
        },
        {
            "code": "AI_USE",
            "name": "AI 활용 역량",
            "score": ai_points,
            "max_score": sum(maximum[key] for key in ("PROMPT", "VERIFY", "IMPROVE")),
            "min_score": ai_min,
            "passed": ai_met,
        },
    ]
    return total, result_met and ai_met, dimensions


def request_grade(provider: AIProvider, context: dict) -> GradeOutput:
    for retry in range(2):
        try:
            return validate_grade_output(context, provider.grade(context))
        except ValueError:
            if retry == 1:
                raise
    raise AssertionError("Unreachable grading retry.")


def save_grade(
    session: Session, owner: int, attempt_id: int, output: GradeOutput
) -> int | None:
    lock_owner(session, owner)
    attempt = session.scalar(
        select(Attempt).where(Attempt.id == attempt_id).with_for_update()
    )
    if attempt is None or attempt.status != "GRADING":
        return None
    user = session.get(User, owner, with_for_update=True)
    rubric = attempt.grading_rubric_snapshot
    checks = [check.model_dump() for check in output.required_checks]
    total, passed, _ = calculate_result(
        rubric, {score.criterion: score.score for score in output.scores}, checks
    )
    session.add_all(
        AttemptScore(
            attempt_id=attempt_id,
            criterion=entry.criterion,
            score=entry.score,
            feedback=entry.feedback,
            evidence_message_id=entry.evidence_message_id,
            evidence_revision_id=entry.evidence_revision_id,
        )
        for entry in output.scores
    )
    attempt.required_check_results = checks if rubric["version"] == "DUAL_V1" else None
    attempt.total_score, attempt.passed, attempt.summary_feedback = (
        total,
        passed,
        output.summary_feedback,
    )
    attempt.status = "GRADED"
    session.flush()
    count = conceptual_pass_count(session, owner)
    earned_level = min(1 + count, 6)
    attempt.leveled_up = earned_level > user.level
    user.level = max(user.level, earned_level)
    return user.level if attempt.leveled_up else None


def finish_grading(
    factory, provider: AIProvider, attempt_id: int
) -> tuple[int, int] | None:
    try:
        with factory() as session:
            attempt = session.get(Attempt, attempt_id)
            if attempt is None or attempt.status != "GRADING":
                return None
            owner = attempt.user_id
            context = grade_context(session, attempt)
        output = request_grade(provider, context)
        with factory.begin() as session:
            level = save_grade(session, owner, attempt_id, output)
        return (owner, level) if level is not None else None
    except (ProviderError, ValueError) as error:
        logger.warning(
            "Grading failed for attempt %s: %s", attempt_id, type(error).__name__
        )
    except Exception as error:  # noqa: BLE001 - every failure must leave GRADING
        logger.error(
            "Grading could not be saved for attempt %s: %s",
            attempt_id,
            type(error).__name__,
        )
    with factory.begin() as session:
        session.execute(
            update(Attempt)
            .where(Attempt.id == attempt_id, Attempt.status == "GRADING")
            .values(status="GRADING_FAILED")
        )
    return None


async def grade_attempt(application: FastAPI, attempt_id: int) -> None:
    state = application.state
    level_change = await run_in_threadpool(
        finish_queued_grading,
        state.session_factory,
        state.provider,
        attempt_id,
        state.clock,
    )
    if level_change is not None:
        async with state.presence.join_lock:
            state.presence.update_level(*level_change)


def finish_queued_grading(
    factory, provider: AIProvider, attempt_id: int, clock
) -> tuple[int, int] | None:
    now = clock()
    token = claim(factory, attempt_id, now)
    if token is None:
        return None
    try:
        model = getattr(provider, "model", None) or type(provider).__name__
        with factory.begin() as session:
            attempt = session.get(Attempt, attempt_id, with_for_update=True)
            if attempt is not None:
                attempt.grading_model = model
                attempt.grading_prompt_version = PROMPT_VERSION
                attempt.grading_failure = None
        with factory() as session:
            attempt = session.get(Attempt, attempt_id)
            if attempt is None or attempt.status != "GRADING":
                return None
            owner = attempt.user_id
            context = grade_context(session, attempt)
        output = request_grade(provider, context)
        with factory.begin() as session:
            job = owns_lease(session, attempt_id, token, clock())
            if job is None:
                return None
            level = save_grade(session, owner, attempt_id, output)
            attempt = session.get(Attempt, attempt_id)
            attempt.grading_model = model
            attempt.grading_prompt_version = PROMPT_VERSION
            attempt.grading_failure = None
            session.delete(job)
        return (owner, level) if level is not None else None
    except ProviderInputError as error:
        logger.warning("Grading provider rejected input for attempt %s", attempt_id)
        retry_or_fail(
            factory, attempt_id, token, clock(), type(error).__name__, retry=False
        )
    except (ProviderError, SQLAlchemyError) as error:
        logger.warning("Transient grading failure for attempt %s", attempt_id)
        retry_or_fail(
            factory,
            attempt_id,
            token,
            clock(),
            type(error).__name__,
            retry=True,
        )
    except ValueError as error:
        logger.warning("Grading output failed for attempt %s", attempt_id)
        retry_or_fail(
            factory,
            attempt_id,
            token,
            clock(),
            type(error).__name__,
            retry=False,
        )
    except Exception as error:  # noqa: BLE001 - durable state must be released
        logger.error("Grading save failed for attempt %s", attempt_id)
        retry_or_fail(
            factory,
            attempt_id,
            token,
            clock(),
            type(error).__name__,
            retry=False,
        )
    return None
