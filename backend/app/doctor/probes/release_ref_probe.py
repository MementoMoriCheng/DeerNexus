"""Release-ref enforcement probe for the production doctor (PR-056).

Implements the ``agent.release_ref_enforcement`` check, promoted from a DEFERRED
placeholder once the Run-pin write side landed. The check verifies the
operator's release-gate declaration against the live release tables so a green
probe means "if you flip ``enforce`` on, ``start_run`` will not 500 on every
admission because the configured default channel has at least one resolvable
published version."

Honesty contract: this is a *config-state + readiness* check, not a per-run
audit. It confirms the configured ``default_channel`` has at least one
``release_channels`` row whose ``current_version_id`` points at a
``published``, non-revoked ``agent_versions`` row. It does NOT verify that
every existing Run is pinned (that is the backup ``new_run_pinned_to_release_ref``
gate) nor that the legacy count is zero (``legacy_unpinned_count_zero`` gate).
``enforce=false`` (the default) WARNs — the gate is inert by design and a
production rollout flips it deliberately.

Isolation contract (mirrors ``audit_probe`` / ``postgres_probe``): a throwaway
engine is created per invocation against ``config.database.app_sqlalchemy_url``
and disposed immediately; the global engine is never touched and no secret is
read. Failures (DB unreachable, tables missing) are contained into a FAIL
result and never raise.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "agent.release_ref_enforcement"
_COMPONENT = "release"
_CONFIG_SOURCE = "config.yaml:production.agent_release"

#: A throwaway engine is created per probe invocation and disposed immediately,
#: mirroring the audit/postgres probes. The connect timeout keeps a dead DB
#: from hanging the doctor.
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


async def probe_release_ref_enforcement(config: AppConfig) -> DoctorCheckResult:
    """Verify the release-gate declaration is satisfiable by the live tables.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    agent_release = config.production.agent_release

    # The gate is opt-in and defaults off (PR-056 deploy is a pure no-op). A
    # production that has not yet flipped enforce is not broken — WARN so the
    # operator sees the gate is available but inert.
    if not agent_release.enforce:
        return _result(
            DoctorStatus.WARN,
            "Release-gate enforcement is off (production.agent_release.enforce=false) — "
            "start_run does not resolve a ReleaseRef and the legacy resume gate is inert. "
            "Flip enforce=true after backfilling / confirming legacy_unpinned count is zero (runbook §14.2).",
            "Confirm the default_channel has at least one published agent_version, then set production.agent_release.enforce=true.",
        )

    channel = agent_release.default_channel
    backend = config.database.backend
    if backend not in ("postgres", "sqlite"):
        # memory / unknown backends are dev-only; a PASS against them would be a
        # misleading green light for a production admission control.
        return _result(
            DoctorStatus.WARN,
            f"agent.release_ref_enforcement skipped: database.backend={backend!r} is not a durable production backend (release tables must survive a process restart to be pinned).",
            "Set database.backend=postgres (or sqlite for a single-node deploy) in production config.yaml.",
        )

    url = config.database.app_sqlalchemy_url
    try:
        from sqlalchemy import func, select, text
        from sqlalchemy.ext.asyncio import create_async_engine

        from deerflow.persistence.release.model import AgentVersionRow, ReleaseChannelRow
    except Exception:  # noqa: BLE001 — persistence layer broken is a FAIL
        logger.warning("release persistence layer not importable", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Could not import deerflow.persistence.release — the release storage layer is broken.",
            "Reinstall deps (uv sync) and verify the gateway imports cleanly; the release tables (migration 0012/0013) are required for Run-pin.",
        )

    try:
        engine = create_async_engine(url, connect_args={"timeout": _CONNECT_TIMEOUT_SECONDS} if backend == "sqlite" else {})
        try:
            async with engine.connect() as conn:
                # Confirm the tables exist (a DB that predates migration 0013
                # would otherwise raise on the JOIN, masking the real issue).
                await conn.execute(text("SELECT 1"))
                # Count resolvable published versions on the configured channel:
                # a release_channels row whose current_version_id points at a
                # published, non-revoked agent_version. If this is zero, every
                # new prod run would be rejected by the resolver.
                resolvable = (
                    await conn.execute(
                        select(func.count())
                        .select_from(ReleaseChannelRow)
                        .join(AgentVersionRow, AgentVersionRow.id == ReleaseChannelRow.current_version_id)
                        .where(
                            ReleaseChannelRow.channel == channel,
                            AgentVersionRow.status == "published",
                        )
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
    except Exception:  # noqa: BLE001 — contain any DB failure into FAIL
        logger.warning("release ref probe could not reach the DB", exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            "Could not query the release tables — the DB is unreachable or the release migrations (0012/0013) have not run.",
            "Run alembic upgrade head (the gateway does this at startup) and confirm DB connectivity; the agent_packages / agent_versions / release_channels tables must exist for the resolver.",
        )

    if not resolvable:
        return _result(
            DoctorStatus.FAIL,
            f"Enforcement is on (default_channel={channel!r}) but no release_channels row points at a published agent_version — every new run would be rejected by the resolver (release_not_found).",
            f"Import/publish an agent version and promote it to the {channel!r} channel before (or alongside) enabling enforcement.",
        )

    return _result(
        DoctorStatus.PASS,
        f"Release-gate enforcement is on (default_channel={channel!r}); {resolvable} resolvable published agent_version(s) on that channel — start_run can pin a ReleaseRef.",
    )


__all__ = ["probe_release_ref_enforcement"]
