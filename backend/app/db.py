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


# Schema migrations, applied in order to databases created before the change.
# Fresh databases get the full current schema from create_all and are stamped
# with the latest version directly, so these never run on them. Append-only:
# never edit or reorder an entry that has shipped.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, ["ALTER TABLE credential ADD COLUMN rp_id VARCHAR NOT NULL DEFAULT 'localhost'"]),
    (2, ["ALTER TABLE credential ADD COLUMN label VARCHAR NOT NULL DEFAULT ''"]),
    (3, ["ALTER TABLE credential ADD COLUMN created_at TIMESTAMP"]),
    # Explicit account -> rewards-card mapping replaces name-substring guessing.
    (4, ["ALTER TABLE account ADD COLUMN card_key VARCHAR",
         "UPDATE account SET card_key='apple' WHERE name='Apple Card'",
         "UPDATE account SET card_key='wf_autograph' WHERE name='Wells Fargo Autograph'",
         "UPDATE account SET card_key='debit' WHERE name='Wells Fargo Everyday Checking'"]),
    # Statement/Plaid convergence: record where a row came from and which
    # Plaid transaction it corresponds to (linked, not double-counted).
    (5, ["ALTER TABLE \"transaction\" ADD COLUMN origin VARCHAR NOT NULL DEFAULT 'statement'",
         "ALTER TABLE \"transaction\" ADD COLUMN plaid_txn_id VARCHAR",
         "CREATE INDEX IF NOT EXISTS ix_transaction_plaid_txn_id ON \"transaction\" (plaid_txn_id)",
         "UPDATE \"transaction\" SET origin='plaid' WHERE category_source='plaid'",
         "UPDATE \"transaction\" SET origin='ocr' WHERE category_source='ocr'"]),
]

SCHEMA_VERSION = max(v for v, _ in MIGRATIONS) if MIGRATIONS else 0


def _run_migrations(fresh: bool) -> None:
    with engine.connect() as conn:
        raw = conn.connection.driver_connection
        cur = raw.cursor()
        current = cur.execute("PRAGMA user_version").fetchone()[0]
        if fresh:
            cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            raw.commit()
            return
        for version, statements in MIGRATIONS:
            if version > current:
                for sql in statements:
                    cur.execute(sql)
                cur.execute(f"PRAGMA user_version = {version}")
                raw.commit()
        cur.close()


def init_db() -> None:
    """Create all tables and bring existing databases up to schema.

    Safe to call repeatedly. Importing models here guarantees every table is
    registered on SQLModel's metadata before create_all runs.
    """
    from . import models  # noqa: F401  (registers tables on metadata)

    settings.ensure_dirs()
    with engine.connect() as conn:
        existing = conn.exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='transaction'"
        ).scalar()
    SQLModel.metadata.create_all(engine)
    _run_migrations(fresh=not existing)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a session and always closes it."""
    with Session(engine) as session:
        yield session
