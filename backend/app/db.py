"""Database engine and session helpers.

SQLite is opened in WAL (write ahead logging) mode so reads do not block the
single writer, which matters because the SSE stream and the ingest pipeline can
touch the database at the same time. Foreign keys are enforced.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

# check_same_thread=False lets the engine be shared across FastAPI's threadpool.
_connect_args = {"check_same_thread": False}

settings.ensure_dirs()

engine: Engine = create_engine(
    f"sqlite:///{settings.DB_PATH}",
    echo=False,
    connect_args=_connect_args,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Apply WAL and durability pragmas on every new connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


def init_db() -> None:
    """Create all tables. Safe to call repeatedly; it is a no-op if they exist.

    Importing models here guarantees every table is registered on SQLModel's
    metadata before create_all runs.
    """
    from . import models  # noqa: F401  (registers tables on metadata)

    settings.ensure_dirs()
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    with Session(engine) as session:
        yield session
