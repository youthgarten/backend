from alembic import context
from sqlalchemy.engine import Connection

from app.db import database_url, make_engine
from app.models import Base
from app.models.catalog import CatalogRelease  # noqa: F401 - register catalog metadata
from app.models.chat import ChatRequest  # noqa: F401 - register chat metadata


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url(), target_metadata=Base.metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
    else:
        engine = make_engine(database_url())
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()
