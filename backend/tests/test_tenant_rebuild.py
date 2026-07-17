"""Tests for PR-014A: Worker tenant rebuild from RunEnvelope + defensive rebind.

Covers runtime-contracts.md §5.2 rule 4 (Worker rebuilds context from
RunEnvelope) and rule 3 (do not rely solely on ContextVar inheritance):

* rebuild_tenant_context / bind_tenant_from_envelope (app.gateway.tenant_rebuild);
* run_agent's defensive rebind from RunContext.tenant when the contextvar is
  unset (simulating a physical Worker or a failed inheritance);
* the no-mapping fail-closed gate (pr-split-guide line 244).

These tests follow the PR-012/PR-013 sibling conventions: an autouse
``_assert_no_tenant_residue`` fixture, ``_tenant`` / ``_run_envelope`` builders,
and per-test ``@pytest.mark.asyncio`` for the async cases.

Test IDs (``TEN-入口`` Worker family, threat-model TM-001 / TM-024).
"""

import asyncio

import pytest

from app.gateway.tenant_rebuild import bind_tenant_from_envelope, rebuild_tenant_context
from deerflow.contracts import (
    ErrorCode,
    PrincipalRef,
    ReleaseRef,
    RunEnvelope,
    TenantContext,
    TenantContextError,
    bind_tenant_context,
    get_tenant_context,
    reset_tenant_context,
)
from deerflow.runtime.runs.manager import RunRecord
from deerflow.runtime.runs.schemas import DisconnectMode, RunStatus
from deerflow.runtime.runs.worker import RunContext, run_agent

ORG_A = "9f1c2b3a-4d5e-4789-abcd-ef0123456789"
ORG_B = "11111111-2222-3333-4444-555555555555"
REQ_ID = "7b8e9f0a-1234-5678-9abc-def012345678"
TS = "2026-07-16T10:00:00Z"

# These tests manage their own TenantContext bind/reset and assert no residue
# (TEN-006). The autouse user/tenant fixture (conftest._auto_user_context) would
# inject a default-org tenant and trip the residue assertion, so the whole
# module opts out.
pytestmark = pytest.mark.no_auto_user


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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


def _release_ref(org: str = ORG_A) -> ReleaseRef:
    return ReleaseRef(
        org_id=org,
        package_id="pkg-1",
        agent_name="demo",
        version="1.0.0",
        digest="sha256:abcdef",
        channel="dev",
        resolved_at=TS,
    )


def _policy_snapshot():
    from deerflow.contracts import PolicySnapshotRef

    return PolicySnapshotRef(policy_version="2026-07-15-01", evaluated_at=TS)


def _run_envelope(*, tenant: TenantContext | None = None, source: str = "api") -> RunEnvelope:
    t = tenant if tenant is not None else _tenant()
    return RunEnvelope(
        run_id="run-1",
        thread_id="th-1",
        tenant=t,
        release_ref=_release_ref(t.org_id),
        policy_snapshot=_policy_snapshot(),
        created_at=TS,
        idempotency_key="idem-1",
        source=source,
    )


@pytest.fixture(autouse=True)
def _assert_no_tenant_residue():
    """No tenant context leaks between / after test cases (TEN-006)."""
    assert get_tenant_context() is None, "tenant context leaked into this test from a previous one"
    yield
    assert get_tenant_context() is None, "tenant context leaked past test teardown"


# ===========================================================================
# rebuild_tenant_context / bind_tenant_from_envelope
# ===========================================================================


class TestRebuildFromEnvelope:
    def test_rebuild_returns_envelope_tenant(self):
        envelope = _run_envelope()
        rebuilt = rebuild_tenant_context(envelope)
        assert rebuilt is envelope.tenant
        assert rebuilt.org_id == ORG_A

    def test_bind_from_envelope_makes_context_readable(self):
        envelope = _run_envelope(tenant=_tenant(ORG_B))
        token = bind_tenant_from_envelope(envelope)
        try:
            assert get_tenant_context() is envelope.tenant
            assert get_tenant_context().org_id == ORG_B
        finally:
            reset_tenant_context(token)

    def test_rebuilt_tenant_org_comes_from_envelope_not_default(self):
        """The rebuilt org_id is the envelope's, never a synthesized default."""
        envelope = _run_envelope(tenant=_tenant(ORG_A))
        rebuilt = rebuild_tenant_context(envelope)
        assert rebuilt.org_id == ORG_A


# ===========================================================================
# No-mapping fail-closed (pr-split-guide line 244)
# ===========================================================================


class TestNoMappingFailClosed:
    def test_none_envelope_raises_tenant_context_missing(self):
        with pytest.raises(TenantContextError) as excinfo:
            rebuild_tenant_context(None)  # type: ignore[arg-type]
        assert excinfo.value.code == ErrorCode.TENANT_CONTEXT_MISSING


# ===========================================================================
# Worker defensive rebind from RunContext.tenant
# ===========================================================================


class _FakeAgent:
    """Minimal graph whose astream captures whether a tenant was bound."""

    def __init__(self) -> None:
        self.bound_org: str | None = None
        self.checkpointer = None
        self.store = None
        self.interrupt_before_nodes: list[str] = []
        self.interrupt_after_nodes: list[str] = []

    async def astream(self, graph_input, *, config, stream_mode, **kwargs):
        ctx = get_tenant_context()
        self.bound_org = ctx.org_id if ctx is not None else None
        if False:  # pragma: no cover
            yield  # make this an async generator that produces nothing


class _FakeRunManager:
    async def set_status(self, *_args, **_kwargs) -> None:
        return None

    async def update_model_name(self, *_args, **_kwargs) -> None:
        return None

    async def update_run_completion(self, *_args, **_kwargs) -> None:
        return None


class _FakeBridge:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def publish(self, _run_id, event, payload) -> None:
        self.events.append((event, payload))

    async def publish_end(self, _run_id) -> None:
        self.events.append(("end", None))

    async def cleanup(self, _run_id, *, delay: int = 0) -> None:
        return None


def _record() -> RunRecord:
    record = RunRecord(
        run_id="run-1",
        thread_id="thread-xyz",
        assistant_id="lead-agent",
        status=RunStatus.pending,
        on_disconnect=DisconnectMode.cancel,
        model_name="gpt-4o",
    )
    record.abort_event = asyncio.Event()
    return record


@pytest.mark.no_auto_user
class TestWorkerDefensiveRebind:
    """run_agent rebinds the tenant from RunContext.tenant when the contextvar
    is unset (§5.2 rule 3/4) — simulating a physical Worker or failed
    ContextVar inheritance across create_task."""

    @pytest.mark.asyncio
    async def test_worker_rebinds_tenant_when_contextvar_unset(self):
        # contextvar is unset (no_auto_user + no explicit bind)
        assert get_tenant_context() is None
        fake_agent = _FakeAgent()

        ctx = RunContext(checkpointer=None, tenant=_tenant(ORG_A))

        await run_agent(
            _FakeBridge(),
            _FakeRunManager(),
            _record(),
            ctx=ctx,
            agent_factory=lambda config: fake_agent,
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "thread-xyz"}},
        )

        assert fake_agent.bound_org == ORG_A, "Worker should have rebound the tenant defensively"
        # contextvar restored after run
        assert get_tenant_context() is None

    @pytest.mark.asyncio
    async def test_worker_does_not_rebind_when_contextvar_already_set(self):
        """If the contextvar is already set (normal create_task inheritance),
        the Worker does not clobber it — it trusts the inherited scope."""
        inherited = _tenant(ORG_B)
        token = bind_tenant_context(inherited)
        try:
            fake_agent = _FakeAgent()
            # RunContext carries ORG_A but the live contextvar is ORG_B
            ctx = RunContext(checkpointer=None, tenant=_tenant(ORG_A))

            await run_agent(
                _FakeBridge(),
                _FakeRunManager(),
                _record(),
                ctx=ctx,
                agent_factory=lambda config: fake_agent,
                graph_input={"messages": []},
                config={"configurable": {"thread_id": "thread-xyz"}},
            )

            # inherited scope (ORG_B) wins; RunContext.tenant does not clobber
            assert fake_agent.bound_org == ORG_B
        finally:
            reset_tenant_context(token)

    @pytest.mark.asyncio
    async def test_worker_without_tenant_runs_unscoped(self):
        """When neither contextvar nor RunContext.tenant is set, the Worker runs
        without a tenant scope (no defensive bind, no error) — the run itself is
        not tenant-gated at this layer."""
        assert get_tenant_context() is None
        fake_agent = _FakeAgent()
        ctx = RunContext(checkpointer=None)  # tenant=None

        await run_agent(
            _FakeBridge(),
            _FakeRunManager(),
            _record(),
            ctx=ctx,
            agent_factory=lambda config: fake_agent,
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "thread-xyz"}},
        )

        assert fake_agent.bound_org is None

    @pytest.mark.asyncio
    async def test_worker_tenant_restored_after_exception(self):
        """If the run raises, the defensively-bound tenant is still restored."""
        assert get_tenant_context() is None

        class _FailingAgent(_FakeAgent):
            async def astream(self, graph_input, *, config, stream_mode, **kwargs):
                raise RuntimeError("agent failed")
                if False:  # pragma: no cover
                    yield  # async generator

        ctx = RunContext(checkpointer=None, tenant=_tenant(ORG_A))

        # run_agent swallows agent exceptions (publishes an error event), so it
        # completes normally — the tenant must still be restored.
        await run_agent(
            _FakeBridge(),
            _FakeRunManager(),
            _record(),
            ctx=ctx,
            agent_factory=lambda config: _FailingAgent(),
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "thread-xyz"}},
        )

        assert get_tenant_context() is None
