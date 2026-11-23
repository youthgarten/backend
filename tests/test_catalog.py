import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from support import bearer, report, watch_to

from app.api.lectures import own_lecture
from app.api.tasks import own_task
from app.api.users import passed_count
from app.errors import ApiError
from app.models import Attempt, Job, Lecture, LectureProgress, Task, TaskMaterial, User
from app.models.catalog import CatalogRelease, conceptual_pass_count, lecture_is_listed
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


@pytest.mark.parametrize("video,duration", [
    ("mock:demo", 600), ("AbCdEf012_-", 0), ("local:../secret", 697),
    ("local:" + "a" * 15, 697),
])
def test_catalog_validates_video_shape_and_duration(manifest, video, duration):
    manifest["lecture"].update(youtube_video_id=video, duration_sec=duration)
    with pytest.raises(ValidationError):
        Catalog.model_validate(manifest)


def test_catalog_accepts_a_bundled_mp4_key(manifest):
    manifest["lecture"].update(youtube_video_id="local:trade-delay-v1", duration_sec=697)
    assert Catalog.model_validate(manifest).lecture.youtube_video_id == "local:trade-delay-v1"


def test_catalog_reuses_unstarted_lecture_only_when_the_assessment_matches(api_engine, manifest):
    manifest["job"] += uuid4().hex[:8]
    manifest["lecture"].update(youtube_video_id="local:trade-delay-v1", duration_sec=697)
    catalog = Catalog.model_validate(manifest)
    with Session(api_engine) as session:
        job = Job(name=catalog.job)
        session.add(job)
        session.flush()
        lecture = Lecture(job_id=job.id, title="임시 강의", youtube_video_id="mock:demo", duration_sec=600, sort_order=1)
        session.add(lecture)
        session.flush()
        task = Task(
            job_id=job.id, lecture_id=lecture.id, title=catalog.task.title,
            situation=catalog.task.situation, instruction=catalog.task.instruction,
            time_limit_min=catalog.task.time_limit_min, grading_rubric=catalog.task.rubric.model_dump(), sort_order=1,
        )
        session.add(task)
        session.flush()
        material = TaskMaterial(task_id=task.id, title="자료", content="임시 내용", sort_order=1)
        session.add(material)
        session.flush()

        with pytest.raises(PublishConflict, match="assessment"):
            publish_catalog(session, catalog, adopt_lecture_id=lecture.id)
        material.content = "내용"
        user = User(
            job_id=job.id, login_id=f"adopt_{uuid4().hex[:12]}", password_hash="fixture",
            nickname="검수", created_at=datetime(2026, 11, 20, 3),
        )
        session.add(user)
        session.flush()
        progress = LectureProgress(
            user_id=user.id, lecture_id=lecture.id, watched_ranges=[[0, 1]], watched_sec=1,
            last_position_sec=1, updated_at=user.created_at,
        )
        session.add(progress)
        session.flush()
        with pytest.raises(PublishConflict, match="learner activity"):
            publish_catalog(session, catalog, adopt_lecture_id=lecture.id)
        session.delete(progress)
        session.flush()
        attempt = _attempt(user.id, task.id, catalog.task.rubric.model_dump())
        session.add(attempt)
        session.flush()
        with pytest.raises(PublishConflict, match="learner activity"):
            publish_catalog(session, catalog, adopt_lecture_id=lecture.id)
        session.delete(attempt)
        session.flush()
        release = publish_catalog(session, catalog, adopt_lecture_id=lecture.id)
        assert (release.lecture_id, release.task_id) == (lecture.id, task.id)
        assert lecture.youtube_video_id == "local:trade-delay-v1"
        assert lecture.duration_sec == 697
        assert publish_catalog(session, catalog, adopt_lecture_id=lecture.id) == release


@pytest.fixture
def trade_video(api_engine):
    manifest_path = Path(__file__).resolve().parents[1] / "docs/catalog.trade-delay.json"
    manifest = _unique_catalog(json.loads(manifest_path.read_text(encoding="utf-8")))
    catalog = Catalog.model_validate(manifest)
    with Session(api_engine) as session, session.begin():
        published = publish_catalog(session, catalog)
        job_id = session.scalar(select(Job.id).where(Job.name == catalog.job))
    yield catalog, published
    with Session(api_engine) as session, session.begin():
        session.execute(delete(TaskMaterial).where(TaskMaterial.task_id == published.task_id))
        session.execute(delete(Task).where(Task.id == published.task_id))
        session.execute(delete(CatalogRelease).where(CatalogRelease.lecture_id == published.lecture_id))
        session.execute(delete(Lecture).where(Lecture.id == published.lecture_id))
        session.execute(delete(Job).where(Job.id == job_id))


def test_trade_video_unlocks_its_assessment_at_558_unique_seconds(trade_video, client, clock, register):
    catalog, published = trade_video
    _, _, login = register(job_name=catalog.job)
    headers = bearer(login)
    lecture_id, task_id = published.lecture_id, published.task_id
    detail = client.get(f"/api/v1/lectures/{lecture_id}", headers=headers).json()
    assert detail["youtube_video_id"] == "local:trade-delay-v1"
    assert detail["task_id"] == task_id and detail["duration_sec"] == 697

    jumped = report(client, headers, lecture_id, [], 680)
    assert jumped.json()["progress_rate"] == 0
    below = watch_to(client, headers, clock, lecture_id, 557)
    assert below == {"progress_rate": 0.79, "completed": False, "just_completed": False}
    locked = client.post("/api/v1/attempts", headers=headers, json={"task_id": task_id})
    assert (locked.status_code, locked.json()["error"]["code"]) == (403, "LECTURE_NOT_COMPLETED")

    clock.advance(seconds=1)
    progress = report(client, headers, lecture_id, [[557, 558]], 558)
    assert progress.json() == {"progress_rate": 0.8, "completed": True, "just_completed": True}
    started = client.post("/api/v1/attempts", headers=headers, json={"task_id": task_id})
    assert started.status_code == 201
    assert started.json()["task"]["id"] == task_id


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
