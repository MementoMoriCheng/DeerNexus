"""Async SQLAlchemy engine lifecycle management.

Initializes at Gateway startup, provides session factory for
repositories, disposes at shutdown.

When database.backend="memory", init_engine is a no-op and
get_session_factory() returns None. Repositories must check for
None and fall back to in-memory implementations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading as _threading
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def _json_serializer(obj: object) -> str:
    """JSON serializer with ensure_ascii=False for Chinese character support."""
    return json.dumps(obj, ensure_ascii=False)


logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def _auto_create_postgres_db(url: str) -> None:
    """Connect to the ``postgres`` maintenance DB and CREATE DATABASE.

    The target database name is extracted from *url*.  The connection is
    made to the default ``postgres`` database on the same server using
    ``AUTOCOMMIT`` isolation (CREATE DATABASE cannot run inside a
    transaction).
    """
    from sqlalchemy import text
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    db_name = parsed.database
    if not db_name:
        raise ValueError("Cannot auto-create database: no database name in URL")

    # Connect to the default 'postgres' database to issue CREATE DATABASE
    maint_url = parsed.set(database="postgres")
    maint_engine = create_async_engine(maint_url, isolation_level="AUTOCOMMIT")
    try:
        async with maint_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        logger.info("Auto-created PostgreSQL database: %s", db_name)
    finally:
        await maint_engine.dispose()


async def init_engine(
    backend: str,
    *,
    url: str = "",
    echo: bool = False,
    pool_size: int = 5,
    sqlite_dir: str = "",
) -> None:
    """Create the async engine and session factory, then auto-create tables.

    Args:
        backend: "memory", "sqlite", or "postgres".
        url: SQLAlchemy async URL (for sqlite/postgres).
        echo: Echo SQL to log.
        pool_size: Postgres connection pool size.
        sqlite_dir: Directory to create for SQLite (ensured to exist).
    """
    global _engine, _session_factory

    if backend == "memory":
        logger.info("Persistence backend=memory -- ORM engine not initialized")
        return

    if backend == "postgres":
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            raise ImportError(
                "database.backend is set to 'postgres' but asyncpg is not installed.\n"
                "Install it with:\n"
                "    cd backend && uv sync --all-packages --extra postgres\n"
                "On the next `make dev` the postgres extra is auto-detected from\n"
                "config.yaml (database.backend: postgres) and reinstalled, so it\n"
                "will not be wiped again. Set UV_EXTRAS=postgres in .env to opt in\n"
                "explicitly. Or switch to backend: sqlite in config.yaml for\n"
                "single-node deployment."
            ) from None

    if backend == "sqlite":
        import os

        from sqlalchemy import event

        # Offload the directory creation: ``init_engine`` runs on the FastAPI
        # lifespan event loop, and a sync ``os.makedirs`` (a stat + mkdir
        # syscall) blocks it during startup. Mirrors the #1912 fix for the
        # checkpointer's ``ensure_sqlite_parent_dir``.
        await asyncio.to_thread(os.makedirs, sqlite_dir or ".", exist_ok=True)
        _engine = create_async_engine(url, echo=echo, json_serializer=_json_serializer)

        # Enable WAL on every new connection. SQLite PRAGMA settings are
        # per-connection, so we wire the listener instead of running PRAGMA
        # once at startup. WAL gives concurrent reads + writers without
        # blocking and is the standard recommendation for any production
        # SQLite deployment (TC-UPG-06 in AUTH_TEST_PLAN.md). The companion
        # ``synchronous=NORMAL`` is the safe-and-fast pairing — fsync only
        # at WAL checkpoint boundaries instead of every commit.
        # We also widen ``busy_timeout`` to 30s here. Python's sqlite3 driver
        # defaults to 5s, which is fine for transient row contention but too
        # tight for cross-process bootstrap: the second-N-th Gateway process
        # may need to wait while the first runs ``ALTER TABLE`` /
        # ``CREATE TABLE`` for a fresh schema. The same widened timeout is
        # mirrored on the alembic-spawned engine in
        # ``migrations/env.py::run_migrations_online`` so its connections
        # behave identically.
        @event.listens_for(_engine.sync_engine, "connect")
        def _enable_sqlite_wal(dbapi_conn, _record):  # noqa: ARG001 — SQLAlchemy contract
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA busy_timeout=30000;")
            finally:
                cursor.close()
    elif backend == "postgres":
        _engine = create_async_engine(
            url,
            echo=echo,
            pool_size=pool_size,
            pool_pre_ping=True,
            json_serializer=_json_serializer,
        )
    else:
        raise ValueError(f"Unknown persistence backend: {backend!r}")

    # PR-063: wire SQLAlchemy event listeners for §4.6 db_query_duration_seconds
    # and db_transaction_failure_total. Attached to the sync_engine because
    # that is where cursor-level events fire (the async wrapper delegates).
    # Best-effort: the listeners log + bump metrics but never raise into the
    # query path. SQLite and Postgres both support these hooks.
    _install_db_metrics_listeners(_engine.sync_engine)

    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # Schema bootstrap (hybrid):
    #   - empty DB        -> create_all + alembic stamp head
    #   - legacy DB       -> create_all (baseline tables only, backfill) + alembic stamp baseline + upgrade head
    #   - already managed -> alembic upgrade head
    # Concurrency: Postgres advisory lock (true cross-process); SQLite uses an
    # in-process asyncio.Lock plus a 30s PRAGMA busy_timeout (also set on
    # alembic's own connections in env.py) -- multi-process SQLite bootstrap
    # is best-effort, gated by SQLite's natural file-level write lock.
    # See deerflow.persistence.bootstrap for the full state machine.
    from deerflow.persistence.bootstrap import bootstrap_schema

    try:
        await bootstrap_schema(_engine, backend=backend)
    except Exception as exc:
        if backend == "postgres" and "does not exist" in str(exc):
            # Database not yet created -- attempt to auto-create it, then retry.
            await _auto_create_postgres_db(url)
            # Rebuild engine against the now-existing database
            await _engine.dispose()
            _engine = create_async_engine(url, echo=echo, pool_size=pool_size, pool_pre_ping=True, json_serializer=_json_serializer)
            _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
            await bootstrap_schema(_engine, backend=backend)
        else:
            raise

    logger.info("Persistence engine initialized: backend=%s", backend)


async def init_engine_from_config(config) -> None:
    """Convenience: init engine from a DatabaseConfig object."""
    if config.backend == "memory":
        await init_engine("memory")
        return
    await init_engine(
        backend=config.backend,
        url=config.app_sqlalchemy_url,
        echo=config.echo_sql,
        pool_size=config.pool_size,
        sqlite_dir=config.sqlite_dir if config.backend == "sqlite" else "",
    )


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    """Return the async session factory, or None if backend=memory."""
    return _session_factory


def get_engine() -> AsyncEngine | None:
    """Return the async engine, or None if not initialized."""
    return _engine


async def close_engine() -> None:
    """Dispose the engine, release all connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Persistence engine closed")
    _engine = None
    _session_factory = None


# ---------------------------------------------------------------------------
# PR-063: §4.6 db_pool / db_query / db_transaction metrics
# ---------------------------------------------------------------------------
#
# SQLAlchemy exposes pool stats (checked-in / checked-out / size) on
# ``engine.pool.status()`` and cursor-level timing via ``before_cursor_execute``
# / ``after_cursor_execute`` event listeners. We attach the listeners in
# ``init_engine`` and expose ``get_pool_stats()`` so a periodic gauge scraper
# (or the gateway lifespan) can sample the pool. The transaction-failure
# counter hooks ``handle_error`` (raised per failed transaction commit).


# Thread-local for per-query timing (avoid dict churn keyed by id(cursor)).
_db_query_timings = _threading.local()


def _install_db_metrics_listeners(sync_engine: Any) -> None:
    """Attach §4.6 metrics listeners to *sync_engine*. Idempotent / best-effort."""
    try:
        from sqlalchemy import event

        from deerflow.observability.metrics import (
            inc_db_transaction_failure,
            observe_db_query,
        )

        @event.listens_for(sync_engine, "before_cursor_execute")
        def _before_cursor_execute(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
            import time as _time

            _db_query_timings.started = _time.perf_counter()

        @event.listens_for(sync_engine, "after_cursor_execute")
        def _after_cursor_execute(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
            import time as _time

            started = getattr(_db_query_timings, "started", None)
            if started is None:
                return
            try:
                observe_db_query(_time.perf_counter() - started)
            except Exception:  # noqa: BLE001 — metrics best-effort
                pass
            finally:
                _db_query_timings.started = None

        @event.listens_for(sync_engine, "handle_error")
        def _handle_error(exception_context: Any) -> None:
            try:
                exc = getattr(exception_context, "original_exception", None)
                error_class = type(exc).__name__ if exc is not None else "Unknown"
                inc_db_transaction_failure(error_class=error_class)
            except Exception:  # noqa: BLE001 — metrics best-effort
                pass

    except Exception:  # noqa: BLE001 — listeners are best-effort
        logger.debug("Failed to install DB metrics listeners", exc_info=True)


def get_pool_stats() -> dict[str, int] | None:
    """Return ``{in_use, size}`` for the active engine pool, or None.

    Returns None for memory backend (no engine), or when the engine's pool
    does not expose ``status()`` (some NullPool / SingletonThreadPool variants).
    """
    if _engine is None:
        return None
    try:
        pool = _engine.pool
        status = pool.status()  # e.g. "Pool size: 5  Connections in pool: 3  Checked out: 2"
        # Parse the "Checked out: N" and total "Pool size: N" fields.
        import re

        checked_out_match = re.search(r"Checked out:\s*(\d+)", status)
        pool_size_match = re.search(r"Pool size:\s*(\d+)", status)
        if checked_out_match is None or pool_size_match is None:
            return None
        return {
            "in_use": int(checked_out_match.group(1)),
            "size": int(pool_size_match.group(1)),
        }
    except Exception:  # noqa: BLE001 — pool introspection is best-effort
        return None


def refresh_db_pool_metrics() -> None:
    """Sample the pool and update the §4.6 db_pool_in_use / db_pool_size gauges.

    Called from the gateway lifespan + a periodic refresh hook. Best-effort:
    a parse failure or memory backend is a no-op (gauges stay at their last
    value or unset).
    """
    try:
        from deerflow.observability.metrics import set_db_pool_stats

        stats = get_pool_stats()
        if stats is None:
            return
        set_db_pool_stats(in_use=stats["in_use"], size=stats["size"])
    except Exception:  # noqa: BLE001 — metrics best-effort
        pass
