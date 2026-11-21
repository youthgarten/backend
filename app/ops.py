import logging
import math
from collections import OrderedDict, deque
from datetime import timedelta
from threading import Lock
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.db import get_session
from app.errors import ApiError
from app.services.ai import provider_is_configured
from app.services.chat_turns import ChatSlots
from app.services.grading_jobs import queue_status

logger = logging.getLogger(__name__)
WINDOW = 60
MAX_BUCKETS = 4096
DEFAULT_LIMITS = {
    "login": 30,
    "signup": 15,
    "refresh": 60,
    "map": 60,
}


class RequestTooLarge(Exception):
    pass


def guarded_route(scope: Scope) -> str | None:
    path = scope["path"]
    if scope["type"] == "websocket" and path == "/api/v1/ws/map":
        return "map"
    if scope.get("method") != "POST":
        return None
    if path in {"/api/v1/auth/login", "/api/v1/auth/signup", "/api/v1/auth/refresh"}:
        return path.rsplit("/", 1)[-1]
    return None


class RateLimits:
    def __init__(self, limits: dict[str, int]):
        self.limits = limits
        self.buckets: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self.lock = Lock()

    def retry_after(self, route: str, source: str, now: float) -> int | None:
        limit = self.limits[route]
        key = (route, source)
        with self.lock:
            if key not in self.buckets:
                while self.buckets:
                    oldest_key, oldest = next(iter(self.buckets.items()))
                    if oldest[-1] > now - WINDOW:
                        break
                    del self.buckets[oldest_key]
                if len(self.buckets) >= MAX_BUCKETS:
                    return WINDOW
                self.buckets[key] = deque()
            bucket = self.buckets[key]
            while bucket and bucket[0] <= now - WINDOW:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, math.ceil(bucket[0] + WINDOW - now))
            bucket.append(now)
            self.buckets.move_to_end(key)
        return None


class RequestGuard:
    def __init__(
        self, app: ASGIApp, *, body_limit: int = 256 * 1024,
        route_limits: dict[str, int] | None = None,
    ):
        self.app = app
        self.body_limit = body_limit
        self.limiter = RateLimits(DEFAULT_LIMITS if route_limits is None else route_limits)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        started = monotonic()
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def report(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
                elapsed_ms = round((monotonic() - started) * 1000)
                if message["status"] >= 500 or elapsed_ms >= 1000:
                    logger.info(
                        "request_completed request_id=%s route=%s status=%d elapsed_ms=%d",
                        request_id,
                        getattr(scope.get("route"), "path", "unmatched"),
                        message["status"], elapsed_ms,
                    )
            await send(message)

        route = guarded_route(scope)
        if route in self.limiter.limits:
            source = (scope.get("client") or ("unknown", 0))[0]
            retry = self.limiter.retry_after(route, source, started)
            if retry is not None:
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1013})
                    return
                await JSONResponse(
                    {"error": {"code": "RATE_LIMITED", "message": "잠시 후 다시 시도해 주세요."}},
                    status_code=429, headers={"Retry-After": str(retry)},
                )(scope, receive, report)
                return

        if kind == "http" and scope.get("method") in {"POST", "PUT", "PATCH"}:
            try:
                body = await self.read_body(scope, receive)
            except RequestTooLarge:
                await JSONResponse(
                    {"error": {"code": "REQUEST_TOO_LARGE", "message": "요청 크기가 한도를 넘었어요."}},
                    status_code=413,
                )(scope, receive, report)
                return
            if body is None:
                return
            chunk = body

            async def downstream():
                nonlocal chunk
                if chunk is None:
                    return await receive()
                message = {"type": "http.request", "body": chunk, "more_body": False}
                chunk = None
                return message

            await self.app(scope, downstream, report)
            return

        await self.app(scope, receive, report)

    async def read_body(self, scope: Scope, receive: Receive) -> bytes | None:
        raw_length = Headers(scope=scope).get("content-length", "")
        if raw_length.isascii() and raw_length.isdecimal() and (
            len(raw_length) > 10 or int(raw_length) > self.body_limit
        ):
            raise RequestTooLarge

        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            if len(buffered) + len(chunk) > self.body_limit:
                raise RequestTooLarge
            buffered.extend(chunk)
            if not message.get("more_body"):
                return bytes(buffered)


def database_ready(session: Annotated[Session, Depends(get_session)]) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


def grading_ready(request: Request) -> JSONResponse:
    state = request.app.state
    worker = state.grading_worker
    now = state.clock()
    if not worker.enabled or worker.last_pulse is None or now - worker.last_pulse > timedelta(seconds=15):
        return JSONResponse({"status": "unavailable", "worker": "inactive"}, status_code=503)
    try:
        queue = queue_status(state.session_factory, now)
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable", "worker": "database"}, status_code=503)
    model_configured = provider_is_configured(state.provider)
    ready = model_configured and queue["stale"] == 0
    return JSONResponse(
        {
            "status": "ok" if ready else "degraded",
            "worker": "running",
            "model_configured": model_configured,
            "queue": queue,
        },
        status_code=200 if ready else 503,
    )


def limit_attempt_action(request: Request, action: str, user_id: int) -> None:
    retry = request.app.state.attempt_limits.retry_after(action, str(user_id), monotonic())
    if retry is not None:
        raise ApiError("RATE_LIMITED", headers={"Retry-After": str(retry)})


def install_operations(application: FastAPI) -> None:
    application.state.chat_slots = ChatSlots()
    application.state.attempt_limits = RateLimits({"messages": 12, "regrade": 5})
    application.add_middleware(RequestGuard)
    application.add_api_route("/health", database_ready)
    application.add_api_route("/health/ready", database_ready)
    application.add_api_route("/health/grading", grading_ready)

    @application.get("/health/live")
    def process_alive():
        return {"status": "ok"}
