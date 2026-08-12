"""MemoryThreadMetaStore visibility tests — Contract semantics (PR-025D).

After PR-025D (Contract), the storage layer no longer filters on ``user_id``
— ``org_id`` is the sole tenant boundary. These tests verify the Contract
property for the in-memory backend: within a single org, threads written
by one user are visible to another user at the store layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from deerflow.contracts import (
    PrincipalRef,
    TenantContext,
    bind_tenant_context,
    reset_tenant_context,
)
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.runtime.user_context import reset_current_user, set_current_user

USER_A = SimpleNamespace(id="user-a", email="a@test.local")
USER_B = SimpleNamespace(id="user-b", email="b@test.local")


def _as_user(user):
    """Bind the user + a default-org tenant."""

    class _Ctx:
        def __enter__(self):
            self._token = set_current_user(user)
            self._tenant_token = bind_tenant_context(
                TenantContext(
                    org_id="default",
                    principal=PrincipalRef(id=str(user.id), type="user", user_id=str(user.id)),
                    auth_method="session",
                    request_id=f"memory-iso-{user.id}",
                    issued_at=datetime.now(UTC),
                )
            )
            return user

        def __exit__(self, *exc):
            reset_tenant_context(self._tenant_token)
            reset_current_user(self._token)

    return _Ctx()


@pytest.fixture
def store():
    return MemoryThreadMetaStore(InMemoryStore())


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_same_org_cross_user_visible(store):
    """Contract: same-org threads are visible across users at the store layer."""
    with _as_user(USER_A):
        await store.create("t-alpha", display_name="A's thread")
    with _as_user(USER_B):
        await store.create("t-beta", display_name="B's thread")

    # Both users see both threads (same org, no user filter).
    with _as_user(USER_A):
        results = await store.search()
        ids = {r["thread_id"] for r in results}
        assert ids == {"t-alpha", "t-beta"}

    with _as_user(USER_B):
        row = await store.get("t-alpha")
        assert row is not None
        assert row["display_name"] == "A's thread"


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_check_access_still_enforces_owner(store):
    """check_access is the HTTP-layer owner gate — it still distinguishes users."""
    with _as_user(USER_A):
        await store.create("t-alpha", display_name="A's thread")

    with _as_user(USER_B):
        assert await store.check_access("t-alpha", "user-b") is False

    with _as_user(USER_A):
        assert await store.check_access("t-alpha", "user-a") is True


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_same_org_mutation_visible(store):
    """Contract: mutations within the same org are visible to all org members."""
    with _as_user(USER_A):
        await store.create("t-alpha", display_name="original")

    with _as_user(USER_B):
        # Same org — mutation succeeds (store no longer filters by user).
        await store.update_display_name("t-alpha", "updated by B")

    with _as_user(USER_A):
        row = await store.get("t-alpha")
        assert row is not None
        assert row["display_name"] == "updated by B"


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_no_tenant_context_raises(store):
    """Calling methods without tenant context raises RuntimeError (fail-closed)."""
    with pytest.raises(RuntimeError):
        await store.search()


@pytest.mark.anyio
@pytest.mark.no_auto_user
async def test_org_none_bypasses_filter(store):
    """org_id=None bypasses the org filter (migration/CLI escape hatch)."""
    with _as_user(USER_A):
        await store.create("t-alpha")
    with _as_user(USER_B):
        await store.create("t-beta")

    all_rows = await store.search(org_id=None)
    assert {r["thread_id"] for r in all_rows} == {"t-alpha", "t-beta"}

    row = await store.get("t-alpha", org_id=None)
    assert row is not None
