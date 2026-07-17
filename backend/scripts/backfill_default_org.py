"""Backfill legacy NULL ``org_id`` resource rows to the default Organization (PR-023).

One-shot data migration: assigns the default Organization (the one the
single-Org tenant resolver already binds every request to) to every
threads_meta / runs / run_events / feedback row whose ``org_id`` is still
NULL. Idempotent — re-running after a successful backfill is a no-op (the
``WHERE org_id IS NULL`` candidate set is empty).

Usage:
    python -m scripts.backfill_default_org [--dry-run] [--batch-size N] [--throttle-ms M] [--org-id ID]

Must NOT be wired into app startup (pr-split-guide.md §14); run it
explicitly during the Phase B migration window, after a backup and a
production-scale dry-run (ADR-0002 §8.2).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    from app.gateway.config import DEFAULT_ORG_NAME, DEFAULT_ORG_SLUG, get_gateway_config
    from deerflow.config import get_app_config
    from deerflow.persistence.engine import (
        close_engine,
        get_session_factory,
        init_engine_from_config,
    )
    from deerflow.tenancy import backfill_resource_org, ensure_default_org

    config = get_app_config()
    await init_engine_from_config(config.database)
    try:
        sf = get_session_factory()
        if sf is None:
            print("Error: persistence engine not available (check config.database).", file=sys.stderr)
            return 1

        gw_config = get_gateway_config()
        org_id = args.org_id or gw_config.default_org_id

        # Precondition: the default Org must exist so the RESTRICT FK on
        # each resource table's org_id is satisfiable. Idempotent — a no-op
        # if the lifespan (or a prior run) already created it.
        await ensure_default_org(sf, org_id=org_id, slug=DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)

        mode = "DRY RUN" if args.dry_run else "BACKFILL"
        logger.info("%s -> org_id=%s batch_size=%d throttle_ms=%d", mode, org_id, args.batch_size, args.throttle_ms)

        report = await backfill_resource_org(
            sf,
            org_id=org_id,
            batch_size=args.batch_size,
            throttle_ms=args.throttle_ms,
            dry_run=args.dry_run,
        )

        for t in report.tables:
            logger.info(
                "  table=%-14s before_null=%d updated=%d after_null=%d batches=%d",
                t.table,
                t.before_null_count,
                t.updated_rows,
                t.after_null_count,
                t.batches,
            )
        logger.info("total_updated=%d", report.total_updated)

        if not args.dry_run:
            for gate, per_table in report.validation.items():
                for table_name, count in per_table.items():
                    status = "PASS" if count == 0 else "FAIL"
                    logger.info("  validation %s %s=%d %s", gate, table_name, count, status)
            if not report.passed:
                logger.error("Validation FAILED — see gate counts above.")
                return 2
            logger.info("Validation PASSED.")
        return 0
    finally:
        await close_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill legacy NULL org_id resource rows to the default Org (PR-023).")
    parser.add_argument("--dry-run", action="store_true", help="Count candidates only; do not UPDATE.")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per committed batch (default 500).")
    parser.add_argument("--throttle-ms", type=int, default=50, help="Pause between batches in ms (default 50).")
    parser.add_argument(
        "--org-id",
        default=None,
        help="Target Org id (default: gateway config DEER_FLOW_DEFAULT_ORG_ID).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
