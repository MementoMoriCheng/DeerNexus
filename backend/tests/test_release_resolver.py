"""Tests for the DB-backed ``DbReleaseResolver`` adapter (PR-054).

Covers the ADR-0004 §6 read-side resolution chain + the §9.2 prod gate +
inline digest verification + the failure codes. Builds the full stack
(Package → Version → Channel via PR-052/053 repository functions) against an
isolated SQLite, then drives the resolver through ``TenantContext``.

Resolver IDs: ``ART-1000`` series.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from app.gateway.release_resolver import (
    CODE_RELEASE_NOT_FOUND,
    CODE_RELEASE_NOT_PUBLISHED,
    CODE_RELEASE_REVOKED,
    CODE_RELEASE_TENANT_MISMATCH,
    DbReleaseResolver,
    ReleaseResolutionError,
)
from deerflow.contracts.context import TenantContext
from deerflow.contracts.identity import PrincipalRef
from deerflow.persistence.release import (
    CHANNEL_DEV,
    CHANNEL_PROD,
    CHANNEL_STAGING,
    create_agent_package,
    create_agent_version,
    promote_channel,
    set_version_status,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'resolver.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


def _tenant(*, org_id: str = ORG_ID, workspace_id: str | None = None, user_id: str = "u-test") -> TenantContext:
    return TenantContext(
        org_id=org_id,
        workspace_id=workspace_id,
        principal=PrincipalRef(type="user", id=user_id, user_id=user_id),
        auth_method="oidc",
        request_id="req-test",
        issued_at=datetime.now(UTC),
    )


async def _pkg(sf, *, org_id: str = ORG_ID, name: str = "alpha"):
    return await create_agent_package(sf, org_id=org_id, name=name, display_name=name)


async def _version(
    sf,
    pkg_id: str,
    *,
    org_id: str = ORG_ID,
    version: str = "1.0.0",
    content: str = "artifact-bytes",
    status: str = "draft",
):
    ver = await create_agent_version(
        sf,
        org_id=org_id,
        package_id=pkg_id,
        version=version,
        manifest={"schema_version": "1.0", "agent_entry": "soul"},
        content=content,
    )
    if status != "draft":
        await set_version_status(sf, version_id=ver.id, org_id=org_id, status=status)
    return ver


async def _promote(sf, *, org_id: str, package_id: str, channel: str, version_id: str, expected: int = 1):
    return await promote_channel(
        sf,
        org_id=org_id,
        package_id=package_id,
        channel=channel,
        target_version_id=version_id,
        expected_channel_version=expected,
    )


# ---------------------------------------------------------------------------
# Protocol conformance (ART-1000)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    async def test_db_resolver_satisfies_protocol(self, sf):
        """DbReleaseResolver structurally satisfies ReleaseResolver.

        Mirrors ``test_release_resolver_protocol_satisfied_by_duck_type`` but
        with the real adapter: a runtime check that the class has the async
        ``resolve`` method with the right signature shape. (Protocol with
        async method → ``isinstance`` against it is not meaningful at runtime
        without ``@runtime_checkable``, so this is a structural smoke check.)
        """
        resolver = DbReleaseResolver(sf)
        assert hasattr(resolver, "resolve")
        # The Protocol's resolve is async — confirm the adapter's is too.
        import inspect

        assert inspect.iscoroutinefunction(resolver.resolve)

    async def test_fake_async_resolver_also_satisfies_protocol(self):
        """A duck-typed async resolver also satisfies the (async) Protocol."""

        class _Fake:
            async def resolve(self, tenant, agent_name, channel):  # noqa: ARG002
                from deerflow.contracts.release import ReleaseRef

                return ReleaseRef(
                    org_id="o",
                    package_id="p",
                    version_id="v",
                    agent_name="a",
                    version="1.0.0",
                    digest="sha256:x",
                    channel="dev",
                    resolved_at=datetime.now(UTC),
                )

        # Structural satisfaction: the fake has the same async resolve shape.
        fake = _Fake()
        import inspect

        assert inspect.iscoroutinefunction(fake.resolve)


# ---------------------------------------------------------------------------
# Successful resolution (ART-1010)
# ---------------------------------------------------------------------------


class TestResolveSuccess:
    async def test_prod_resolution_returns_full_release_ref(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="published")
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD, version_id=ver.id)
        resolver = DbReleaseResolver(sf)
        ref = await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert ref.org_id == ORG_ID
        assert ref.workspace_id is None
        assert ref.package_id == pkg.id
        assert ref.version_id == ver.id
        assert ref.agent_name == "alpha"
        assert ref.version == "1.0.0"
        assert ref.digest == ver.digest
        assert ref.channel == "prod"
        assert ref.resolved_at.tzinfo is not None

    async def test_dev_channel_skips_prod_gate(self, sf):
        """dev allows draft/reviewed/published — no prod gate enforced."""
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="draft")  # draft, not published
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_DEV, version_id=ver.id)
        resolver = DbReleaseResolver(sf)
        ref = await resolver.resolve(_tenant(), agent_name="alpha", channel="dev")
        assert ref.digest == ver.digest

    async def test_staging_channel_skips_prod_gate(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="reviewed")
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_STAGING, version_id=ver.id)
        resolver = DbReleaseResolver(sf)
        ref = await resolver.resolve(_tenant(), agent_name="alpha", channel="staging")
        assert ref.digest == ver.digest


# ---------------------------------------------------------------------------
# Failure codes (ART-1020)
# ---------------------------------------------------------------------------


class TestResolveFailures:
    async def test_tenant_mismatch_when_org_id_empty(self, sf):
        """When the tenant has no bound org_id, the resolver fails with
        release_tenant_mismatch. ``TenantContext.org_id`` is min_length=1, so
        a genuinely-empty org cannot be a valid TenantContext — use a minimal
        stub object to exercise the resolver's ``getattr`` guard."""
        from types import SimpleNamespace

        empty_tenant = SimpleNamespace(org_id=None, workspace_id=None)
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(empty_tenant, agent_name="alpha", channel="dev")
        assert exc_info.value.code == CODE_RELEASE_TENANT_MISMATCH

    async def test_not_found_when_package_absent(self, sf):
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(), agent_name="ghost", channel="dev")
        assert exc_info.value.code == CODE_RELEASE_NOT_FOUND

    async def test_not_found_when_channel_absent(self, sf):
        pkg = await _pkg(sf)
        await _version(sf, pkg.id, status="published")
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert exc_info.value.code == CODE_RELEASE_NOT_FOUND

    async def test_not_found_when_channel_empty(self, sf):
        """get_or_create implicit-create path: channel row exists with NULL
        current_version_id (created but nothing promoted yet)."""
        from deerflow.persistence.release import get_or_create_channel

        pkg = await _pkg(sf)
        await _version(sf, pkg.id, status="published")
        await get_or_create_channel(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD)
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert exc_info.value.code == CODE_RELEASE_NOT_FOUND

    async def test_not_published_when_prod_points_at_draft(self, sf):
        """prod gate: draft/reviewed → release_not_published."""
        pkg = await _pkg(sf)
        # Publish then revoke to get a revoked version — but for the
        # not_published test, promote a draft directly to prod (the promote
        # gate would normally block this; bypass via a dev promote then a
        # manual channel current_version_id update is messy, so instead
        # test not_published via reviewed on prod).
        ver = await _version(sf, pkg.id, status="reviewed")
        # Promote to dev first (legal), then manually point prod at the
        # reviewed version by promoting to prod — the prod gate in promote
        # would block reviewed. So seed the channel row directly.
        from deerflow.persistence.release.model import ReleaseChannelRow

        async with sf() as session:
            session.add(
                ReleaseChannelRow(
                    id="ch-manual",
                    org_id=ORG_ID,
                    workspace_id=None,
                    package_id=pkg.id,
                    channel=CHANNEL_PROD,
                    current_version_id=ver.id,
                    row_version=1,
                )
            )
            await session.commit()
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert exc_info.value.code == CODE_RELEASE_NOT_PUBLISHED

    async def test_revoked_when_prod_points_at_revoked(self, sf):
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="published")
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD, version_id=ver.id)
        # Revoke the version after promotion.
        await set_version_status(sf, version_id=ver.id, org_id=ORG_ID, status="revoked")
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert exc_info.value.code == CODE_RELEASE_REVOKED

    async def test_cross_org_not_found(self, sf):
        """Cross-Org: tenant A resolving B's agent_name → release_not_found."""
        pkg = await _pkg(sf, org_id=OTHER_ORG_ID, name="alpha")
        ver = await _version(sf, pkg.id, org_id=OTHER_ORG_ID, status="published")
        await _promote(
            sf,
            org_id=OTHER_ORG_ID,
            package_id=pkg.id,
            channel=CHANNEL_PROD,
            version_id=ver.id,
        )
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(org_id=ORG_ID), agent_name="alpha", channel="prod")
        assert exc_info.value.code == CODE_RELEASE_NOT_FOUND


# ---------------------------------------------------------------------------
# Inline digest verification (ART-1030) — ADR §9.2 prod gate subset
# ---------------------------------------------------------------------------


class TestInlineDigestCheck:
    async def test_digest_mismatch_corruption_hidden_as_not_found(self, sf):
        """Tamper the stored digest so the recomputed inline digest differs.
        The resolver existence-hides corruption as release_not_found (an
        unauthorised caller cannot distinguish "no such agent" from
        "tampered artifact")."""
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="published")
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD, version_id=ver.id)
        # Tamper: overwrite the digest column with a bogus value.
        from sqlalchemy import update as sa_update

        from deerflow.persistence.release.model import AgentVersionRow

        async with sf() as session:
            await session.execute(sa_update(AgentVersionRow).where(AgentVersionRow.id == ver.id).values(digest="sha256:deadbeef" + "0" * 56))
            await session.commit()
        resolver = DbReleaseResolver(sf)
        with pytest.raises(ReleaseResolutionError) as exc_info:
            await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert exc_info.value.code == CODE_RELEASE_NOT_FOUND

    async def test_digest_check_skipped_for_object_key_versions(self, sf):
        """When content_inline is NULL (object_key path), the digest check is
        skipped — S3 existence verification is a follow-up. The resolver
        still resolves (prod gate passes on status alone)."""
        pkg = await _pkg(sf)
        # Create a version with object_key (large content > inline threshold).
        big = "x" * (65536 + 1)
        ver = await _version(sf, pkg.id, content=big, status="published")
        # Confirm it took the object_key path.
        assert ver.object_key is not None
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD, version_id=ver.id)
        resolver = DbReleaseResolver(sf)
        ref = await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert ref.digest == ver.digest

    async def test_inline_digest_check_can_be_disabled(self, sf):
        """``inline_digest_check=False`` skips the recomputation (test/prod
        escape hatch for the object_key path or perf)."""
        pkg = await _pkg(sf)
        ver = await _version(sf, pkg.id, status="published")
        await _promote(sf, org_id=ORG_ID, package_id=pkg.id, channel=CHANNEL_PROD, version_id=ver.id)
        from sqlalchemy import update as sa_update

        from deerflow.persistence.release.model import AgentVersionRow

        async with sf() as session:
            await session.execute(sa_update(AgentVersionRow).where(AgentVersionRow.id == ver.id).values(digest="sha256:deadbeef" + "0" * 56))
            await session.commit()
        resolver = DbReleaseResolver(sf, inline_digest_check=False)
        # With the check disabled, the tampered digest is returned as-is.
        ref = await resolver.resolve(_tenant(), agent_name="alpha", channel="prod")
        assert ref.digest.startswith("sha256:deadbeef")
