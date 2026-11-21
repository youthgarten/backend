from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session

from app.settings import ENV_FILE, setting


def database_url(key: str = "DATABASE_URL") -> URL:
    value = setting(key)
    if not value:
        raise RuntimeError(f"Set {key} in the environment or {ENV_FILE}.")
    url = make_url(value)
    if url.drivername != "mysql+pymysql" or not url.database:
        raise ValueError(f"{key} must be a mysql+pymysql URL with a database name.")
    return url.update_query_dict({"charset": "utf8mb4"})


def make_engine(url: URL) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        isolation_level="READ COMMITTED",
        connect_args={"connect_timeout": 3},
    )


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session
