"""Restore an application-level backup into an empty DB (PR-065).

DR scenario (runbook §10.2): the primary DB is lost; the operator points the
gateway at a fresh empty DB and runs this script against it to repopulate
from a backup manifest + its content files. The target MUST be empty — a
restore into a live DB would corrupt both the backup point and live data,
and ``restore_from_manifest`` asserts that up front.

``--target-db-url`` is **required and explicit** — this script never restores
into the configured (running) primary DB by accident. The operator must name
the recovery target. After restore, the verification gates
(``backup.verify.verify_restore``) run automatically and the exit code reflects
any FAIL (SKIP gates are labelled deferrals, not failures).

Usage:
    python -m scripts.restore --manifest PATH --target-db-url URL [--content-dir DIR] [--skip-verify]

``--content-dir`` defaults to the manifest's parent directory (where the Job
wrote ``snapshot/<table>.jsonl``). ``--skip-verify`` restores without the
post-restore gates (for an operator who will verify separately).

Exit codes:
    0 — restore + verification passed
    1 — misconfiguration / target not empty / content missing
    2 — restore completed but verification FAILED
    3 — restore itself failed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from deerflow.persistence.backup import RestoreError, load_manifest, restore_from_manifest, verify_restore

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = load_manifest(manifest_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    content_dir = Path(args.content_dir) if args.content_dir else manifest_path.parent
    logger.info(
        "restore: manifest=%s backup_id=%s schema_version=%s target=%s content_dir=%s",
        manifest_path,
        manifest.backup_id,
        manifest.schema_version,
        args.target_db_url,
        content_dir,
    )

    # The target engine is throwaway; restore owns the session factory so it
    # can insert+commit per table. ``create_async_engine`` + create_all on an
    # empty DB provisions the schema before row insert (the restore contract
    # is "fill rows into an empty DB", and we create_all here so the operator
    # does not have to run alembic first — though running alembic upgrade head
    # is the production path and would produce an identical schema).
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base  # noqa: F401  (ensures import side-effect)

    engine = create_async_engine(args.target_db_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)
        report = await restore_from_manifest(sf, manifest, content_dir)
        # Stamp the restored schema's alembic head to the manifest's recorded
        # version. The restore reproduced the exact schema the snapshot was
        # taken against (same metadata, same create_all), so stamping is
        # accurate — and it lets the ``schema_compatible`` verify gate (and a
        # later ``alembic upgrade head`` on the target) see the true version
        # rather than an unversioned DB. ``alembic_version`` is not an ORM
        # model (alembic owns it), so ``create_all`` did not make it; create
        # it inline with a portable DDL before stamping.
        if manifest.schema_version and manifest.schema_version != "unknown":
            async with engine.begin() as conn:
                await conn.execute(sa_text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"))
                await conn.execute(sa_text("DELETE FROM alembic_version"))
                await conn.execute(
                    sa_text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                    {"v": manifest.schema_version},
                )
            logger.info("restore: stamped alembic_version=%s", manifest.schema_version)
    except RestoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"Error: restore failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        await engine.dispose()

    logger.info(
        "restore: integrity_ok=%s tables=%s",
        report.integrity_ok,
        ", ".join(f"{t}={report.restored_counts[t]}" for t in report.tables_in_order),
    )
    if not report.integrity_ok:
        logger.error("restore: row-count integrity FAILED — see logs above.")
        return 2

    if args.skip_verify:
        logger.info("restore: --skip-verify set; post-restore gates not run.")
        return 0

    # Re-open a fresh engine for verify (the restore engine is disposed) so
    # the gates read the restored DB without any in-session state.
    verify_engine = create_async_engine(args.target_db_url)
    try:
        verify_sf = async_sessionmaker(verify_engine, expire_on_commit=False)
        verify_report = await verify_restore(verify_sf, manifest)
    finally:
        await verify_engine.dispose()

    for gate in verify_report.gates:
        level = logging.INFO if gate.status == "PASS" else (logging.WARNING if gate.status == "SKIP" else logging.ERROR)
        logger.log(level, "  verify %-38s %s — %s", gate.name, gate.status, gate.detail)
    logger.info(
        "restore: verify passed=%d failed=%d skipped=%d",
        verify_report.passed,
        verify_report.failed,
        verify_report.skipped,
    )
    if verify_report.failed > 0:
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore an application-level backup into an empty DB (PR-065).")
    parser.add_argument("--manifest", required=True, help="Path to the backup manifest.json to restore from.")
    parser.add_argument(
        "--target-db-url",
        required=True,
        help="SQLAlchemy async URL of the EMPTY target DB to restore into (e.g. postgresql+asyncpg://... or sqlite+aiosqlite:///path).",
    )
    parser.add_argument("--content-dir", default=None, help="Directory holding snapshot/<table>.jsonl (default: manifest's parent).")
    parser.add_argument("--skip-verify", action="store_true", help="Restore without running the post-restore verification gates.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
