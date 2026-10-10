"""Database engine, session factory and FastAPI dependency.

The database location comes from ``DATABASE_URL`` and defaults to a local
SQLite file under ``./data``. SQLite foreign keys are enabled explicitly
because SQLite ignores them unless asked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/geospatial.db")


def _is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def _ensure_sqlite_directory(url: str) -> None:
    """Create the parent directory of a file-based SQLite database."""
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() == "sqlite" and database and database != ":memory:":
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str = DATABASE_URL) -> Engine:
    """Build an engine; SQLite gets thread-safe settings and FK enforcement."""
    connect_args: dict[str, object] = {}
    if _is_sqlite(url):
        _ensure_sqlite_directory(url)
        # Requests run in a threadpool, so connections cross threads.
        connect_args["check_same_thread"] = False

    new_engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)

    if _is_sqlite(url):

        @event.listens_for(new_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return new_engine


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables if they do not exist yet."""
    Base.metadata.create_all(bind=engine)
