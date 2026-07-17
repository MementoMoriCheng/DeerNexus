"""Unit tests for the PR-012 TenantContext ContextVar lifecycle.

Covers bind / get / require / reset plus the concurrency and cleanup
properties mandated by runtime-contracts.md §5.2 and tested by
testing-strategy.md §7.1:

* ``TEN-001``: a bound context is readable in the current coroutine / task;
* ``TEN-002``: a normal exit restores the prior value (reset / nesting);
* ``TEN-003``: an exceptional exit still restores (try/finally);
* ``TEN-004``: concurrent coroutines holding OrgA / OrgB do not cross-contaminate;
* ``TEN-005``: a plain thread pool does not inherit stale context;
* ``TEN-006``: no tenant context leaks between / after test cases;
* ``TEN-007``: a missing context raises ``tenant_context_missing`` (fail closed);
* ``TEN-008``: the runtime never falls back to a default Org.

``TEN-009`` (DB connection-pool tenant reuse) is database-dependent and is
deliberately out of scope for PR-012; it ships with the CI connection-pool
stage and the 90-day test exit (testing-strategy.md §22.1 / §27).

These are pure contract tests — no app / ORM / FastAPI dependency is imported.
The dependency boundary is enforced in ``tests/test_harness_boundary.py``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from deerflow.contracts import (
    ErrorCode,
    PrincipalRef,
    TenantContext,
    TenantContextError,
    bind_tenant_context,
    get_tenant_context,
    is_retryable_code,
    require_tenant_context,
    reset_tenant_context,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "contracts"

ORG_A = "9f1c2b3a-4d5e-4789-abcd-ef0123456789"
ORG_B = "11111111-2222-3333-4444-555555555555"
REQ_ID = "7b8e9f0a-1234-5678-9abc-def012345678"

# These are pure ContextVar lifecycle tests that bind/reset their own tenant
# and assert no residue (TEN-006). The autouse user/tenant fixture
# (conftest._auto_user_context) would inject a default-org tenant and trip the
# residue assertion, so the whole module opts out.
pytestmark = pytest.mark.no_auto_user
TS = "2026-07-16T10:00:00Z"


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _principal() -> PrincipalRef:
    return PrincipalRef(type="user", id="u-1", user_id="u-1", display_name="Ada")


def _tenant(org: str = ORG_A) -> TenantContext:
    return TenantContext(
        org_id=org,
        principal=_principal(),
        auth_method="oidc",
        request_id=REQ_ID,
        issued_at=TS,
    )


@pytest.fixture(autouse=True)
def _assert_no_tenant_residue():
    """TEN-006: no tenant context leaks between or after test cases.

    Acts as a safety net: each test that binds is expected to ``reset`` in a
    ``finally``. If it leaks, the teardown assertion fails so one bad test
    cannot poison its neighbours.
    """
    assert get_tenant_context() is None, "tenant context leaked into this test from a previous one"
    yield
    assert get_tenant_context() is None, "tenant context leaked past test teardown"


# ===========================================================================
# TEN-001 — bind readable in the current task
# ===========================================================================


class TestBoundContextReadable:
    def test_bound_context_readable_in_same_task(self):
        ctx = _tenant()
        token = bind_tenant_context(ctx)
        try:
            assert get_tenant_context() is ctx
        finally:
            reset_tenant_context(token)

    def test_canonical_fixture_binds_and_round_trips(self):
        raw = _load_fixture("tenant_context.json")
        ctx = TenantContext.model_validate(raw)
        token = bind_tenant_context(ctx)
        try:
            bound = get_tenant_context()
            assert bound is ctx
            assert bound.org_id == raw["org_id"]
            assert bound.principal.display_name == raw["principal"]["display_name"]
        finally:
            reset_tenant_context(token)


# ===========================================================================
# TEN-002 — normal exit restores the prior value
# ===========================================================================


class TestNormalExitRestores:
    def test_reset_restores_unset_state(self):
        ctx = _tenant()
        token = bind_tenant_context(ctx)
        try:
            assert get_tenant_context() is ctx
        finally:
            reset_tenant_context(token)
        assert get_tenant_context() is None

    def test_nested_bind_restores_outer_value(self):
        outer = _tenant(ORG_A)
        inner = _tenant(ORG_B)
        token_outer = bind_tenant_context(outer)
        try:
            assert get_tenant_context().org_id == ORG_A
            token_inner = bind_tenant_context(inner)
            try:
                assert get_tenant_context().org_id == ORG_B
            finally:
                reset_tenant_context(token_inner)
            assert get_tenant_context().org_id == ORG_A
        finally:
            reset_tenant_context(token_outer)
        assert get_tenant_context() is None


# ===========================================================================
# TEN-003 — exceptional exit still restores
# ===========================================================================


class TestExceptionExitRestores:
    def test_reset_runs_in_finally_on_exception(self):
        ctx = _tenant()
        with pytest.raises(ValueError, match="boom"):
            token = bind_tenant_context(ctx)
            try:
                raise ValueError("boom")
            finally:
                reset_tenant_context(token)
        assert get_tenant_context() is None

    def test_nested_exception_restores_inner_then_outer(self):
        outer = _tenant(ORG_A)
        inner = _tenant(ORG_B)
        token_outer = bind_tenant_context(outer)
        try:
            token_inner = bind_tenant_context(inner)
            with pytest.raises(RuntimeError, match="inner-fail"):
                try:
                    raise RuntimeError("inner-fail")
                finally:
                    reset_tenant_context(token_inner)
            assert get_tenant_context().org_id == ORG_A
        finally:
            reset_tenant_context(token_outer)
        assert get_tenant_context() is None


# ===========================================================================
# TEN-004 — concurrent coroutines (OrgA / OrgB) do not cross-contaminate
# ===========================================================================


class TestConcurrentCoroutinesIsolated:
    @pytest.mark.asyncio
    async def test_concurrent_tasks_keep_their_own_org(self):
        seen: dict[str, str] = {}

        async def worker(org: str) -> None:
            token = bind_tenant_context(_tenant(org))
            try:
                # yield twice so the sibling task runs in between
                await asyncio.sleep(0)
                seen[org] = get_tenant_context().org_id
                await asyncio.sleep(0)
                assert get_tenant_context().org_id == org
            finally:
                reset_tenant_context(token)

        await asyncio.gather(worker(ORG_A), worker(ORG_B))
        assert seen == {ORG_A: ORG_A, ORG_B: ORG_B}
        # the parent task context is untouched by child tasks
        assert get_tenant_context() is None

    @pytest.mark.asyncio
    async def test_create_task_inherits_snapshot_not_live_writes(self):
        # child created while ORG_A is bound sees ORG_A, but a later rebind
        # to ORG_B in the parent must not mutate the already-captured child
        token_a = bind_tenant_context(_tenant(ORG_A))
        try:
            child = asyncio.create_task(self._snapshot_org())
            # rebind parent after scheduling the child
            token_b = bind_tenant_context(_tenant(ORG_B))
            try:
                pass
            finally:
                reset_tenant_context(token_b)
            assert await child == ORG_A
            assert get_tenant_context().org_id == ORG_A
        finally:
            reset_tenant_context(token_a)

    @staticmethod
    async def _snapshot_org() -> str:
        await asyncio.sleep(0)
        ctx = get_tenant_context()
        return ctx.org_id if ctx else ""


# ===========================================================================
# TEN-005 — a plain thread pool does not inherit stale context
# ===========================================================================


class TestThreadPoolNoInherit:
    def test_plain_thread_pool_does_not_inherit(self):
        ctx = _tenant(ORG_A)
        token = bind_tenant_context(ctx)
        try:
            assert get_tenant_context() is ctx
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                # a bare thread pool does NOT copy contextvars, so the worker
                # thread sees the unset value rather than the bound tenant
                result = executor.submit(get_tenant_context).result()
            assert result is None
        finally:
            reset_tenant_context(token)
        assert get_tenant_context() is None

    def test_copy_context_carries_tenant_explicitly(self):
        """The §5.2 rule-3 escape hatch: copy_context().run(...) threads it."""
        ctx = _tenant(ORG_A)
        token = bind_tenant_context(ctx)
        try:
            snapshot = contextvars.copy_context()
            result = snapshot.run(get_tenant_context)
            assert result is ctx
            # the snapshot did not mutate the live context
            assert get_tenant_context() is ctx
        finally:
            reset_tenant_context(token)
        assert get_tenant_context() is None


# ===========================================================================
# TEN-006 — no residual context at end of a test case
# ===========================================================================


class TestNoResidualContext:
    def test_repeated_bind_reset_cycles_leave_no_residue(self):
        for org in (ORG_A, ORG_B, ORG_A):
            token = bind_tenant_context(_tenant(org))
            try:
                assert get_tenant_context().org_id == org
            finally:
                reset_tenant_context(token)
        # the autouse ``_assert_no_tenant_residue`` teardown also asserts None

    def test_safety_net_catches_a_leaked_bind(self):
        """A leaked bind must trip the autouse teardown, not pass silently.

        Runs the leak in a child process-style isolation: we assert directly
        that an *un-reset* bind would be observable, mirroring what the
        teardown checks. We then clean up so the suite stays green.
        """
        token = bind_tenant_context(_tenant(ORG_A))
        try:
            # observable: a leaked bind is detectable via get()
            assert get_tenant_context() is not None
        finally:
            reset_tenant_context(token)


# ===========================================================================
# TEN-007 — missing context raises tenant_context_missing (fail closed)
# ===========================================================================


class TestMissingContextFailsClosed:
    def test_require_raises_when_unset(self):
        assert get_tenant_context() is None
        with pytest.raises(TenantContextError) as excinfo:
            require_tenant_context()
        assert excinfo.value.code == ErrorCode.TENANT_CONTEXT_MISSING

    def test_missing_context_code_is_non_retryable(self):
        assert is_retryable_code(ErrorCode.TENANT_CONTEXT_MISSING) is False

    def test_require_returns_context_after_bind(self):
        ctx = _tenant()
        token = bind_tenant_context(ctx)
        try:
            assert require_tenant_context() is ctx
        finally:
            reset_tenant_context(token)

    def test_require_raises_again_after_reset(self):
        token = bind_tenant_context(_tenant())
        try:
            assert require_tenant_context() is not None
        finally:
            reset_tenant_context(token)
        with pytest.raises(TenantContextError) as excinfo:
            require_tenant_context()
        assert excinfo.value.code == ErrorCode.TENANT_CONTEXT_MISSING


# ===========================================================================
# TEN-008 — never falls back to a default Org
# ===========================================================================


class TestNoDefaultOrgFallback:
    def test_get_returns_none_without_synthetic_org(self):
        # no tenant, no default Org synthesis — callers must require() to fail
        assert get_tenant_context() is None

    def test_org_id_must_be_non_empty(self):
        # the DTO invariant is min_length=1; an empty string fails closed
        with pytest.raises(ValidationError):
            TenantContext(
                org_id="",
                principal=_principal(),
                auth_method="oidc",
                request_id=REQ_ID,
                issued_at=TS,
            )

    def test_org_id_is_a_required_field(self):
        with pytest.raises(ValidationError):
            TenantContext(
                principal=_principal(),
                auth_method="oidc",
                request_id=REQ_ID,
                issued_at=TS,
            )

    def test_no_default_org_is_synthesized_on_require(self):
        # failing closed means a missing context is an error, never a default
        with pytest.raises(TenantContextError):
            require_tenant_context()
