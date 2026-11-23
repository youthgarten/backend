import json
from pathlib import Path
from typing import Annotated

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import utc_now
from app.db import database_url, get_session, make_engine
from app.main import create_app
from app.models import (
    AnswerRevision,
    Attempt,
    AttemptEvent,
    AttemptScore,
    ChatMessage,
    Job,
    Lecture,
    LectureProgress,
    Task,
    TaskMaterial,
    User,
)
from app.models.catalog import CatalogRelease
from scripts.publish_catalog import Catalog, publish_catalog
from scripts.seed import JOB_NAME, TASK_TITLE, TRADE_MANIFEST, seed, seed_demo

ROOT = Path(__file__).resolve().parents[1]


def test_default_seed_publishes_the_youtube_trade_lesson(engine):
    with Session(engine) as session:
        seed_demo(session)
        job = session.scalar(select(Job).where(Job.name == JOB_NAME))
        original = session.scalars(select(Lecture).where(Lecture.job_id == job.id)).one()
        seed(session)
        seed(session)
        release = session.scalars(select(CatalogRelease).where(
            CatalogRelease.job_id == job.id,
            CatalogRelease.catalog_key == "shipping-delay-reply",
            CatalogRelease.active.is_(True),
        )).one()
        lecture = session.get(Lecture, release.lecture_id)
        task = session.scalars(select(Task).where(Task.lecture_id == lecture.id)).one()
        assert (release.version, lecture.youtube_video_id, lecture.duration_sec) == (2, "c9pBqlOSnx4", 697)
        assert lecture.id == original.id
        assert task.title == TASK_TITLE


def test_seed_preserves_started_legacy_lecture_and_lists_only_v2(engine):
    with Session(engine) as session:
        seed_demo(session)
        job = session.scalar(select(Job).where(Job.name == JOB_NAME))
        old = session.scalars(select(Lecture).where(Lecture.job_id == job.id)).one()
        old_video = old.youtube_video_id
        user = User(login_id="started_placeholder", password_hash="fixture", nickname="검수",
                    job_id=job.id, created_at=utc_now())
        session.add(user)
        session.flush()
        progress = LectureProgress(user_id=user.id, lecture_id=old.id, watched_ranges=[[0, 8]],
                                   watched_sec=8, last_position_sec=8, updated_at=utc_now())
        session.add(progress)
        session.flush()

        release = seed(session)
        archived = session.get(CatalogRelease, (job.id, "shipping-delay-reply", 1))
        current = session.get(CatalogRelease, (job.id, "shipping-delay-reply", 2))
        assert archived.lecture_id == old.id and not archived.active
        assert current.lecture_id == release.lecture_id and current.active
        assert old.youtube_video_id == old_video and progress.watched_sec == 8
        assert session.get(Lecture, current.lecture_id).youtube_video_id == "c9pBqlOSnx4"
        assert seed(session) == release


def test_seed_supersedes_published_mp4_version(engine):
    with Session(engine) as session:
        seed_demo(session)
        job = session.scalar(select(Job).where(Job.name == JOB_NAME))
        old = session.scalars(select(Lecture).where(Lecture.job_id == job.id)).one()
        v1 = json.loads(TRADE_MANIFEST.read_text(encoding="utf-8"))
        v1["version"] = 1
        v1["lecture"]["youtube_video_id"] = "local:trade-delay-v1"
        publish_catalog(session, Catalog.model_validate(v1), adopt_lecture_id=old.id)
        v2 = seed(session)
        assert v2.lecture_id != old.id and old.youtube_video_id == "local:trade-delay-v1"
        assert not session.get(CatalogRelease, (job.id, "shipping-delay-reply", 1)).active
        assert session.get(CatalogRelease, (job.id, "shipping-delay-reply", 2)).active


@pytest.fixture(scope="module")
def engine():
    url = database_url("TEST_DATABASE_URL")
    if not url.database or not url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to a dedicated _test database.")
    database = make_engine(url)
    config = Config(str(ROOT / "alembic.ini"))
    with database.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    try:
        yield database
    finally:
        database.dispose()


def test_fastapi_reports_database_ready(engine):
    with TestClient(
        create_app(engine, jwt_secret="test-only-valid-signing-key-32-bytes")
    ) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_fastapi_reports_database_unavailable(engine):
    # The isolated server will reject a nonexistent account.
    database = make_engine(
        database_url("TEST_DATABASE_URL").set(username="missing_health_check_user")
    )
    try:
        application = create_app(
            engine, jwt_secret="test-only-valid-signing-key-32-bytes"
        )

        def unavailable_session():
            with Session(database) as session:
                yield session

        application.dependency_overrides[get_session] = unavailable_session
        with TestClient(application) as client:
            response = client.get("/health")
    finally:
        database.dispose()
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_initial_migration_uses_mysql_8_and_expected_tables(engine):
    assert engine.dialect.server_version_info[0] == 8
    assert set(inspect(engine).get_table_names()) == {
        "job",
        "user",
        "lecture",
        "lecture_progress",
        "task",
        "task_material",
        "attempt",
        "refresh_token",
        "chat_message",
        "chat_request",
        "answer_revision",
        "attempt_event",
        "attempt_score",
        "grading_job",
        "catalog_release",
        "alembic_version",
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT @@character_set_database")) == "utf8mb4"


def test_seed_can_be_run_twice(engine):
    with Session(engine) as session:
        seed_demo(session)
        seed_demo(session)
        job = session.scalars(select(Job).where(Job.name == "무역 사무")).one()
        lecture = session.scalars(select(Lecture).where(Lecture.job_id == job.id)).one()
        task = session.scalars(select(Task).where(Task.lecture_id == lecture.id)).one()
        assert task.job_id == job.id
        assert task.grading_rubric["criterion_max_scores"] == {
            "RESULT": 50,
            "PROMPT": 15,
            "VERIFY": 20,
            "IMPROVE": 15,
        }
        assert task.grading_rubric["task_solution_min_score"] == 40
        assert task.grading_rubric["ai_use_min_score"] == 40
        assert task.grading_rubric["verify_min_score"] == 12
        assert task.grading_rubric["required_checks"][0]["check_id"] == "SHIPPING_DATE"
        materials = session.scalars(
            select(TaskMaterial.title).where(TaskMaterial.task_id == task.id)
        )
        assert set(materials) == {"바이어 메일 원문", "계약서 일부", "선적 일정표"}


@pytest.fixture
def attempt_setup(engine):
    with Session(engine) as session:
        seed_demo(session)
        task = session.scalars(
            select(Task).join(Job).where(Job.name == JOB_NAME, Task.title == TASK_TITLE)
        ).one()
        now = utc_now()
        user = User(
            login_id="schema_test",
            password_hash="test-only",
            nickname="스키마 테스트",
            job_id=task.job_id,
            created_at=now,
        )
        session.add(user)
        session.flush()
        yield session, task, now, user.id


def build_attempt(task, now, user_id, status="IN_PROGRESS"):
    return Attempt(
        task_id=task.id,
        user_id=user_id,
        status=status,
        started_at=now,
        deadline_at=now,
        grading_rubric_snapshot=task.grading_rubric,
    )


def test_database_enforces_only_one_active_attempt(attempt_setup):
    session, task, now, user_id = attempt_setup
    current = build_attempt(task, now, user_id)
    session.add(current)
    session.flush()

    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(build_attempt(task, now, user_id))
        session.flush()

    current.status = "GRADING"
    session.flush()
    session.add(build_attempt(task, now, user_id))
    session.flush()


def test_deleting_attempt_cascades_to_all_child_records(attempt_setup):
    session, task, now, user_id = attempt_setup
    attempt = build_attempt(task, now, user_id)
    session.add(attempt)
    session.flush()
    attempt_id = attempt.id
    message = ChatMessage(
        attempt_id=attempt_id, role="USER", content="검증", created_at=now
    )
    session.add(message)
    session.flush()
    session.add_all(
        [
            AnswerRevision(attempt_id=attempt_id, content="초안", created_at=now),
            AttemptEvent(attempt_id=attempt_id, type="FOCUS", created_at=now),
            AttemptScore(
                attempt_id=attempt_id,
                criterion="VERIFY",
                score=12,
                feedback="검증 기록",
                evidence_message_id=message.id,
            ),
        ]
    )
    session.flush()
    session.delete(attempt)
    session.flush()

    for model in (ChatMessage, AnswerRevision, AttemptEvent, AttemptScore):
        assert (
            session.scalar(
                select(model.id if model != AttemptScore else model.criterion).where(
                    model.attempt_id == attempt_id
                )
            )
            is None
        )


def test_database_keeps_max_length_emoji_answers(attempt_setup):
    session, task, now, user_id = attempt_setup
    answer = "🧪" * 20_000
    attempt = build_attempt(task, now, user_id)
    attempt.answer = answer
    session.add(attempt)
    session.flush()
    revision = AnswerRevision(attempt_id=attempt.id, content=answer, created_at=now)
    session.add(revision)
    session.flush()
    session.expire_all()
    assert attempt.answer == answer
    assert revision.content == answer


def test_request_session_rolls_back_unsaved_changes_on_error(engine):
    application = create_app(engine, jwt_secret="test-only-valid-signing-key-32-bytes")
    job_name = "request_rollback_test"

    @application.post("/test-write")
    def write_job(session: Annotated[Session, Depends(get_session)]):
        session.add(Job(name=job_name))
        session.flush()
        raise HTTPException(status_code=409)

    with TestClient(application) as client:
        assert client.post("/test-write").status_code == 409

    with Session(engine) as session:
        assert session.scalar(select(Job).where(Job.name == job_name)) is None
