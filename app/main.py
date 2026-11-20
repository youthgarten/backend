from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.api import attempts, auth, jobs, lectures, map_ws, tasks, users
from app.clock import utc_now
from app.db import database_url, make_engine
from app.errors import ApiError, api_error_handler
from app.ops import install_operations
from app.services.ai import AIProvider, configured_provider
from app.services.grading import grade_attempt
from app.services.grading_jobs import recover_grading
from app.services.grading_worker import GradingWorker
from app.services.presence import Presence
from app.settings import setting


def create_app(
    engine: Engine | None = None,
    *,
    clock: Callable[[], datetime] = utc_now,
    jwt_secret: str | None = None,
    provider: AIProvider | None = None,
    recover_inflight: bool = True,
    run_worker: bool | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        secret = jwt_secret if jwt_secret is not None else setting("JWT_SECRET")
        if not secret or len(secret.strip().encode("utf-8")) < 32:
            raise RuntimeError(
                "JWT_SECRET must be set to a signing key of at least 32 bytes."
            )
        secure_cookies = setting("SECURE_COOKIES") or "1"
        if secure_cookies not in {"0", "1"}:
            raise RuntimeError(
                "SECURE_COOKIES must be 1 for HTTPS or 0 for local HTTP."
            )
        application.state.jwt_secret = secret
        application.state.secure_cookies = secure_cookies == "1"

        database = engine if engine is not None else make_engine(database_url())
        application.state.session_factory = sessionmaker(database)
        worker = GradingWorker(
            application, enabled=engine is None if run_worker is None else run_worker
        )
        application.state.grading_worker = worker
        try:
            if recover_inflight:
                recover_grading(application.state.session_factory)
            worker.start()
            yield
        finally:
            await worker.stop()
            if engine is None:
                database.dispose()

    application = FastAPI(title="youthgarten API", lifespan=lifespan)
    application.state.clock = clock
    application.state.presence = Presence()
    application.state.grade_attempt = partial(grade_attempt, application)
    application.state.provider = (
        provider if provider is not None else configured_provider()
    )
    application.add_exception_handler(ApiError, api_error_handler)
    for router in (
        auth.router,
        jobs.router,
        users.router,
        lectures.router,
        tasks.router,
        attempts.router,
        map_ws.router,
    ):
        application.include_router(router, prefix="/api/v1")

    install_operations(application)
    return application


app = create_app()
