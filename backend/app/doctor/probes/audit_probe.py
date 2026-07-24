"""Audit outbox probe for the production doctor (PR-042).

Implements the ``audit.outbox`` check: verifies the ``audit_outbox`` table is
reachable on the configured production DB and that the backlog + dead-letter
count are within the ADR-0005 §14 SLO (oldest pending < 5 min → P2 alert;
dead-letter count must be 0 — any dead-lettered event is a P1 because it is a
compliance-evidence loss).

The probe is a true live check (not a config-only stub): it opens a
throwaway engine against ``config.database.app_sqlalchemy_url`` (mirroring
``postgres_probe``'s isolation — never reuses the global engine, never leaks
secrets), counts ``pending`` / ``dead_letter`` rows, and the age of the
oldest claimable pending row. The Class A same-transaction wiring (PR-042)
means every IAM mutation that commits leaves exactly one ``pending`` row,
so a non-empty table on an idle gateway is expected briefly; the probe
checks the SLO, not the row count.

In-process / backend-aware by design: ``sqlite`` / ``memory`` backends are
dev-only and cannot satisfy a production audit declaration, so the probe
WARNs and skips (a PASS against sqlite would be a misleading green light
for a compliance control-plane). The worker itself (``audit_worker``) is
not exercised here — its drain loop is verified by ``test_audit_outbox`` /
``test_audit_sink_worker``; this probe only confirms the storage + backlog
are healthy where the gateway is running.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "audit.outbox"
_COMPONENT = "audit"
_CONFIG_SOURCE = "config.yaml:production.audit,deerflow/persistence/audit/outbox.py"

#: ADR-0005 §14 SLO threshold for the oldest pending row. A pending row older
#: than this means the drain worker is not keeping up (or is down) — P2.
_OLDEST_PENDING_SLO_SECONDS = 300.0

#: A throwaway engine is created per probe invocation and disposed immediately
#: afterwards, so there is no global pool to leak; the connect timeout keeps a
#: dead DB from hanging the doctor.
_CONNECT_TIMEOUT_SECONDS = 5


def _result(status: DoctorStatus, message: str, remediation: str | None = None) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


async def probe_audit_outbox(config: AppConfig) -> DoctorCheckResult:
    """Verify the ``audit_outbox`` table is reachable and backlog is within SLO.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    backend = config.database.backend
    if backend not in ("postgres", "sqlite"):
        # memory / unknown backends are dev-only: a PASS here would be a
        # misleading green light for a compliance control-plane.
        return _result(
            DoctorStatus.WARN,
            f"audit.outbox skipped: database.backend={backend!r} is not a durable production backend (audit evidence must survive a process restart).",
            "Set database.backend=postgres (or sqlite for a single-node deploy) in production config.yaml.",
        )

    url = config.database.app_sqlalchemy_url
    try:
        from sqlalchemy import func, select, text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.audit.model import AuditOutboxRow
    except Exception:  # noqa: BLE001 — persistence layer broken is a FAIL
        logger.warning("audit persistence layer not importable", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Could not import deerflow.persistence.audit — the audit storage layer is broken.",
            "Reinstall deps (uv sync) and verify the gateway imports cleanly; the audit outbox is a §7.1 compliance prerequisite.",
        )

    try:
        engine = create_async_engine(url, connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS} if backend == "sqlite" else {})
        try:
            async with engine.connect() as conn:
                # Confirm the table exists (a DB that predates migration 0011
                # would otherwise raise on the COUNT, masking the real issue).
                await conn.execute(text("SELECT 1"))
                pending = int((await conn.execute(select(func.count()).select_from(AuditOutboxRow).where(AuditOutboxRow.status == "pending"))).scalar_one())
                dead_letter = int((await conn.execute(select(func.count()).select_from(AuditOutboxRow).where(AuditOutboxRow.status == "dead_letter"))).scalar_one())
                oldest = (await conn.execute(select(func.min(AuditOutboxRow.available_at)).where(AuditOutboxRow.status == "pending"))).scalar_one_or_none()
        finally:
            await engine.dispose()
    except Exception:  # noqa: BLE001 — contain any DB failure into FAIL
        logger.warning("audit outbox probe could not reach the DB", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Could not query the audit_outbox table — the DB is unreachable or the audit_outbox migration (0011) has not run.",
            "Run alembic upgrade head (the gateway does this at startup) and confirm DB connectivity; the audit outbox table must exist for Class A writes to be durable.",
        )

    # Dead-letter is the hard FAIL: a dead-lettered event is a compliance-
    # evidence loss (ADR §8 P1). Any non-zero count fails closed.
    if dead_letter > 0:
        return _result(
            DoctorStatus.FAIL,
            f"audit_outbox has {dead_letter} dead-lettered event(s) — compliance evidence is being lost (ADR §8 P1). Each dead-lettered AuditEvent failed to publish after {_OLDEST_PENDING_SLO_SECONDS:.0f}s of retries.",
            "Inspect audit_outbox rows with status='dead_letter', resolve the publish failure (last_error column), and re-queue them manually. This is a P1.",
        )

    # Oldest-pending SLO (ADR §14 P2): a pending row older than the window
    # means the worker is behind or down.
    oldest_age_seconds = 0.0
    if oldest is not None:
        oldest_dt = oldest
        if oldest_dt.tzinfo is None:
            oldest_dt = oldest_dt.replace(tzinfo=UTC)
        oldest_age_seconds = max(0.0, (datetime.now(UTC) - oldest_dt).total_seconds())
    if oldest_age_seconds > _OLDEST_PENDING_SLO_SECONDS:
        return _result(
            DoctorStatus.FAIL,
            f"audit_outbox oldest pending row is {oldest_age_seconds:.0f}s old (SLO < {_OLDEST_PENDING_SLO_SECONDS:.0f}s) — the drain worker is not keeping up or is down (ADR §14 P2).",
            "Check that the audit worker task is running (gateway lifespan) and that the DB is not saturated. Backlog older than 5 minutes is a P2 alert.",
        )

    return _result(
        DoctorStatus.PASS,
        f"audit_outbox table reachable; {pending} pending, {dead_letter} dead-letter, oldest pending age {oldest_age_seconds:.0f}s (within SLO).",
    )


__all__ = ["probe_audit_outbox"]
