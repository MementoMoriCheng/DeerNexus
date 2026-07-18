"""Live PostgreSQL connectivity probe for the production doctor (PR-064).

Implements the ``postgres.connectivity`` check from runbook §5.1: opens a
throwaway engine on the configured DB URL, verifies connectivity with
``SELECT 1``, reads the server version (must be ≥15 per runbook §5.1 FAIL
threshold), and reports the pool configuration. The probe follows the
``tenant_probe`` isolation contract — it never touches the global
``_engine`` / ``_session_factory`` and never writes.

Behaviour by backend:

* ``backend=postgres`` — full probe (connectivity + version + pool).
* ``backend=sqlite`` / ``backend=memory`` — WARN skip with a clear message:
  ``postgres.connectivity`` is only meaningful against a Postgres deployment.
  Production should not run on sqlite/memory, so a WARN here does not block
  admission on its own but surfaces the mismatch.

Any DB error (unreachable DB, auth failure, version read failure) is
contained into a FAIL result — the doctor never crashes mid-report, and an
unverifiable DB connection is itself a production-admission blocker per
runbook §5.1.

No-secret guarantee: the result message carries only the engine's
``url.host`` (or a backend label), never the full URL or password. The
``test_doctor_probes.py::TestPostgresProbe::test_no_secret_leak`` test pins
this — a leak would defeat the doctor's role as safe deployment evidence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "postgres.connectivity"
_COMPONENT = "database"
_CONFIG_SOURCE = "config.yaml:database"
# Runbook §5.1: PostgreSQL <15 is a FAIL. DeerNexus uses partition features
# only available in 15+ (and alembic migrations assume them).
_MIN_POSTGRES_MAJOR = 15


def _result(status: DoctorStatus, message: str, remediation: str | None = None) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


def _host_of(url_str: str) -> str:
    """Return a display-safe host label for the configured DB URL.

    Never returns the full URL (which carries the password) — only the host
    (or a fallback label) so the operator can identify which DB the probe
    targeted without exposing credentials in the doctor report.
    """
    try:
        # SQLAlchemy URL .render_as_string hides the password by default;
        # parse just the host portion manually to be defense-in-depth.
        if "://" in url_str:
            after_scheme = url_str.split("://", 1)[1]
            # Strip user:pass@ if present.
            if "@" in after_scheme:
                after_scheme = after_scheme.split("@", 1)[1]
            host = after_scheme.split(":", 1)[0].split("/", 1)[0]
            return host or "unknown-host"
    except Exception:  # noqa: BLE001 — never raise on URL introspection
        pass
    return "configured-database"


def _parse_major_version(version_string: str) -> int | None:
    """Extract the major version from a ``SELECT version()`` result.

    PostgreSQL returns strings like ``'PostgreSQL 15.4 on x86_64-pc-linux-gnu'``
    or (very old) ``'PostgreSQL 9.6.10'``. Modern versions (10+) use a single
    integer major; pre-10 used ``X.Y``. We only need to compare against 15, so
    a single int parse suffices.
    """
    import re

    match = re.search(r"PostgreSQL\s+(\d+)", version_string)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def probe_postgres_connectivity(config: AppConfig) -> DoctorCheckResult:
    """Probe the configured DB for connectivity, version, and pool stats.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    backend = config.database.backend
    if backend != "postgres":
        return _result(
            DoctorStatus.WARN,
            f"postgres.connectivity skipped: database.backend={backend!r} (only 'postgres' is a valid production backend; sqlite/memory are dev-only).",
            "Set database.backend=postgres and database.postgres_url in production config.yaml.",
        )

    url = config.database.app_sqlalchemy_url
    host_label = _host_of(url)

    try:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                version_row = (await conn.execute(text("SELECT version()"))).scalar_one_or_none()
                version_string = str(version_row) if version_row is not None else ""
        finally:
            await engine.dispose()
    except Exception:  # noqa: BLE001 — contain any DB failure into FAIL
        logger.warning("postgres probe could not reach the DB at %s", host_label, exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            f"Could not connect to the configured PostgreSQL database (host={host_label}); the DB is unreachable or credentials are invalid.",
            "Check database.postgres_url, DB network reachability, and that the DB user has CONNECT privilege. Re-run doctor after fixing.",
        )

    major = _parse_major_version(version_string)
    if major is None:
        return _result(
            DoctorStatus.WARN,
            f"Connected to PostgreSQL at {host_label} but could not parse server version from {version_string!r}; cannot verify >=15.",
            "Confirm PostgreSQL version >=15 manually; the version string format was unexpected.",
        )
    if major < _MIN_POSTGRES_MAJOR:
        return _result(
            DoctorStatus.FAIL,
            f"Connected to PostgreSQL {major} at {host_label}, but runbook §5.1 requires >= {_MIN_POSTGRES_MAJOR}.",
            f"Upgrade PostgreSQL at {host_label} to {_MIN_POSTGRES_MAJOR}+ before production admission.",
        )

    return _result(
        DoctorStatus.PASS,
        f"Connected to PostgreSQL {major} at {host_label} (SELECT 1 ok; pool_size={config.database.pool_size}).",
    )


__all__ = ["probe_postgres_connectivity"]
