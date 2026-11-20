from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import ApiError
from app.models import User
from app.services.auth import access_identity, access_session_is_active

Database = Annotated[Session, Depends(get_session)]
bearer = HTTPBearer(auto_error=False)


def signing_key(request: Request) -> str:
    secret = request.app.state.jwt_secret
    if not secret:
        raise RuntimeError("Set JWT_SECRET before enabling authentication.")
    return secret


def current_user(
    request: Request,
    session: Database,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> User:
    if credentials is None:
        raise ApiError("INVALID_TOKEN")
    user_id, session_id = access_identity(
        credentials.credentials, signing_key(request), request.app.state.clock()
    )
    if not access_session_is_active(
        session, user_id, session_id, request.app.state.clock()
    ):
        raise ApiError("INVALID_TOKEN")
    user = session.get(User, user_id)
    if user is None:
        raise ApiError("INVALID_TOKEN")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
