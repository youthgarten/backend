from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session
from support import bearer, watch_to

from app.db import database_url, make_engine
from app.main import create_app
from app.models import Attempt, LectureProgress, User
from app.services.ai import ChatCompletionsProvider
from scripts.seed import seed_demo


class Clock:
    now = datetime(2026, 11, 20, 3)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture(scope="session")
def api_engine():
    url = database_url("TEST_DATABASE_URL")
    if not url.database.endswith("_test"):
        pytest.fail("A dedicated _test database is required.")
    engine = make_engine(url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        seed_demo(session)
    yield engine
    engine.dispose()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def application(api_engine, clock, monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "0")
    return create_app(
        api_engine,
        clock=clock,
        jwt_secret="integration-tests-signing-secret-32-bytes",
        provider=ChatCompletionsProvider(None, None, None),
    )


@pytest.fixture
def client(application):
    with TestClient(application) as browser:
        yield browser


@pytest.fixture
def register(client, api_engine):
    ids = []

    def create(password="testing-password", *, job_name="무역 사무"):
        job = next(
            job
            for job in client.get("/api/v1/jobs").json()
            if job["name"] == job_name
        )
        body = {
            "login_id": "u_" + uuid4().hex[:16],
            "password": password,
            "nickname": "지원",
            "job_id": job["id"],
        }
        created = client.post("/api/v1/auth/signup", json=body)
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
        account = {name: body[name] for name in ("login_id", "password")}
        login = client.post("/api/v1/auth/login", json=account)
        assert login.status_code == 200, login.text
        return created.json(), account, login

    yield create
    with Session(api_engine) as session, session.begin():
        session.execute(delete(Attempt).where(Attempt.user_id.in_(ids)))
        session.execute(delete(LectureProgress).where(LectureProgress.user_id.in_(ids)))
        session.execute(delete(User).where(User.id.in_(ids)))


@pytest.fixture
def ready(client, register, clock):
    user, _, login = register()
    headers = bearer(login)
    task = client.get("/api/v1/tasks", headers=headers).json()[0]
    locked = client.post(
        "/api/v1/attempts", headers=headers, json={"task_id": task["id"]}
    )
    assert (locked.status_code, locked.json()["error"]["code"]) == (
        403,
        "LECTURE_NOT_COMPLETED",
    )
    watch_to(client, headers, clock, task["required_lecture"]["id"], 480)
    return user, headers, task
