from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.lectures import own_lecture
from app.api.tasks import own_task
from app.api.users import passed_count
from app.errors import ApiError
from app.models import Attempt, Job, Lecture, Task, TaskMaterial, User
from app.models.catalog import conceptual_pass_count, lecture_is_listed
from app.services.grade_output import GradeOutput
from app.services.grading import save_grade
from app.services.results import result_info
from scripts.publish_catalog import Catalog, PublishConflict, publish_catalog


@pytest.fixture
def manifest():
    return {
        "job": "카탈로그 검증 직무",
        "key": "test-business-email",
        "version": 1,
        "lecture": {
            "title": "검수 자료",
            "youtube_video_id": "AbCdEf012_-",
            "duration_sec": 1000,
            "sort_order": 1,
        },
        "task": {
            "title": "검수 시험",
            "situation": "상황 자료에 따릅니다.",
            "instruction": "답안을 제출합니다.",
            "time_limit_min": 30,
            "materials": [{"title": "자료", "content": "내용"}],
            "rubric": {
                "version": "DUAL_V1",
                "task_solution_min_score": 40,
                "ai_use_min_score": 40,
                "verify_min_score": 12,
                "criterion_max_scores": {"RESULT": 50, "PROMPT": 15, "VERIFY": 20, "IMPROVE": 15},
                "required_checks": [{"check_id": "KEY_FACT", "label": "핵심 요건", "rule": "사실에 맞는 답안"}],
            },
        },
    }


@pytest.mark.parametrize("video,duration", [("mock:demo", 600), ("AbCdEf012_-", 0)])
def test_catalog_validates_video_shape_and_duration(manifest, video, duration):
    manifest["lecture"].update(youtube_video_id=video, duration_sec=duration)
    with pytest.raises(ValidationError):
        Catalog.model_validate(manifest)


def test_catalog_validates_rubric_and_required_check_ids(manifest):
    duplicate = deepcopy(manifest)
    checks = duplicate["task"]["rubric"]["required_checks"]
    checks.append(checks[0])
    with pytest.raises(ValidationError):
        Catalog.model_validate(duplicate)

    manifest["task"]["rubric"]["verify_min_score"] = 25
    with pytest.raises(ValidationError):
        Catalog.model_validate(manifest)


def test_catalog_rejects_aggregate_material_above_model_input_budget(manifest):
    manifest["task"]["materials"] = [
        {"title": str(index), "content": "자료" * 3000}
        for index in range(3)
    ]
    with pytest.raises(ValidationError, match="model input budget"):
        Catalog.model_validate(manifest)


def test_catalog_publish_is_immutable_and_orders_materials(api_engine, manifest):
    with Session(api_engine) as session:
        release = publish_catalog(session, Catalog.model_validate(manifest))
        assert publish_catalog(session, Catalog.model_validate(manifest)) == release
        first_lecture = session.get(Lecture, release.lecture_id)
        task = session.get(Task, release.task_id)
        assert task.job_id == first_lecture.job_id
        assert [row.sort_order for row in session.scalars(
            select(TaskMaterial).where(TaskMaterial.task_id == task.id)
        )] == [1]

        manifest["lecture"]["duration_sec"] = 900
        with pytest.raises(PublishConflict):
            publish_catalog(session, Catalog.model_validate(manifest))

        manifest["version"] = 2
        updated = publish_catalog(session, Catalog.model_validate(manifest))
        assert updated.lecture_id != release.lecture_id
        assert updated.task_id != release.task_id
        assert first_lecture.duration_sec == 1000
        assert session.get(Lecture, updated.lecture_id).duration_sec == 900
        assert session.scalars(
            select(Lecture.id).where(Lecture.job_id == task.job_id, lecture_is_listed(Lecture.id))
        ).all() == [updated.lecture_id]
        assert own_lecture(session, task.job_id, release.lecture_id).id == release.lecture_id
        with pytest.raises(ApiError, match="LECTURE_NOT_FOUND"):
            own_lecture(session, task.job_id, release.lecture_id, listed_only=True)
        with pytest.raises(ApiError, match="TASK_NOT_FOUND"):
            own_task(session, User(job_id=task.job_id), release.task_id)


def _unique_catalog(manifest):
    catalog = deepcopy(manifest)
    suffix = uuid4().hex[:12]
    catalog["job"] = f"카탈로그 직무 {suffix}"
    catalog["key"] = f"catalog-{suffix}"
    return catalog


def _attempt(user_id, task_id, rubric, *, status="GRADED", passed=True):
    now = datetime(2026, 11, 20, 3)
    return Attempt(
        user_id=user_id,
        task_id=task_id,
        status=status,
        started_at=now,
        deadline_at=now + timedelta(minutes=30),
        submitted_at=now,
        answer="검증된 답안",
        grading_rubric_snapshot=deepcopy(rubric),
        task_snapshot=None,
        required_check_results=None,
        total_score=80 if passed else 0,
        passed=passed,
        leveled_up=False,
        summary_feedback="평가 완료",
        result_seen_at=None,
    )


def _passing_output():
    return GradeOutput.model_validate({
        "scores": [
            {"criterion": "RESULT", "score": 40, "feedback": "충족"},
            {"criterion": "PROMPT", "score": 14, "feedback": "충족"},
            {"criterion": "VERIFY", "score": 12, "feedback": "충족"},
            {"criterion": "IMPROVE", "score": 14, "feedback": "충족"},
        ],
        "required_checks": [
            {"check_id": "KEY_FACT", "met": True, "feedback": "충족"}
        ],
        "summary_feedback": "통과",
    })


def test_catalog_versions_count_as_one_passed_task_for_profile_and_level(
    api_engine, manifest
):
    catalog = _unique_catalog(manifest)
    with Session(api_engine) as session:
        first = publish_catalog(session, Catalog.model_validate(catalog))
        catalog["version"] = 2
        second = publish_catalog(session, Catalog.model_validate(catalog))
        job_id = session.scalar(select(Job.id).where(Job.name == catalog["job"]))
        user = User(
            login_id=f"catalog_{uuid4().hex[:12]}",
            password_hash="not-used",
            nickname="검증자",
            job_id=job_id,
            level=4,
            tutorial_seen=True,
            created_at=datetime(2026, 11, 20, 3),
        )
        uncataloged_lecture = Lecture(
            job_id=job_id,
            title="직접 등록 강의",
            youtube_video_id="AbCdEf012_-",
            duration_sec=600,
            sort_order=99,
        )
        session.add_all([user, uncataloged_lecture])
        session.flush()
        uncataloged_task = Task(
            job_id=job_id,
            lecture_id=uncataloged_lecture.id,
            title="직접 등록 과제",
            situation="상황",
            instruction="지시",
            time_limit_min=30,
            sort_order=99,
            grading_rubric=deepcopy(catalog["task"]["rubric"]),
        )
        session.add(uncataloged_task)
        session.flush()
        session.add_all([
            _attempt(user.id, first.task_id, catalog["task"]["rubric"]),
            _attempt(user.id, uncataloged_task.id, catalog["task"]["rubric"]),
        ])
        duplicate_version = _attempt(
            user.id,
            second.task_id,
            catalog["task"]["rubric"],
            status="GRADING",
            passed=None,
        )
        session.add(duplicate_version)
        session.flush()

        level_change = save_grade(
            session, user.id, duplicate_version.id, _passing_output()
        )

        assert conceptual_pass_count(session, user.id) == 2
        assert passed_count(session, user.id) == 2
        assert user.level == 4
        assert level_change is None

        standalone_attempt = _attempt(
            user.id,
            uncataloged_task.id,
            catalog["task"]["rubric"],
            status="GRADING",
            passed=None,
        )
        session.add(standalone_attempt)
        session.flush()
        assert result_info(session, standalone_attempt, user.level, False)[
            "recommended_lecture"
        ] == {
            "id": uncataloged_lecture.id,
            "title": "직접 등록 강의",
        }
        session.rollback()


def test_old_catalog_result_recommends_active_release_but_keeps_assessed_snapshot(
    api_engine, manifest
):
    catalog = _unique_catalog(manifest)
    with Session(api_engine) as session:
        first = publish_catalog(session, Catalog.model_validate(catalog))
        catalog["version"] = 2
        catalog["lecture"]["title"] = "현재 추천 강의"
        second = publish_catalog(session, Catalog.model_validate(catalog))
        old_task = session.get(Task, first.task_id)
        user = User(
            login_id=f"catalog_{uuid4().hex[:12]}",
            password_hash="not-used",
            nickname="검증자",
            job_id=old_task.job_id,
            level=1,
            tutorial_seen=True,
            created_at=datetime(2026, 11, 20, 3),
        )
        session.add(user)
        session.flush()
        attempt = _attempt(
            user.id,
            old_task.id,
            catalog["task"]["rubric"],
            status="GRADING",
            passed=None,
        )
        attempt.task_snapshot = {
            "title": "응시 당시 과제",
            "situation": "당시 상황",
            "instruction": "당시 지시",
            "materials": [],
        }
        session.add(attempt)
        session.flush()

        result = result_info(session, attempt, user.level, False)

        assert result["task"]["title"] == "응시 당시 과제"
        assert result["recommended_lecture"] == {
            "id": second.lecture_id,
            "title": "현재 추천 강의",
        }
        session.rollback()
