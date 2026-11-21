from datetime import timedelta
from secrets import token_hex
from typing import Annotated

from anyio import from_thread
from fastapi import APIRouter, Cookie, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import Database, signing_key
from app.api.schemas import Profile, Token
from app.api.users import profile
from app.errors import ApiError
from app.models import Job, User
from app.services.auth import (
    ACCESS_SECONDS,
    COOKIE_NAME,
    COOKIE_PATH,
    REFRESH_SECONDS,
    access_token,
    check_password,
    hash_password,
    issue_refresh,
    lock_refresh,
    revoke_session,
)

router = APIRouter(prefix="/auth", tags=["auth"])
RefreshCookie = Annotated[str | None, Cookie(alias=COOKIE_NAME)]
MISSING_USER_HASH = "$2b$12$s3yb2c3KqiQKvUcOBtWR7.NCO7.Ow57SOJQqFckThsh23w9sN9sXK"
REFRESH_REPLAY_GRACE = timedelta(seconds=10)


class Signup(BaseModel):
    login_id: str = Field(min_length=4, max_length=30, pattern=r"^[a-z0-9_]+$")
    password: str = Field(min_length=8, max_length=64)
    nickname: str = Field(min_length=2, max_length=20)
    job_id: int


class Login(BaseModel):
    login_id: str = Field(max_length=30)
    password: str = Field(max_length=64)


@router.post("/signup", status_code=201, response_model=Profile)
def signup(body: Signup, session: Database, request: Request):
    job = session.get(Job, body.job_id)
    if job is None:
        raise ApiError("INVALID_JOB")
    user = User(
        login_id=body.login_id,
        password_hash=hash_password(body.password),
        nickname=body.nickname,
        job_id=job.id,
        created_at=request.app.state.clock(),
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        if (
            session.scalar(select(User.id).where(User.login_id == body.login_id))
            is not None
        ):
            raise ApiError("LOGIN_ID_TAKEN") from error
        raise
    data = profile(user, job)
    session.commit()
    return data


def session_response(
    request: Request,
    response: Response,
    user_id: int,
    session_id: str,
    token: str,
    now,
):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=REFRESH_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=request.app.state.secure_cookies,
    )
    return {
        "access_token": access_token(user_id, session_id, signing_key(request), now),
        "token_type": "bearer",
        "expires_in": ACCESS_SECONDS,
    }


@router.post("/login", response_model=Token)
def login(body: Login, request: Request, response: Response, session: Database):
    user = session.scalar(select(User).where(User.login_id == body.login_id))
    password_ok = check_password(
        body.password, user.password_hash if user is not None else MISSING_USER_HASH
    )
    if user is None or not password_ok:
        raise ApiError("INVALID_CREDENTIALS")
    now = request.app.state.clock()
    session_id = token_hex(32)
    token = issue_refresh(session, user.id, now, session_id)
    data = session_response(request, response, user.id, session_id, token, now)
    session.commit()
    return data


@router.post("/refresh", response_model=Token)
def refresh(
    request: Request,
    response: Response,
    session: Database,
    refresh_token: RefreshCookie = None,
):
    existing = lock_refresh(session, refresh_token)
    now = request.app.state.clock()
    if existing is None or existing.expires_at <= now:
        raise ApiError("INVALID_REFRESH_TOKEN")
    if existing.revoked_at is not None:
        if now > existing.revoked_at + REFRESH_REPLAY_GRACE:
            revoke_session(session, existing, now)
            session.commit()
        raise ApiError("INVALID_REFRESH_TOKEN")
    existing.revoked_at = now
    token = issue_refresh(session, existing.user_id, now, existing.family_id)
    data = session_response(
        request, response, existing.user_id, existing.family_id, token, now
    )
    session.commit()
    return data


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    session: Database,
    refresh_token: RefreshCookie = None,
):
    existing = lock_refresh(session, refresh_token)
    now = request.app.state.clock()
    revoked_session = None
    if existing is not None and existing.expires_at > now:
        revoked_session = existing.family_id
        revoke_session(session, existing, now)
    session.commit()
    if revoked_session is not None:
        from_thread.run(request.app.state.presence.revoke_session, revoked_session)
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=request.app.state.secure_cookies,
    )
