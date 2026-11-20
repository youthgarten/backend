from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_hex, token_urlsafe

import bcrypt
import jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import RefreshToken
from app.services.attempts import lock_owner

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 14 * 24 * 60 * 60
COOKIE_NAME = "refresh_token"
COOKIE_PATH = "/api/v1/auth"


def password_digest(password: str) -> bytes:
    # Pre-hash so bcrypt receives the entire allowed UTF-8 password.
    return sha256(password.encode()).hexdigest().encode("ascii")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password_digest(password), bcrypt.gensalt()).decode("ascii")


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password_digest(password), hashed.encode("ascii"))


def timestamp(now: datetime) -> int:
    return int(now.replace(tzinfo=timezone.utc).timestamp())


def access_token(user_id: int, session_id: str, secret: str, now: datetime) -> str:
    issued = timestamp(now)
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": session_id,
            "type": "access",
            "iat": issued,
            "exp": issued + ACCESS_SECONDS,
        },
        secret,
        algorithm="HS256",
    )


def access_identity(token: str, secret: str, now: datetime) -> tuple[int, str]:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={
                "require": ["sub", "sid", "type", "iat", "exp"],
                "verify_exp": False,
                "verify_iat": False,
            },
        )
        user_id = int(claims["sub"])
        current = timestamp(now)
        if (
            claims["type"] != "access"
            or not 0 < user_id < 2**63
            or not isinstance(claims["sid"], str)
            or len(claims["sid"]) != 64
            or type(claims["exp"]) is not int
            or type(claims["iat"]) is not int
            or claims["iat"] > current
        ):
            raise ValueError("Invalid claims")
    except (jwt.InvalidTokenError, ValueError, TypeError) as error:
        raise ApiError("INVALID_TOKEN") from error
    if claims["exp"] <= current:
        raise ApiError("TOKEN_EXPIRED")
    return user_id, claims["sid"]


def verify_access_token(token: str, secret: str, now: datetime) -> int:
    return access_identity(token, secret, now)[0]


def access_session_is_active(
    session: Session, user_id: int, session_id: str, now: datetime
) -> bool:
    return (
        session.scalar(
            select(RefreshToken.id)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.family_id == session_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .limit(1)
        )
        is not None
    )


def refresh_hash(token: str) -> str:
    return sha256(token.encode()).hexdigest()


def issue_refresh(
    session: Session, user_id: int, now: datetime, family_id: str | None = None
) -> str:
    token = token_urlsafe(48)
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=refresh_hash(token),
            family_id=family_id or token_hex(32),
            created_at=now,
            expires_at=now + timedelta(seconds=REFRESH_SECONDS),
        )
    )
    return token


def lock_refresh(session: Session, token: str | None) -> RefreshToken | None:
    if token is None:
        return None
    query = select(RefreshToken).where(RefreshToken.token_hash == refresh_hash(token))
    prior = session.scalar(query)
    if prior is None:
        return None
    lock_owner(session, prior.user_id)
    return session.scalar(
        query.with_for_update().execution_options(populate_existing=True)
    )


def revoke_session(session: Session, token: RefreshToken, now: datetime) -> None:
    session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == token.user_id,
            RefreshToken.family_id == token.family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
