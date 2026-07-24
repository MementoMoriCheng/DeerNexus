"""Application-level backup Job (PR-065).

Snapshots every DeerFlow-owned table into a tamper-evident manifest + per-
table content files under ``destination_dir``. This is the **evidence layer**
complementing (not replacing) the DB platform's backup (pg_dump/WAL/PITR —
runbook §9.1). The operator's cron schedules this Job and moves the
destination into a separate, encrypted failure domain.

Not wired into app startup (pr-split-guide §14: "不与数据库业务迁移混合" and
the Job is a single-shot, not a long-lived process). Mirrors the
``backfill_default_org.py`` CLI skeleton: lazy imports, init engine, do the
work, stamp a metric, close engine, exit code.

Usage:
    python -m scripts.backup [--config config.yaml] [--dest DIR] [--dry-run]

``--dest`` overrides ``production.backup.destination_dir``. ``--dry-run``
computes the manifest (so the operator can preview table coverage + digests)
without writing content files or the manifest.

Exit codes:
    0 — backup written (or dry-run manifest printed)
    1 — misconfiguration (no destination, backup disabled, engine error)
    2 — snapshot failed mid-run (destination left untouched / partial)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    from deerflow.config import get_app_config
    from deerflow.config.app_config import AppConfig
    from deerflow.observability.metrics import set_backup_last_success_timestamp
    from deerflow.persistence.backup import (
        MANIFEST_FILENAME,
        take_snapshot,
        write_manifest,
        write_table_rows,
    )
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine_from_config

    if args.config:
        config = AppConfig.from_file(args.config)
    else:
        config = get_app_config()
    await init_engine_from_config(config.database)
    try:
        sf = get_session_factory()
        if sf is None:
            print("Error: persistence engine not available (check config.database).", file=sys.stderr)
            return 1

        # Resolve destination: --dest flag wins, else config declaration.
        destination = args.dest or config.production.backup.destination_dir
        if not destination:
            print(
                "Error: no backup destination. Set production.backup.destination_dir or pass --dest.",
                file=sys.stderr,
            )
            return 1
        destination_path = Path(destination)

        now = datetime.now(UTC)
        logger.info(
            "backup: backend=%s destination=%s declared_rpo_hours=%d dry_run=%s",
            config.database.backend,
            destination_path,
            config.production.backup.declared_rpo_hours,
            args.dry_run,
        )

        manifest = await take_snapshot(
            sf,
            backend=config.database.backend,
            declared_rpo_hours=config.production.backup.declared_rpo_hours,
            now=now,
        )
        logger.info(
            "backup: backup_id=%s schema_version=%s tables=%d",
            manifest.backup_id,
            manifest.schema_version,
            len(manifest.tables),
        )
        for entry in manifest.tables:
            logger.info("  table=%-22s rows=%-6d digest=%s", entry.name, entry.row_count, entry.content_digest[:12])

        if args.dry_run:
            logger.info("backup: dry-run complete; no files written.")
            return 0

        # Write per-table content files then the manifest. Content files first
        # so a crash mid-write does not leave a manifest pointing at missing
        # content; the manifest is the last thing written (atomic rename).
        for entry in manifest.tables:
            rows = await _read_rows(sf, entry.name)
            write_table_rows(destination_path, entry.name, rows)
        written = write_manifest(destination_path, manifest)
        logger.info("backup: manifest written to %s (%s)", written, MANIFEST_FILENAME)

        # Stamp the freshness metric (fail-open by contract; a metric miss
        # never breaks the backup). The doctor probe reads the manifest file
        # as the durable source of truth, so this metric is advisory.
        set_backup_last_success_timestamp(manifest.created_at.timestamp())
        return 0
    finally:
        await close_engine()


async def _read_rows(sf, table_name: str) -> list:
    """Read one table's normalised rows for content-file writing.

    Local helper (not reusing snapshot.read_table_rows directly) so the
    lazy import surface stays in _run.
    """
    from deerflow.persistence.backup import read_table_rows

    return await read_table_rows(sf, table_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Application-level backup Job (PR-065). Snapshots every DeerFlow table to a manifest + content files.")
    parser.add_argument("--config", default=None, help="Path to config.yaml (default: repo config.yaml).")
    parser.add_argument("--dest", default=None, help="Destination directory (overrides production.backup.destination_dir).")
    parser.add_argument("--dry-run", action="store_true", help="Compute the manifest without writing files.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
