"""Cross-org isolation matrix — non-negotiable tenant boundary gate (PR-024).

Sister suite to ``test_owner_isolation.py``. Where that file proves the
*user* filter (users cannot see each other's rows within one org), this file
proves the hard *tenant* filter: a row written under OrgA's bound
``TenantContext`` must never be readable, mutable or deletable from OrgB's
context, and vice-versa. ``org_id`` is the hard isolation boundary
(runtime-contracts §5.2, data-model §11.2) — this is the property the
"OrgA cannot see OrgB" rollout question in pr-split-guide §3 reduces to.

These tests bypass the HTTP layer and exercise the storage-layer org filter
directly by binding two distinct ``TenantContext`` values. The safety property:

  After a repository write with org_id=OrgA, a subsequent read/mutation with
  org_id=OrgB must not return or affect the row, and vice-versa.

Every test opts out of the autouse contextvar fixture
(``@pytest.mark.no_auto_user``) so it can bind the specific tenant (and user)
it cares about. The same user is used in both org contexts so any leak here is
attributable purely to the org boundary, not the (already-tested) user filter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deerflow.contracts import (
    PrincipalRef,
    TenantContext,
    bind_tenant_context,
    reset_tenant_context,
)
from deerflow.runtime.user_context import (
    reset_current_user,
    set_current_user,
)

ORG_A = "org-a-isolation"
ORG_B = "org-b-isolation"
# Same user in both orgs so a leak is unambiguously an org-boundary failure.
SHARED_USER = SimpleNamespace(id="user-shared", email="shared@test.local")


def _tenant(org_id: str) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        principal=PrincipalRef(id="user-shared", type="user", user_id="user-shared"),
        auth_method="session",
        request_id=f"org-isolation-{org_id}",
        issued_at=datetime.now(UTC),
    )


def _as_org(org_id: str):
    """Bind the shared user + the given org's tenant contextvars."""

    class _Ctx:
        def __enter__(self):
            self._user_token = set_current_user(SHARED_USER)
            self._tenant_token = bind_tenant_context(_tenant(org_id))
            return org_id

        def __exit__(self, *exc):
            reset_tenant_context(self._tenant_token)
            reset_current_user(self._user_token)

    return _Ctx()


async def _make_engines(tmp_path):
    """Initialize the shared engine and seed BOTH org rows (FK parent)."""
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine
    from deerflow.persistence.orgs.model import OrganizationRow

    url = f"sqlite+aiosqlite:///{tmp_path / 'org_isolation.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    async with sf() as session:
        for org_id in (ORG_A, ORG_B):
            if await session.get(OrganizationRow, org_id) is None:
                session.add(OrganizationRow(id=org_id, slug=org_id, name=org_id, status="active"))
        await session.commit()
    return close_engine


# ── threads_meta org isolation ────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_thread_meta_cross_org_isolation(tmp_path):
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.create("t-alpha", display_name="OrgA thread")

        with _as_org(ORG_B):
            await repo.create("t-beta", display_name="OrgB thread")

        # OrgA sees only its own thread.
        with _as_org(ORG_A):
            assert (await repo.get("t-alpha")) is not None
            leaked = await repo.get("t-beta")
            assert leaked is None, "OrgA leaked OrgB's thread"
            assert [r["thread_id"] for r in await repo.search()] == ["t-alpha"]

        # OrgB sees only its own thread.
        with _as_org(ORG_B):
            assert (await repo.get("t-beta")) is not None
            leaked = await repo.get("t-alpha")
            assert leaked is None, "OrgB leaked OrgA's thread"
            assert [r["thread_id"] for r in await repo.search()] == ["t-beta"]
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_thread_meta_cross_org_mutation_denied(tmp_path):
    """OrgB cannot update/delete/check-access a thread owned by OrgA."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.create("t-alpha", display_name="original")

        with _as_org(ORG_B):
            # Mutations are no-ops against a cross-org row.
            await repo.update_display_name("t-alpha", "hijacked")
            await repo.update_status("t-alpha", "running")
            await repo.delete("t-alpha")
            # check_access denies a cross-org row even for the permissive mode.
            assert await repo.check_access("t-alpha", "user-shared") is False

        with _as_org(ORG_A):
            row = await repo.get("t-alpha")
            assert row is not None
            assert row["display_name"] == "original"
            assert row["status"] == "idle"
    finally:
        await cleanup()


# ── runs org isolation ────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_runs_cross_org_isolation(tmp_path):
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.run import RunRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = RunRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.put("run-a1", thread_id="t-alpha")
        with _as_org(ORG_B):
            await repo.put("run-b1", thread_id="t-beta")

        with _as_org(ORG_A):
            assert (await repo.get("run-a1")) is not None
            assert await repo.get("run-b1") is None, "OrgA leaked OrgB's run"
            assert [r["run_id"] for r in await repo.list_by_thread("t-alpha")] == ["run-a1"]
            assert await repo.list_by_thread("t-beta") == []

        with _as_org(ORG_B):
            assert (await repo.get("run-b1")) is not None
            assert await repo.get("run-a1") is None, "OrgB leaked OrgA's run"
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_runs_cross_org_delete_denied(tmp_path):
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.run import RunRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = RunRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.put("run-a1", thread_id="t-alpha")
        with _as_org(ORG_B):
            await repo.delete("run-a1")  # cross-org delete is a no-op

        with _as_org(ORG_A):
            assert await repo.get("run-a1") is not None
    finally:
        await cleanup()


# ── run_events org isolation ──────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_run_events_cross_org_isolation(tmp_path):
    """run_events holds raw conversation content — most sensitive leak vector."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.runtime.events.store.db import DbRunEventStore

    cleanup = await _make_engines(tmp_path)
    try:
        store = DbRunEventStore(get_session_factory())

        with _as_org(ORG_A):
            await store.put(thread_id="t-alpha", run_id="r-a", event_type="human_message", category="message", content="OrgA secret")

        with _as_org(ORG_B):
            await store.put(thread_id="t-beta", run_id="r-b", event_type="human_message", category="message", content="OrgB secret")

        with _as_org(ORG_A):
            msgs = await store.list_messages("t-alpha")
            assert len(msgs) == 1
            assert "OrgA" in msgs[0]["content"]
            # OrgA must not see OrgB's messages.
            assert await store.list_messages("t-beta") == []
            assert await store.count_messages("t-beta") == 0

        with _as_org(ORG_B):
            assert await store.list_messages("t-alpha") == []
            assert len(await store.list_messages("t-beta")) == 1
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_run_events_cross_org_delete_denied(tmp_path):
    from deerflow.persistence.engine import get_session_factory
    from deerflow.runtime.events.store.db import DbRunEventStore

    cleanup = await _make_engines(tmp_path)
    try:
        store = DbRunEventStore(get_session_factory())

        with _as_org(ORG_A):
            await store.put(thread_id="t-alpha", run_id="r-a", event_type="human_message", category="message", content="OrgA")
            assert await store.count_messages("t-alpha") == 1

        # OrgB cannot delete OrgA's events.
        with _as_org(ORG_B):
            deleted = await store.delete_by_thread("t-alpha")
            assert deleted == 0
            deleted = await store.delete_by_run("t-alpha", "r-a")
            assert deleted == 0

        with _as_org(ORG_A):
            assert await store.count_messages("t-alpha") == 1
    finally:
        await cleanup()


# ── feedback org isolation ────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_feedback_cross_org_isolation(tmp_path):
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.feedback import FeedbackRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = FeedbackRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.create(run_id="r-a", thread_id="t-alpha", rating=1)
        with _as_org(ORG_B):
            await repo.create(run_id="r-b", thread_id="t-beta", rating=-1)

        with _as_org(ORG_A):
            assert len(await repo.list_by_thread("t-alpha")) == 1
            assert await repo.list_by_thread("t-beta") == []
            grouped = await repo.list_by_thread_grouped("t-alpha")
            assert set(grouped) == {"r-a"}

        with _as_org(ORG_B):
            assert len(await repo.list_by_thread("t-beta")) == 1
            assert await repo.list_by_thread("t-alpha") == []
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_feedback_cross_org_delete_denied(tmp_path):
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.feedback import FeedbackRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = FeedbackRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.create(run_id="r-a", thread_id="t-alpha", rating=1)
            fb_id = (await repo.list_by_thread("t-alpha"))[0]["feedback_id"]

        with _as_org(ORG_B):
            # Cross-org get returns None; cross-org delete is a no-op.
            assert await repo.get(fb_id) is None
            assert await repo.delete(fb_id) is False
            assert await repo.delete_by_run(thread_id="t-alpha", run_id="r-a") is False

        with _as_org(ORG_A):
            assert await repo.get(fb_id) is not None
    finally:
        await cleanup()


# ── fail-closed: no tenant bound ⇒ hard error ─────────────────────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_repository_without_tenant_context_raises(tmp_path):
    """Fail-closed: a read with AUTO_ORG and no bound tenant errors (§5.2 rule 6)."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())
        # Bypass the user filter so the org resolver is the failing gate.
        # Neither contextvar is set under @pytest.mark.no_auto_user.
        with pytest.raises(RuntimeError, match="no tenant context is bound"):
            await repo.get("anything", user_id=None)
    finally:
        await cleanup()


# ── escape hatch: explicit org_id=None bypasses the org filter ────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_explicit_org_none_bypasses_filter(tmp_path):
    """System-admin / migration paths pass org_id=None to see all orgs."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())

        with _as_org(ORG_A):
            await repo.create("t-alpha")
        with _as_org(ORG_B):
            await repo.create("t-beta")

        # No tenant bound; explicit None bypasses both user and org filters.
        all_rows = await repo.search(user_id=None, org_id=None)
        assert {r["thread_id"] for r in all_rows} == {"t-alpha", "t-beta"}
    finally:
        await cleanup()
