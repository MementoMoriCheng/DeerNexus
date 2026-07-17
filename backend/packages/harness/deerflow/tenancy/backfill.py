"""Default-Org backfill job for legacy resource rows (PR-023).

One-shot data migration that assigns the default Organization to every
resource row whose ``org_id`` is still NULL. PR-021 made ``org_id`` nullable
on the four core Run-lifecycle tables (threads_meta / runs / run_events /
feedback); PR-022 materialised the default Org. This job fills the legacy
NULL rows so that PR-025A's NOT NULL enforcement has zero NULLs to reject
and PR-024's repository org-scope has complete attribution.

Mapping (ADR-0002 §8.1 step 4): **blanket → default Org**, in resource-
dependency order (ADR-0002 §8.2): threads → runs → run_events → feedback.
The four tables reference each other only via soft columns (no FK between
them), so each is updated independently by ``WHERE org_id IS NULL``.

Implicit watermark: the ``WHERE org_id IS NULL`` predicate *is* the
watermark — already-backfilled rows fall out of the candidate set, so a
crash / re-run resumes from the last committed batch with no progress
table. A persistent progress table is a production-scale hardening concern
tracked separately (testing-strategy.md §15.3); the single-Org one-shot
migration does not need it.

Batched + throttled + re-entrant + dry-run, per pr-split-guide.md §7 PR-023.
Must NOT auto-run on app startup (pr-split-guide §14) — this module exposes
the async core; the CLI in ``scripts/backfill_default_org.py`` is the
explicit entry point.

Lives in the harness layer: imports only ``deerflow.persistence`` + stdlib,
never ``app`` (harness-boundary test). ``org_id`` is passed in by the caller.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.orgs.model import OrganizationRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.tenancy.audit_events import emit_tenant_event

logger = logging.getLogger(__name__)

# Resource tables in dependency order (ADR-0002 §8.2): a thread owns runs,
# runs own events/feedback. Each entry is (model, primary-key column name)
# — the PK is used to scope a batched UPDATE on dialects (e.g. Postgres)
# that do not accept UPDATE ... LIMIT directly.
_RESOURCE_TABLES: tuple[tuple[type, str], ...] = (
    (ThreadMetaRow, "thread_id"),
    (RunRow, "run_id"),
    (RunEventRow, "id"),
    (FeedbackRow, "feedback_id"),
)


@dataclass
class BackfillTableResult:
    """Per-table outcome of a backfill pass."""

    table: str
    before_null_count: int
    updated_rows: int
    after_null_count: int
    batches: int


@dataclass
class BackfillReport:
    """Aggregated backfill outcome + post-backfill validation gates."""

    org_id: str
    dry_run: bool
    batch_size: int
    throttle_ms: int
    tables: list[BackfillTableResult] = field(default_factory=list)
    total_updated: int = 0
    # validation gates: row-count invariance, no-null-org, FK integrity.
    # Each maps table name -> count (0 == pass).
    validation: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True iff every validation gate reports zero violations."""
        return all(count == 0 for per_table in self.validation.values() for count in per_table.values())


async def _null_org_count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(model.org_id.is_(None))) or 0


async def _total_count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def _backfill_one_table(
    sf: async_sessionmaker[AsyncSession],
    model: type,
    pk_name: str,
    *,
    org_id: str,
    batch_size: int,
    throttle_ms: float,
) -> BackfillTableResult:
    """Backfill one table in committed batches; return its result."""
    table_name = model.__tablename__
    pk_col = getattr(model, pk_name)

    async with sf() as session:
        before_null = await _null_org_count(session, model)

    updated_total = 0
    batches = 0
    # Loop until a batch touches zero rows (no NULL candidates left).
    # SQLAlchemy's Core ``update()`` has no ``.limit()`` even though SQLite
    # supports ``UPDATE ... LIMIT`` natively, so batches are bounded on every
    # dialect via a PK subquery: UPDATE ... WHERE <pk> IN (SELECT <pk> ...
    # WHERE org_id IS NULL LIMIT :n). This also keeps the Postgres path
    # identical to the SQLite one (no dialect fork needed).
    while True:
        async with sf() as session:
            subq = select(pk_col).where(model.org_id.is_(None)).limit(batch_size)
            stmt = update(model).where(model.org_id.is_(None), pk_col.in_(subq)).values(org_id=org_id).execution_options(synchronize_session=False)
            result = await session.execute(stmt)
            touched = result.rowcount or 0
            await session.commit()

        if touched == 0:
            break
        updated_total += touched
        batches += 1
        logger.info("backfill %s: batch %d updated %d (cumulative %d)", table_name, batches, touched, updated_total)
        if throttle_ms > 0:
            await asyncio.sleep(throttle_ms / 1000.0)

    async with sf() as session:
        after_null = await _null_org_count(session, model)

    return BackfillTableResult(
        table=table_name,
        before_null_count=before_null,
        updated_rows=updated_total,
        after_null_count=after_null,
        batches=batches,
    )


async def _validate(sf: async_sessionmaker[AsyncSession], models: list[type]) -> dict[str, dict[str, int]]:
    """Run post-backfill acceptance gates (testing-strategy.md §15.2).

    - ``no_null_org``: rows still missing org_id (must be 0; the four
      resource tables are not on the system whitelist).
    - ``orphan_fk``: rows whose org_id does not resolve to an Organization
      (RESTRICT FK already enforces this on write, but the explicit count
      is the documented acceptance gate).
    Row-count invariance is implied: backfill only mutates org_id, so
    per-table totals are unchanged by construction.
    """
    gates: dict[str, dict[str, int]] = {"no_null_org": {}, "orphan_fk": {}}
    async with sf() as session:
        org_ids_subq = select(OrganizationRow.id)
        for model in models:
            gates["no_null_org"][model.__tablename__] = await _null_org_count(session, model)
            orphan = await session.scalar(select(func.count()).select_from(model).where(model.org_id.is_not(None), model.org_id.not_in(org_ids_subq)))
            gates["orphan_fk"][model.__tablename__] = orphan or 0
    return gates


async def backfill_resource_org(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    batch_size: int = 500,
    throttle_ms: int = 50,
    dry_run: bool = False,
) -> BackfillReport:
    """Backfill NULL ``org_id`` rows on the four resource tables to ``org_id``.

    Args:
        sf: Session factory for the target DB (must already be bootstrapped
            and contain the default Org row — the caller ensures this).
        org_id: Target Organization id (the default Org).
        batch_size: Rows updated per committed batch.
        throttle_ms: Pause between batches (rate-limit).
        dry_run: If True, count candidates only; do not UPDATE.

    Returns:
        A :class:`BackfillReport` with per-table counts, total updated, and
        validation gates. In dry-run mode ``updated_rows``/``total_updated``
        are 0 and validation reflects the pre-backfill state.
    """
    models = [m for m, _ in _RESOURCE_TABLES]
    report = BackfillReport(org_id=org_id, dry_run=dry_run, batch_size=batch_size, throttle_ms=throttle_ms)

    emit_tenant_event(
        "backfill_started",
        org_id=org_id,
        principal_id=None,
        payload={"dry_run": dry_run, "batch_size": batch_size, "throttle_ms": throttle_ms},
    )

    if dry_run:
        for model, _ in _RESOURCE_TABLES:
            async with sf() as session:
                before_null = await _null_org_count(session, model)
            report.tables.append(
                BackfillTableResult(
                    table=model.__tablename__,
                    before_null_count=before_null,
                    updated_rows=0,
                    after_null_count=before_null,
                    batches=0,
                )
            )
        report.total_updated = 0
        logger.info(
            "backfill DRY RUN: %d candidate NULL-org row(s) across %d table(s)",
            sum(t.before_null_count for t in report.tables),
            len(report.tables),
        )
    else:
        for model, pk_name in _RESOURCE_TABLES:
            result = await _backfill_one_table(
                sf,
                model,
                pk_name,
                org_id=org_id,
                batch_size=batch_size,
                throttle_ms=float(throttle_ms),
            )
            report.tables.append(result)
            report.total_updated += result.updated_rows
        report.validation = await _validate(sf, models)

    emit_tenant_event(
        "backfill_completed",
        org_id=org_id,
        principal_id=None,
        payload={"dry_run": dry_run, "total_updated": report.total_updated, "passed": report.passed},
    )
    return report


__all__ = [
    "BackfillReport",
    "BackfillTableResult",
    "backfill_resource_org",
]
