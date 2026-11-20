from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Attempt, AttemptScore, Task
from app.models.catalog import current_catalog_lecture
from app.services.attempts import iso
from app.services.grading import calculate_result
from app.services.rubrics import CRITERIA, public_requirements


def result_info(
    session: Session, attempt: Attempt, level: int, first_view: bool
) -> dict:
    task = session.get(Task, attempt.task_id)
    lecture = current_catalog_lecture(session, task.lecture_id)
    rubric = attempt.grading_rubric_snapshot
    scores = dimensions = checks = None
    if attempt.status == "GRADED":
        rows = {
            item.criterion: item
            for item in session.scalars(
                select(AttemptScore)
                .where(AttemptScore.attempt_id == attempt.id)
                .with_for_update()
            )
        }
        scores = [
            {
                "criterion": key,
                "name": name,
                "dimension": dimension,
                "score": rows[key].score,
                "max_score": rubric["criterion_max_scores"][key],
                "feedback": rows[key].feedback,
                "evidence_message_id": rows[key].evidence_message_id,
                "evidence_revision_id": rows[key].evidence_revision_id,
            }
            for key, (name, dimension) in CRITERIA.items()
        ]
        if rubric["version"] == "DUAL_V1":
            recorded = {
                entry["check_id"]: entry for entry in attempt.required_check_results
            }
            checks = [
                {**recorded[item["check_id"]], "label": item["label"]}
                for item in rubric["required_checks"]
            ]
            _, _, dimensions = calculate_result(
                rubric, {key: row.score for key, row in rows.items()}, checks
            )
    return {
        "id": attempt.id,
        "status": attempt.status,
        "task": {
            "id": task.id,
            "title": (
                attempt.task_snapshot["title"]
                if attempt.task_snapshot is not None
                else task.title
            ),
        },
        "started_at": iso(attempt.started_at),
        "submitted_at": iso(attempt.submitted_at),
        "answer": attempt.answer,
        "content_snapshot_status": (
            "FROZEN" if attempt.task_snapshot is not None else "LEGACY_LIVE"
        ),
        "total_score": attempt.total_score,
        "passed": attempt.passed,
        "summary_feedback": attempt.summary_feedback,
        "pass_requirements": public_requirements(rubric),
        "dimensions": dimensions,
        "required_checks": checks,
        "scores": scores,
        "recommended_lecture": {"id": lecture.id, "title": lecture.title},
        "level_up": {"leveled_up": bool(attempt.leveled_up), "level": level},
        "first_view": first_view,
    }
