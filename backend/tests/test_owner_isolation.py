"""Same-org cross-user visibility at the storage layer — Contract semantics.

Sister suite to ``test_org_isolation.py``.  After PR-025D (Contract), the
storage layer no longer filters on ``user_id`` — ``org_id`` is the sole
tenant boundary.  Cross-user isolation within a single org is enforced by
``check_access`` / RBAC ``@require_rbac(owner_check=True)`` at the HTTP
layer, not by repository WHERE clauses.

These tests verify the **Contract property**: within a single org, rows
written by one user are visible at the store layer to another user in the
same org (no store-level ``user_id`` filter).  This is intentional — the
safety property moved from defense-in-depth (store filter + HTTP gate) to
a single authoritative gate (RBAC owner_check).

The HTTP-layer isolation gate (``check_access`` → 404) is covered by the
RBAC matrix tests (``test_rbac_runtime_routers.py``) and the
``check_access`` unit tests in ``test_thread_meta_repo.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deerflow.runtime.user_context import (
    reset_current_user,
    set_current_user,
)

USER_A = SimpleNamespace(id="user-a", email="a@test.local")
USER_B = SimpleNamespace(id="user-b", email="b@test.local")


async def _make_engines(tmp_path):
    """Initialize the shared engine against a per-test SQLite DB.

    Returns a cleanup coroutine the caller should await at the end.
    """
    from deerflow.persistence.engine import close_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'isolation.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    # PR-024: repository writes stamp org_id from the bound tenant context; the
    # FK to organizations.id requires the default org row to exist.
    from conftest import seed_test_default_org

    await seed_test_default_org()
    return close_engine


def _as_user(user):
    """Bind the user (and matching default-org tenant) contextvars.

    These tests assert visibility *within* the default org across users, so the
    tenant always binds to the default org.
    """

    class _Ctx:
        def __enter__(self):
            from datetime import UTC, datetime

            from deerflow.contracts import (
                PrincipalRef,
                TenantContext,
                bind_tenant_context,
            )

            self._token = set_current_user(user)
            self._tenant_token = bind_tenant_context(
                TenantContext(
                    org_id="default",
                    principal=PrincipalRef(id=str(user.id), type="user", user_id=str(user.id)),
                    auth_method="session",
                    request_id=f"owner-isolation-{user.id}",
                    issued_at=datetime.now(UTC),
                )
            )
            return user

        def __exit__(self, *exc):
            from deerflow.contracts import reset_tenant_context

            reset_tenant_context(self._tenant_token)
            reset_current_user(self._token)

    return _Ctx()


# ── Contract: same-org cross-user visibility (no store-level user filter) ─


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_thread_meta_same_org_cross_user_visible(tmp_path):
    """Contract (PR-025D): within one org, store-layer reads are user-agnostic.

    User A writes a thread; User B (same org) can read it at the store layer.
    The HTTP-layer owner_check gate enforces isolation — not the store.
    """
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())

        with _as_user(USER_A):
            await repo.create("t-alpha", display_name="A's thread")

        with _as_user(USER_B):
            # Same org — store no longer filters by user_id (Contract).
            row = await repo.get("t-alpha")
            assert row is not None, "Same-org cross-user read should succeed at store layer (Contract)"
            assert row["display_name"] == "A's thread"

            # search returns all threads in the org, regardless of writer.
            results = await repo.search()
            thread_ids = {r["thread_id"] for r in results}
            assert "t-alpha" in thread_ids
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_thread_meta_check_access_still_enforces_owner(tmp_path):
    """check_access is the HTTP-layer owner gate — it still distinguishes users."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())

        with _as_user(USER_A):
            await repo.create("t-alpha", display_name="A's thread")

        with _as_user(USER_B):
            # check_access denies cross-user (even same-org) for an existing row.
            assert await repo.check_access("t-alpha", "user-b") is False

        with _as_user(USER_A):
            assert await repo.check_access("t-alpha", "user-a") is True
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_runs_same_org_cross_user_visible(tmp_path):
    """Contract: same-org runs are visible across users at the store layer."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.run import RunRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = RunRepository(get_session_factory())

        with _as_user(USER_A):
            await repo.put("run-a1", thread_id="t-alpha")

        with _as_user(USER_B):
            row = await repo.get("run-a1")
            assert row is not None, "Same-org cross-user read should succeed (Contract)"
            assert row["run_id"] == "run-a1"

            runs = await repo.list_by_thread("t-alpha")
            assert any(r["run_id"] == "run-a1" for r in runs)
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_run_events_same_org_cross_user_visible(tmp_path):
    """Contract: same-org run_events are visible across users at the store layer.

    This is the most sensitive vector (raw conversation content) — the HTTP
    owner_check gate is the authoritative isolation mechanism.
    """
    from deerflow.persistence.engine import get_session_factory
    from deerflow.runtime.events.store.db import DbRunEventStore

    cleanup = await _make_engines(tmp_path)
    try:
        store = DbRunEventStore(get_session_factory())

        with _as_user(USER_A):
            await store.put(
                thread_id="t-alpha",
                run_id="run-a1",
                event_type="human_message",
                category="message",
                content="User A private question",
            )

        with _as_user(USER_B):
            msgs = await store.list_messages("t-alpha")
            assert len(msgs) == 1, "Same-org cross-user event read should succeed (Contract)"
    finally:
        await cleanup()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_feedback_same_org_cross_user_visible(tmp_path):
    """Contract: same-org feedback is visible across users at the store layer."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.feedback import FeedbackRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = FeedbackRepository(get_session_factory())

        with _as_user(USER_A):
            a_fb = await repo.create(run_id="run-a1", thread_id="t-alpha", rating=1)

        with _as_user(USER_B):
            row = await repo.get(a_fb["feedback_id"])
            assert row is not None, "Same-org cross-user read should succeed (Contract)"

            fbs = await repo.list_by_thread("t-alpha")
            assert len(fbs) == 1
    finally:
        await cleanup()


# ── Fail-closed: no user context still errors for stamp paths ──────────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_create_without_context_raises(tmp_path):
    """create() still needs user_id from contextvar for stamping."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())
        with pytest.raises(RuntimeError, match="no user context is set"):
            await repo.create("t-alpha")
    finally:
        await cleanup()


# ── Escape hatch: org_id=None bypasses org filter (migration/system) ────────


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_org_none_bypasses_filter(tmp_path):
    """Migration scripts pass org_id=None to see all orgs."""
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.thread_meta import ThreadMetaRepository

    cleanup = await _make_engines(tmp_path)
    try:
        repo = ThreadMetaRepository(get_session_factory())

        with _as_user(USER_A):
            await repo.create("t-alpha")

        # No tenant bound; explicit org_id=None bypasses the org filter.
        all_rows = await repo.search(org_id=None)
        assert {r["thread_id"] for r in all_rows} == {"t-alpha"}

        row = await repo.get("t-alpha", org_id=None)
        assert row is not None
    finally:
        await cleanup()
