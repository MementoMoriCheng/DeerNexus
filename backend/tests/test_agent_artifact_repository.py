"""DB CRUD + digest + storage + inventory tests for the agent-artifact write path (PR-052).

Covers :mod:`deerflow.persistence.release` end-to-end against an isolated
SQLite: the digest format, the ObjectStore abstraction, the repository
(CRUD, published-immutability, threshold routing, Org isolation, session
passthrough), and the inventory reconciler.

Fixture conventions mirror ``test_iam_service_account_repository.py``: boot
an isolated SQLite via ``init_engine``, yield ``get_session_factory()``,
tear down with ``close_engine``.

Artifact IDs: ``ART-300`` series (repository layer).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.release import (
    VERSION_DRAFT,
    VERSION_PUBLISHED,
    VERSION_REVIEWED,
    VERSION_REVOKED,
    IllegalVersionTransitionError,
    InlineObjectStore,
    VersionImmutableError,
    archive_agent_package,
    compute_artifact_digest,
    compute_object_key,
    create_agent_package,
    create_agent_version,
    get_agent_package,
    get_agent_version,
    get_agent_version_by_digest,
    list_agent_packages,
    list_agent_versions,
    set_version_status,
    update_agent_package,
    update_agent_version,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"

# Every test in this module is async (repository I/O) — apply anyio once at
# the module level rather than per-test.
pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'release_repo.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# Digest (ART-300)
# ---------------------------------------------------------------------------


class TestDigest:
    def test_digest_is_sha256_prefixed_hex(self):
        d = compute_artifact_digest("hello")
        assert d.startswith("sha256:")
        # sha256("hello") — well-known constant.
        assert d == "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    def test_digest_deterministic(self):
        assert compute_artifact_digest("content") == compute_artifact_digest("content")

    def test_digest_differs_for_different_content(self):
        assert compute_artifact_digest("a") != compute_artifact_digest("b")

    def test_digest_str_is_utf8_encoded(self):
        # str and its utf-8 bytes must produce the same digest.
        assert compute_artifact_digest("héllo") == compute_artifact_digest("héllo".encode())


# ---------------------------------------------------------------------------
# ObjectStore (ART-310)
# ---------------------------------------------------------------------------


class TestObjectStore:
    def test_object_key_shape(self):
        key = compute_object_key(org_id="org-1", workspace_id="ws-1", version_id="v-1")
        assert key == "org/org-1/workspace/ws-1/agent-version/v-1/artifact"

    def test_object_key_default_workspace_sentinel(self):
        key = compute_object_key(org_id="org-1", workspace_id=None, version_id="v-1")
        assert "/workspace/_default/" in key

    def test_inline_store_is_noop_durable(self):
        store = InlineObjectStore()
        store.put(object_key="k", content=b"data")  # must not raise
        assert store.exists("k") is True  # inline artifact always present
        assert store.get("k") == b""  # content lives in the row, not here
        store.delete("k")  # idempotent no-op


# ---------------------------------------------------------------------------
# AgentPackage CRUD (ART-320)
# ---------------------------------------------------------------------------


class TestPackageRepository:
    async def test_create_and_get(self, sf):
        pkg = await create_agent_package(sf, org_id=ORG_ID, name="alpha", display_name="Alpha")
        assert pkg.status == "active"
        fetched = await get_agent_package(sf, package_id=pkg.id, org_id=ORG_ID)
        assert fetched is not None
        assert fetched.name == "alpha"

    async def test_unique_org_name(self, sf):
        await create_agent_package(sf, org_id=ORG_ID, name="dup", display_name="D")
        with pytest.raises(IntegrityError):
            await create_agent_package(sf, org_id=ORG_ID, name="dup", display_name="D2")

    async def test_same_name_different_org_allowed(self, sf):
        await create_agent_package(sf, org_id=ORG_ID, name="shared", display_name="A")
        await create_agent_package(sf, org_id=OTHER_ORG_ID, name="shared", display_name="B")

    async def test_cross_org_get_returns_none(self, sf):
        pkg = await create_agent_package(sf, org_id=ORG_ID, name="x", display_name="X")
        assert await get_agent_package(sf, package_id=pkg.id, org_id=OTHER_ORG_ID) is None

    async def test_list_excludes_archived_by_default(self, sf):
        await create_agent_package(sf, org_id=ORG_ID, name="a", display_name="A")
        p2 = await create_agent_package(sf, org_id=ORG_ID, name="b", display_name="B")
        await archive_agent_package(sf, package_id=p2.id, org_id=ORG_ID)
        names = {p.name for p in await list_agent_packages(sf, org_id=ORG_ID)}
        assert names == {"a"}
        archived = await list_agent_packages(sf, org_id=ORG_ID, include_archived=True)
        assert {p.name for p in archived} == {"a", "b"}

    async def test_update_mutable_fields(self, sf):
        pkg = await create_agent_package(sf, org_id=ORG_ID, name="a", display_name="A")
        updated = await update_agent_package(sf, package_id=pkg.id, org_id=ORG_ID, display_name="A2", description="d")
        assert updated.display_name == "A2"
        assert updated.description == "d"

    async def test_update_cross_org_returns_none(self, sf):
        pkg = await create_agent_package(sf, org_id=ORG_ID, name="a", display_name="A")
        assert await update_agent_package(sf, package_id=pkg.id, org_id=OTHER_ORG_ID, display_name="X") is None


# ---------------------------------------------------------------------------
# AgentVersion CRUD + published-immutability (ART-330)
# ---------------------------------------------------------------------------


class TestVersionRepository:
    async def _pkg(self, sf, name: str = "alpha") -> str:
        return (await create_agent_package(sf, org_id=ORG_ID, name=name, display_name=name)).id

    async def test_create_computes_digest_and_routes_inline(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={"agent_entry": "main"}, content="hello")
        assert v.digest == compute_artifact_digest("hello")
        assert v.size_bytes == 5
        assert v.content_inline == "hello"
        assert v.object_key is None
        assert v.status == VERSION_DRAFT

    async def test_large_content_routes_to_object_key(self, sf):
        pkg = await self._pkg(sf)
        big = "x" * 11
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={"a": 1}, content=big, inline_size_threshold=10)
        assert v.object_key is not None
        assert v.content_inline is None
        assert v.object_key.startswith(f"org/{ORG_ID}/workspace/_default/agent-version/{v.id}/artifact")

    async def test_get_by_digest(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="payload")
        found = await get_agent_version_by_digest(sf, org_id=ORG_ID, digest=v.digest)
        assert found is not None
        assert found.id == v.id

    async def test_unique_org_package_version(self, sf):
        pkg = await self._pkg(sf)
        await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        with pytest.raises(IntegrityError):
            await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="b")

    async def test_unique_org_digest(self, sf):
        pkg = await self._pkg(sf)
        await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="same")
        # Same content → same digest → collision even with a different version string.
        with pytest.raises(IntegrityError):
            await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="2.0.0", manifest={}, content="same")

    async def test_draft_is_mutable(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        updated = await update_agent_version(sf, version_id=v.id, org_id=ORG_ID, content="b")
        assert updated.digest == compute_artifact_digest("b")
        assert updated.content_inline == "b"

    async def test_published_is_immutable(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_PUBLISHED)
        with pytest.raises(VersionImmutableError):
            await update_agent_version(sf, version_id=v.id, org_id=ORG_ID, content="b")

    async def test_revoked_is_immutable(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_PUBLISHED)
        await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_REVOKED)
        with pytest.raises(VersionImmutableError):
            await update_agent_version(sf, version_id=v.id, org_id=ORG_ID, content="b")

    async def test_publish_stamps_published_at(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        published = await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_PUBLISHED)
        assert published.published_at is not None

    async def test_revoke_stamps_revoked_at(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_PUBLISHED)
        revoked = await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_REVOKED)
        assert revoked.revoked_at is not None

    async def test_illegal_transition_published_to_draft(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_PUBLISHED)
        with pytest.raises(IllegalVersionTransitionError):
            await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_DRAFT)

    async def test_legal_review_then_publish(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_REVIEWED)
        published = await set_version_status(sf, version_id=v.id, org_id=ORG_ID, status=VERSION_PUBLISHED)
        assert published.status == VERSION_PUBLISHED

    async def test_list_scoped_to_package(self, sf):
        p1 = await self._pkg(sf, "alpha")
        p2 = await self._pkg(sf, "beta")
        await create_agent_version(sf, org_id=ORG_ID, package_id=p1, version="1.0.0", manifest={}, content="a")
        await create_agent_version(sf, org_id=ORG_ID, package_id=p2, version="1.0.0", manifest={}, content="b")
        p1_versions = await list_agent_versions(sf, org_id=ORG_ID, package_id=p1)
        assert len(p1_versions) == 1
        assert p1_versions[0].package_id == p1

    async def test_cross_org_get_returns_none(self, sf):
        pkg = await self._pkg(sf)
        v = await create_agent_version(sf, org_id=ORG_ID, package_id=pkg, version="1.0.0", manifest={}, content="a")
        assert await get_agent_version(sf, version_id=v.id, org_id=OTHER_ORG_ID) is None


# ---------------------------------------------------------------------------
# Session passthrough (Class A same-transaction, ART-340)
# ---------------------------------------------------------------------------


class TestSessionPassthrough:
    async def test_create_version_in_caller_session_is_staged_not_committed(self, sf):
        """The session-passthrough path stages the row in the caller's
        transaction WITHOUT committing — the Class A guarantee that the
        business write and the audit-outbox row land (or roll back) together
        (ADR-0005 §7.1).

        Verified by rolling back the caller session: a self-committing call
        would have persisted the row; a passthrough call leaves it uncommitted
        so the rollback discards it.
        """
        from deerflow.persistence.release.model import AgentVersionRow

        pkg = await create_agent_package(sf, org_id=ORG_ID, name="a", display_name="A")
        async with sf() as session:
            v = await create_agent_version(
                sf,
                org_id=ORG_ID,
                package_id=pkg.id,
                version="1.0.0",
                manifest={},
                content="x",
                session=session,
            )
            await session.flush()
            staged = await session.get(AgentVersionRow, v.id)
            assert staged is not None  # visible inside the open transaction
            await session.rollback()  # discard — passthrough did not commit
        async with sf() as check:
            assert await check.get(AgentVersionRow, v.id) is None  # rolled back
