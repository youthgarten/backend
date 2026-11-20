import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RefreshToken
from app.ops import RequestGuard
from app.services.ai import ChatCompletionsProvider
from app.services.grading_worker import GradingWorker
from scripts.prune_sessions import prune_sessions


def guarded_client(*, body_limit=256, login_limit=2):
    application = FastAPI()
    application.add_middleware(
        RequestGuard,
        body_limit=body_limit,
        route_limits={"login": login_limit},
    )

    @application.post("/api/v1/auth/login")
    async def login(request: Request):
        return {"size": len(await request.body())}

    return TestClient(application)


def test_guard_limits_body_size_with_or_without_content_length():
    with guarded_client(body_limit=16, login_limit=3) as client:
        invalid = client.post("/api/v1/auth/login", content=b"x" * 17)
        assert invalid.status_code == 413
        assert invalid.json()["error"]["code"] == "REQUEST_TOO_LARGE"
        streaming = client.post("/api/v1/auth/login", content=iter([b"x" * 8, b"x" * 9]))
        assert streaming.status_code == 413
        accepted = client.post("/api/v1/auth/login", content=b"x" * 16)
        assert accepted.status_code == 200
        assert accepted.json() == {"size": 16}


def test_guard_limits_login_and_provides_retry_and_request_identifiers():
    with guarded_client() as client:
        first = client.post("/api/v1/auth/login")
        second = client.post("/api/v1/auth/login")
        blocked = client.post("/api/v1/auth/login")
        assert first.status_code == second.status_code == 200
        assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "RATE_LIMITED"
        assert int(blocked.headers["Retry-After"]) >= 1
        assert blocked.headers["X-Request-ID"]


def test_worker_sweeps_expiration_but_preserves_jobs_until_model_is_configured(monkeypatch):
    application = FastAPI()
    application.state.clock = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    application.state.session_factory = None
    application.state.provider = ChatCompletionsProvider(None, None, None)
    scanned = []
    graded = []

    def scan(*_):
        scanned.append(True)
        return [10]

    async def grade(_application, attempt_id):
        graded.append(attempt_id)

    monkeypatch.setattr("app.services.grading_worker.expire_and_list", scan)
    monkeypatch.setattr("app.services.grading_worker.grade_attempt", grade)

    async def scenario():
        worker = GradingWorker(application, enabled=True)
        await worker.dispatch_once()
        await asyncio.sleep(0)
        assert scanned == [True] and not graded
        application.state.provider = ChatCompletionsProvider("http://localhost:8091/v1", "test", None)
        await worker.dispatch_once()
        await worker.stop()
        assert scanned == [True, True] and graded == [10]

    asyncio.run(scenario())


def test_token_cleanup_only_removes_tokens_past_retention(api_engine, register, clock):
    user, _, _ = register()
    now = clock()
    with Session(api_engine) as session:
        old = RefreshToken(
            user_id=user["id"], token_hash=uuid4().hex + uuid4().hex,
            family_id=uuid4().hex + uuid4().hex,
            created_at=now - timedelta(days=22), expires_at=now - timedelta(days=8),
        )
        recently_expired = RefreshToken(
            user_id=user["id"], token_hash=uuid4().hex + uuid4().hex,
            family_id=uuid4().hex + uuid4().hex,
            created_at=now - timedelta(days=16), expires_at=now - timedelta(days=2),
        )
        session.add_all([old, recently_expired])
        session.flush()
        old_id, recent_id = old.id, recently_expired.id
        assert prune_sessions(session, now, 10000) >= 1
        assert session.get(RefreshToken, old_id) is not None
        assert prune_sessions(session, now, 10000, apply=True) >= 1
        remaining = session.scalars(select(RefreshToken.id).where(
            RefreshToken.id.in_([old_id, recent_id])
        )).all()
        assert remaining == [recent_id]
