"""Async SQLAlchemy setup."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

engine = create_async_engine(settings.db_url, future=True,
                             connect_args={"check_same_thread": False},
                             pool_size=5, max_overflow=10)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, conn_record):
    """Enable WAL mode + NORMAL synchronous for better concurrency on SQLite."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create tables and apply pending schema patches."""
    from . import models  # noqa: F401  (ensure mappers loaded)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # --- incremental patches (SQLite ALTER TABLE ADD COLUMN) ---
        await conn.run_sync(_patch_schema)


def _patch_schema(connection):
    """Add missing columns to existing tables."""
    from sqlalchemy import text
    # attempts.extra_ports
    cols = connection.execute(text("PRAGMA table_info(attempts)")).fetchall()
    att_col_names = {row[1] for row in cols}
    if "extra_ports" not in att_col_names:
        connection.execute(text("ALTER TABLE attempts ADD COLUMN extra_ports TEXT"))
    # challenges.proxy_prefix
    cols = connection.execute(text("PRAGMA table_info(challenges)")).fetchall()
    ch_col_names = {row[1] for row in cols}
    if "proxy_prefix" not in ch_col_names:
        connection.execute(text("ALTER TABLE challenges ADD COLUMN proxy_prefix VARCHAR(64)"))
