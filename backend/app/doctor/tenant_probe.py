"""Live-DB tenant migration-phase probe for the production doctor (PR-025C).

Implements the runbook §5.2 "租户迁移状态判定" rule as the doctor's first
live-DB check. The doctor is otherwise config-only by design (see
``app/doctor/production.py``); this module is deliberately separate because
it needs a real database connection to cross-check the configured
``tenancy.multi_org.phase`` against the observed DB state — exactly the
"Doctor 必须读取明确迁移阶段，不得只根据 Feature Flag 猜测状态" mandate.

Read-only guarantee
-------------------

The probe opens a **throwaway** engine via ``create_async_engine`` on the
configured app DB URL, issues only ``SELECT COUNT(*)`` statements, then
``dispose()``s the engine. It never:

* touches the global ``_engine`` / ``_session_factory`` in
  ``deerflow.persistence.engine`` (those belong to the running gateway);
* runs alembic or ``bootstrap_schema`` (unlike
  ``init_engine_from_config``, which would mutate the DB);
* writes anything.

A DB connection failure is contained: the probe returns a FAIL
``DoctorCheckResult`` with a connectivity remediation rather than raising, so
the doctor never crashes mid-report.

Judgement table (runbook §5.2)
------------------------------

The probe reads two facts from the DB and one from config:

* ``phase`` — ``current_multi_org_phase()`` (the single read-point in
  ``deerflow.tenancy.feature_flags``);
* ``null_org_total`` — sum of NULL ``org_id`` rows across the four
  Run-lifecycle resource tables (``threads_meta`` / ``runs`` /
  ``run_events`` / ``feedback``);
* ``org_count`` — row count of ``organizations``.

It then classifies the (phase, null_org_total, org_count) triple per the
runbook table. The two unconditional FAIL rows are the safety properties:

* "Feature ON 但仍有空 ``org_id``" (runbook §5.2 row 4) — phase in
  (validation, active) with residual NULLs;
* "租户过滤关闭但存在多 Org" (runbook §5.2 row 5) — phase=disabled with >1 org
  (an org exists that the single-Org resolver cannot route traffic to).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.doctor.models import DoctorCheckResult, DoctorStatus
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.orgs.model import OrganizationRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.tenancy.feature_flags import current_multi_org_phase

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

# The four Run-lifecycle resource tables whose ``org_id`` is enforced NOT NULL
# (PR-025A / migration 0006). Kept in sync with
# ``deerflow.tenancy.backfill._RESOURCE_TABLES`` — both modules must agree on
# which tables carry the tenant column.
_NULL_ORG_TABLES: tuple[type, ...] = (ThreadMetaRow, RunRow, RunEventRow, FeedbackRow)

_CHECK_ID = "tenant.migration_state"
_COMPONENT = "tenant"
_CONFIG_SOURCE = "config.yaml:tenancy.multi_org.phase,production database"


def _result(
    status: DoctorStatus,
    message: str,
    remediation: str | None = None,
) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


async def _count_null_org(sf: async_sessionmaker[AsyncSession]) -> int:
    """Sum NULL ``org_id`` rows across the four resource tables.

    Reuses the query shape from ``deerflow.tenancy.backfill._null_org_count``.
    A separate session per table keeps the query trivially portable across
    SQLite/Postgres and avoids a cross-table UNION that would couple the
    probe to identical column types.
    """
    total = 0
    for model in _NULL_ORG_TABLES:
        async with sf() as session:
            count = await session.scalar(select(func.count()).select_from(model).where(model.org_id.is_(None)))
            total += int(count or 0)
    return total


async def _count_orgs(sf: async_sessionmaker[AsyncSession]) -> int:
    async with sf() as session:
        return int(await session.scalar(select(func.count()).select_from(OrganizationRow)) or 0)


def _classify(phase: str, null_org_total: int, org_count: int) -> DoctorCheckResult:
    """Apply the runbook §5.2 state table to the observed triple.

    Split per-phase so each branch's message names the offending condition
    an operator needs to act on. The two runbook FAIL rows are checked first
    within each relevant phase so a residual-NULL or multi-org-without-filter
    state cannot be masked by a milder judgement.
    """
    if phase == "disabled":
        if org_count > 1:
            return _result(
                DoctorStatus.FAIL,
                f"phase=disabled (single-Org) but {org_count} Organization rows exist; the tenant resolver cannot route traffic to the extra org(s) — this matches runbook §5.2 '租户过滤关闭但存在多 Org'.",
                "Remove the extra Org rows, or advance tenancy.multi_org.phase to 'validation'/'active' so the resolver can serve them.",
            )
        return _result(
            DoctorStatus.PASS,
            f"phase=disabled (single-Org); {org_count} Org row(s), consistent with today's single-Org default.",
        )

    if phase == "validation":
        if null_org_total > 0:
            return _result(
                DoctorStatus.FAIL,
                f"phase=validation but {null_org_total} NULL org_id row(s) remain across resource tables; the validation phase requires zero NULL org_id (PR-023 backfill + PR-025A enforce must be complete).",
                "Run the default-org backfill (scripts/backfill_default_org.py) until NULL org_id = 0, then re-run doctor.",
            )
        if org_count < 1:
            return _result(
                DoctorStatus.FAIL,
                "phase=validation but no Organization rows exist; the validation Org and default Org must both be present.",
                "Ensure the gateway lifespan seeded the default + validation Org (tenancy.multi_org.validation_org), then re-run doctor.",
            )
        return _result(
            DoctorStatus.PASS,
            f"phase=validation; NULL org_id=0, {org_count} Org row(s) — Enforce validation prerequisites satisfied.",
        )

    # phase == "active"
    if null_org_total > 0:
        return _result(
            DoctorStatus.FAIL,
            f"phase=active (multi-org ON) but {null_org_total} NULL org_id row(s) remain; matches runbook §5.2 'Feature ON 但仍有空 org_id'.",
            "Run the default-org backfill until NULL org_id = 0 before keeping multi-org active.",
        )
    if org_count < 2:
        return _result(
            DoctorStatus.WARN,
            f"phase=active but only {org_count} Org row(s); multi-org is ON without a second tenant — allowed but should be reconciled (promote the validation Org or drop phase back to validation).",
        )
    return _result(
        DoctorStatus.PASS,
        f"phase=active; NULL org_id=0, {org_count} Org row(s) — multi-org Active.",
    )


async def probe_tenant_migration_phase(config: AppConfig) -> DoctorCheckResult:
    """Probe the live DB and classify the tenant migration state (runbook §5.2).

    Opens a throwaway read-only engine on ``config.database.app_sqlalchemy_url``,
    counts NULL ``org_id`` rows and ``organizations`` rows, reads the configured
    phase via :func:`current_multi_org_phase`, and classifies the triple.

    Any DB error (unreachable DB, missing tables, auth failure) is contained
    into a FAIL result with a connectivity remediation — the doctor must never
    crash mid-report, and an unverifiable migration state is itself a blocker
    for production admission.
    """
    phase = current_multi_org_phase()
    url = config.database.app_sqlalchemy_url

    try:
        engine = create_async_engine(url)
        try:
            sf = async_sessionmaker(engine, expire_on_commit=False)
            null_org_total = await _count_null_org(sf)
            org_count = await _count_orgs(sf)
        finally:
            await engine.dispose()
    except Exception:  # noqa: BLE001 — contain any DB failure into FAIL
        logger.warning("tenant migration probe could not reach the DB", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            f"Could not query the database to verify tenant migration state (phase={phase}); the migration state is unverifiable, which blocks production admission.",
            "Check database.backend / database.postgres_url / sqlite path in config.yaml and DB network reachability, then re-run doctor.",
        )

    return _classify(phase, null_org_total, org_count)


__all__ = ["probe_tenant_migration_phase"]
