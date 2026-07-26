"""Channel repository + CAS tests for the release-channel layer (PR-053).

Covers :mod:`deerflow.persistence.release.repository` channel functions against
an isolated SQLite: get_or_create idempotency, the ADR §5 channel status gate
(dev/staging/prod), promote/rollback, the CAS primitive (one-writer-wins),
cross-Org isolation, ReleaseEvent append, and session passthrough.

Fixture conventions mirror ``test_agent_artifact_repository.py``: boot an
isolated SQLite via ``init_engine``, yield ``get_session_factory()``, tear
down with ``close_engine``. Each test builds a parent Package + Version via
the PR-052 repository so the channel FKs resolve.

Channel IDs: ``ART-700`` series (repository layer).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.release import (
    CHANNEL_DEV,
    CHANNEL_PROD,
    CHANNEL_STAGING,
    ChannelGateError,
    ReleaseConflictError,
    create_agent_package,
    create_agent_version,
    get_channel,
    get_or_create_channel,
    list_events,
    promote_channel,
    rollback_channel,
    set_version_status,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'channel_repo.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _pkg(sf, *, org_id: str = ORG_ID, name: str = "alpha"):
    return await create_agent_package(sf, org_id=org_id, name=name, display_name=name)


async def _version(
    sf,
    pkg_id: str,
    *,
    org_id: str = ORG_ID,
    version: str = "1.0.0",
    content: str = "artifact-bytes",
    status: str | None = None,
):
    ver = await create_agent_version(
        sf,
        org_id=org_id,
        package_id=pkg_id,
        version=version,
        manifest={"schema_version": "1.0", "agent_entry": "soul"},
        content=content,
    )
    # create_agent_version already stamps status="draft"; only transition if a
    # different status is requested (draft → draft is not a legal self-transition).
    if status is not None and status != "draft":
        await set_version_status(sf, version_id=ver.id, org_id=org_id, status=status)
    return ver


# ---------------------------------------------------------------------------
# get_or_create_channel (ART-700)
# ---------------------------------------------------------------------------


class TestGetOrCreateChannel:
    async def test_creates_with_null_current_version(self, sf):
        pkg = await _pkg(sf)
        ch = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_DEV)
        assert ch.current_version_id is None
        assert ch.row_version == 1
        assert ch.channel == CHANNEL_DEV
        assert ch.workspace_id is None

    async def test_idempotent_returns_same_row(self, sf):
        pkg = await _pkg(sf)
        first = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_DEV)
        second = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_DEV)
        assert first.id == second.id
        assert first.row_version == 1  # not bumped on read

    async def test_different_channels_independent_rows(self, sf):
        pkg = await _pkg(sf)
        dev = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_DEV)
        staging = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_STAGING)
        prod = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD)
        assert len({dev.id, staging.id, prod.id}) == 3

    async def test_unknown_channel_raises_value_error(self, sf):
        pkg = await _pkg(sf)
        with pytest.raises(ValueError):
            await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel="qa")

    async def test_cross_org_independent(self, sf):
        pkg_a = await _pkg(sf, org_id=ORG_ID, name="shared")
        pkg_b = await _pkg(sf, org_id=OTHER_ORG_ID, name="shared")
        ch_a = await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg_a.id, channel=CHANNEL_DEV)
        ch_b = await get_or_create_channel(sf, org_id=OTHER_ORG_ID, package_id=pkg_b.id, channel=CHANNEL_DEV)
        assert ch_a.id != ch_b.id
        assert ch_a.org_id == ORG_ID
        assert ch_b.org_id == OTHER_ORG_ID

    async def test_get_channel_returns_none_for_missing(self, sf):
        pkg = await _pkg(sf)
        assert await get_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_DEV) is None


# ---------------------------------------------------------------------------
# Promote + channel gate (ART-710)
# ---------------------------------------------------------------------------


class TestPromote:
    async def test_dev_promote_draft_allowed(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="draft")
        ch, ev = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_DEV,
            target_version_id=ver.id,
            expected_channel_version=1,
        )
        assert ch.current_version_id == ver.id
        assert ch.row_version == 2  # bumped by CAS
        assert ev.action == "promote"
        assert ev.from_version_id is None  # first promote
        assert ev.to_version_id == ver.id

    async def test_staging_rejects_draft(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="draft")
        with pytest.raises(ChannelGateError):
            await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                channel=CHANNEL_STAGING,
                target_version_id=ver.id,
                expected_channel_version=1,
            )

    async def test_staging_allows_reviewed(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="reviewed")
        ch, _ = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_STAGING,
            target_version_id=ver.id,
            expected_channel_version=1,
        )
        assert ch.current_version_id == ver.id

    async def test_prod_rejects_reviewed(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="reviewed")
        with pytest.raises(ChannelGateError):
            await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                channel=CHANNEL_PROD,
                target_version_id=ver.id,
                expected_channel_version=1,
            )

    async def test_prod_allows_published(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="published")
        ch, _ = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=ver.id,
            expected_channel_version=1,
        )
        assert ch.current_version_id == ver.id

    async def test_second_promote_records_from_version(self, sf):
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, version="1.0.0", status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=1,
        )
        ch, ev = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v2.id,
            expected_channel_version=2,  # bumped after first promote
        )
        assert ev.from_version_id == v1.id
        assert ev.to_version_id == v2.id
        assert ch.row_version == 3

    async def test_target_version_wrong_package_raises(self, sf):
        pkg_a = await _pkg(sf, name="a")
        pkg_b = await _pkg(sf, name="b")
        ver_b = await _version(sf, pkg_b.id, status="draft")
        with pytest.raises(ValueError):  # router → 404
            await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg_a.id,
                channel=CHANNEL_DEV,
                target_version_id=ver_b.id,
                expected_channel_version=1,
            )

    async def test_target_version_wrong_org_raises(self, sf):
        pkg_a = await _pkg(sf, org_id=ORG_ID)
        pkg_b = await _pkg(sf, org_id=OTHER_ORG_ID, name="beta")
        ver_b = await _version(sf, pkg_b.id, org_id=OTHER_ORG_ID, status="draft")
        with pytest.raises(ValueError):  # cross-Org → 404
            await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg_a.id,
                channel=CHANNEL_DEV,
                target_version_id=ver_b.id,
                expected_channel_version=1,
            )


# ---------------------------------------------------------------------------
# CAS — one-writer-wins (ART-720, the load-bearing concurrency test)
# ---------------------------------------------------------------------------


class TestCasOneWriterWins:
    """ADR §7: only one of N concurrent promotes with the same
    ``expected_channel_version`` wins; the others get
    :class:`ReleaseConflictError`.

    SQLite serialises writes on a single connection, so a true race is hard
    to reproduce. These tests instead verify the CAS *semantics*: (a) a stale
    ``expected_channel_version`` after a prior promote must conflict, and (b)
    two promotes sharing the same expected version in separate transactions
    resolve to one-winner + one-loser.
    """

    async def test_stale_expected_version_conflicts(self, sf):
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        # First promote succeeds, bumps row_version 1 → 2.
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=1,
        )
        # Second promote still expects row_version=1 (stale) → conflict.
        with pytest.raises(ReleaseConflictError):
            await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                channel=CHANNEL_PROD,
                target_version_id=v2.id,
                expected_channel_version=1,  # stale
            )

    async def test_correct_expected_after_promote_succeeds(self, sf):
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=1,
        )
        # Caller re-read row_version (now 2) and retries with correct value.
        ch, _ = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v2.id,
            expected_channel_version=2,  # correct
        )
        assert ch.current_version_id == v2.id
        assert ch.row_version == 3

    async def test_two_concurrent_promotes_same_expected_one_wins(self, sf):
        """Two promotes sharing the same ``expected_channel_version`` in
        separate sessions: the first commits and bumps; the second's CAS
        predicate no longer matches → :class:`ReleaseConflictError`."""
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, version="1.0.0", status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        # Winner: promote v1 with expected=1.
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=1,
        )
        # Loser: same expected=1, but row_version is now 2 → conflict.
        with pytest.raises(ReleaseConflictError):
            await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                channel=CHANNEL_PROD,
                target_version_id=v2.id,
                expected_channel_version=1,
            )
        # The channel still points at the winner's target.
        ch = await get_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD)
        assert ch is not None
        assert ch.current_version_id == v1.id


# ---------------------------------------------------------------------------
# Rollback (ART-730)
# ---------------------------------------------------------------------------


class TestRollback:
    async def test_prod_rollback_to_published_allowed(self, sf):
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, version="1.0.0", status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        # Promote v2 then rollback to v1.
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v2.id,
            expected_channel_version=1,
        )
        ch, ev = await rollback_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=2,
        )
        assert ch.current_version_id == v1.id
        assert ev.action == "rollback"
        assert ev.from_version_id == v2.id
        assert ev.to_version_id == v1.id

    async def test_prod_rollback_to_revoked_rejected(self, sf):
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, version="1.0.0", status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        await set_version_status(sf, version_id=v1.id, org_id=ORG_ID, status="revoked")
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v2.id,
            expected_channel_version=1,
        )
        with pytest.raises(ChannelGateError):
            await rollback_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                channel=CHANNEL_PROD,
                target_version_id=v1.id,
                expected_channel_version=2,
            )


# ---------------------------------------------------------------------------
# ReleaseEvent append + listing (ART-740)
# ---------------------------------------------------------------------------


class TestReleaseEvents:
    async def test_events_recorded_in_order(self, sf):
        pkg = await _pkg(sf)
        v1 = await _version(sf, pkg.id, version="1.0.0", status="published")
        v2 = await _version(sf, pkg.id, version="2.0.0", content="diff", status="published")
        ch1, _ = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=1,
        )
        ch2, _ = await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v2.id,
            expected_channel_version=2,
        )
        await rollback_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            target_version_id=v1.id,
            expected_channel_version=3,
        )
        assert ch1.id == ch2.id  # same channel row, just bumped
        events = await list_events(sf, org_id=ORG_ID, channel_id=ch1.id)
        assert len(events) == 3
        actions = [e.action for e in events]
        assert actions == ["rollback", "promote", "promote"]  # newest-first

    async def test_list_events_scoped_to_org(self, sf):
        pkg_a = await _pkg(sf, org_id=ORG_ID, name="a")
        pkg_b = await _pkg(sf, org_id=OTHER_ORG_ID, name="b")
        v_a = await _version(sf, pkg_a.id, status="published")
        v_b = await _version(sf, pkg_b.id, org_id=OTHER_ORG_ID, status="published")
        await promote_channel(
            sf,
            org_id=ORG_ID,
            package_id=pkg_a.id,
            channel=CHANNEL_PROD,
            target_version_id=v_a.id,
            expected_channel_version=1,
        )
        ch_b, _ = await promote_channel(
            sf,
            org_id=OTHER_ORG_ID,
            package_id=pkg_b.id,
            channel=CHANNEL_PROD,
            target_version_id=v_b.id,
            expected_channel_version=1,
        )
        events_a = await list_events(sf, org_id=ORG_ID)
        events_b = await list_events(sf, org_id=OTHER_ORG_ID)
        assert all(e.org_id == ORG_ID for e in events_a)
        assert all(e.org_id == OTHER_ORG_ID for e in events_b)
        assert len(events_a) == 1
        assert len(events_b) == 1
        assert events_b[0].channel_id == ch_b.id


# ---------------------------------------------------------------------------
# Session passthrough (ART-750)
# ---------------------------------------------------------------------------


class TestSessionPassthrough:
    async def test_promote_stages_in_caller_session(self, sf):
        """When the caller passes an open session, the promote stages inside
        it without committing — caller commits atomically with the audit row."""
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="published")
        async with sf() as session:
            ch, ev = await promote_channel(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                channel=CHANNEL_PROD,
                target_version_id=ver.id,
                expected_channel_version=1,
                actor_id="u-test",
                session=session,
            )
            # Stage an audit-outbox row in the SAME session to prove same-tx.
            from deerflow.contracts.identity import PrincipalRef
            from deerflow.contracts.policy import ResourceRef
            from deerflow.persistence.audit import enqueue_audit_outbox_in_session
            from deerflow.tenancy.audit_events import build_audit_event

            event = build_audit_event(
                "release.agent.published",
                org_id=ORG_ID,
                actor=PrincipalRef(type="user", id="u-test", user_id="u-test"),
                outcome="success",
                resource=ResourceRef(type="release_channel", id=ch.id, org_id=ORG_ID),
                payload={"channel_id": ch.id},
            )
            await enqueue_audit_outbox_in_session(session, event)
            await session.commit()
        # Both rows durable post-commit.
        ch_after = await get_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD)
        assert ch_after is not None
        assert ch_after.current_version_id == ver.id
        events = await list_events(sf, org_id=ORG_ID, channel_id=ch.id)
        assert len(events) == 1
