"""Test configuration for the backend test suite.

Sets up sys.path and pre-mocks modules that would cause circular import
issues when unit-testing lightweight config/registry code in isolation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Make 'app' and 'deerflow' importable from any working directory
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

# Break the circular import chain that exists in production code:
#   deerflow.subagents.__init__
#     -> .executor (SubagentExecutor, SubagentResult)
#       -> deerflow.agents.thread_state
#         -> deerflow.agents.__init__
#           -> lead_agent.agent
#             -> subagent_limit_middleware
#               -> deerflow.subagents.executor  <-- circular!
#
# By injecting a mock for deerflow.subagents.executor *before* any test module
# triggers the import, __init__.py's "from .executor import ..." succeeds
# immediately without running the real executor module.
_executor_mock = MagicMock()
_executor_mock.SubagentExecutor = MagicMock
_executor_mock.SubagentResult = MagicMock
_executor_mock.SubagentStatus = MagicMock
_executor_mock.MAX_CONCURRENT_SUBAGENTS = 3
_executor_mock.get_background_task_result = MagicMock()

sys.modules["deerflow.subagents.executor"] = _executor_mock


@pytest.fixture()
def provisioner_module():
    """Load docker/provisioner/app.py as an importable test module.

    Shared by test_provisioner_kubeconfig and test_provisioner_pvc_volumes so
    that any change to the provisioner entry-point path or module name only
    needs to be updated in one place.
    """
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "docker" / "provisioner" / "app.py"
    spec = importlib.util.spec_from_file_location("provisioner_app_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Auto-set user context for every test unless marked no_auto_user
# ---------------------------------------------------------------------------
#
# Repository methods read ``user_id`` from a contextvar by default
# (see ``deerflow.runtime.user_context``). Without this fixture, every
# pre-existing persistence test would raise RuntimeError because the
# contextvar is unset. The fixture sets a default test user on every
# test; tests that explicitly want to verify behaviour *without* a user
# context should mark themselves ``@pytest.mark.no_auto_user``.


@pytest.fixture(autouse=True)
def _reset_skill_storage_singleton():
    """Reset the SkillStorage singleton between tests to prevent cross-test contamination."""
    try:
        from deerflow.skills.storage import reset_skill_storage
    except ImportError:
        yield
        return
    reset_skill_storage()
    try:
        yield
    finally:
        reset_skill_storage()


@pytest.fixture(autouse=True)
def _restore_title_config_singleton():
    """Reset ``_title_config`` to its pristine default after every test.

    ``AppConfig.from_file()`` writes the on-disk ``title`` block into the
    module-level singleton (``config/app_config.py`` calls
    ``load_title_config_from_dict``). Any test that loads the real
    ``config.yaml`` therefore leaves the singleton in a state that
    ``test_title_middleware_core_logic.py`` does not expect; that suite
    relies on the pristine ``TitleConfig()`` default (``enabled=True``).
    We restore the default after every test so test files stay
    independent regardless of order.
    """
    try:
        from deerflow.config.title_config import reset_title_config
    except ImportError:
        yield
        return

    try:
        yield
    finally:
        reset_title_config()


@pytest.fixture(autouse=True)
def _auto_user_context(request):
    """Inject a default ``test-user-autouse`` user and bound tenant context.

    Opt-out via ``@pytest.mark.no_auto_user``. Uses lazy import so that
    tests which don't touch the persistence layer never pay the cost
    of importing runtime.user_context.

    Since PR-024, repository reads/writes resolve ``org_id`` from the bound
    :class:`~deerflow.contracts.TenantContext` and fail closed when none is
    bound. To mirror production (every request binds a tenant via
    TenantResolutionMiddleware, PR-013), the autouse fixture also binds a
    minimal tenant context for the default bootstrap org so ordinary
    persistence tests keep working without per-test boilerplate. The literal
    ``"default"`` matches ``DEFAULT_BOOTSTRAP_ORG_ID`` (app.gateway.config);
    importing that module here would drag in the full app config chain.
    """
    if request.node.get_closest_marker("no_auto_user"):
        yield
        return

    try:
        from deerflow.runtime.user_context import (
            reset_current_user,
            set_current_user,
        )
    except ImportError:
        yield
        return

    user = SimpleNamespace(id="test-user-autouse", email="test@local")
    token = set_current_user(user)

    # Bind a tenant context for the default bootstrap org (PR-024: repository
    # reads/writes resolve org_id from the bound tenant context and fail
    # closed when none is bound). Lazy import keeps the contracts import out
    # of tests that never touch persistence.
    tenant_token = None
    try:
        from datetime import UTC, datetime

        from deerflow.contracts import (
            PrincipalRef,
            TenantContext,
            bind_tenant_context,
            reset_tenant_context,
        )
    except ImportError:
        datetime = None  # type: ignore[assignment]
        TenantContext = None  # type: ignore[assignment]

    if TenantContext is not None:
        tenant = TenantContext(
            org_id="default",
            principal=PrincipalRef(
                id="test-user-autouse",
                type="user",
                user_id="test-user-autouse",
            ),
            auth_method="session",
            request_id="test-request-autouse",
            issued_at=datetime.now(UTC),
        )
        tenant_token = bind_tenant_context(tenant)

    try:
        yield
    finally:
        if tenant_token is not None:
            reset_tenant_context(tenant_token)
        reset_current_user(token)


async def seed_test_default_org() -> None:
    """Idempotently insert the default bootstrap org row into the active DB.

    Repository writes stamp ``org_id`` from the bound tenant context (PR-024),
    and ``org_id`` carries a real FK to ``organizations.id`` (PR-021). Test
    databases created via ``init_engine`` + ``create_all`` have the table but
    no rows, so a stamped ``org_id='default'`` would violate the FK. Call this
    from a test's engine-init fixture (after ``init_engine``) so the autouse
    tenant context's org satisfies the constraint. No-op when no engine is
    initialised or the row already exists.
    """
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.orgs.model import OrganizationRow

    sf = get_session_factory()
    if sf is None:
        return
    async with sf() as session:
        existing = await session.get(OrganizationRow, "default")
        if existing is not None:
            return
        session.add(
            OrganizationRow(
                id="default",
                slug="default",
                name="Default (test)",
                status="active",
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# OpenTelemetry test isolation (PR-062)
# ---------------------------------------------------------------------------
#
# OTel's public ``trace.set_tracer_provider`` and the private
# ``_set_tracer_provider`` are both gated by a ``Once`` whose ``do_once``
# semantics mean the provider can be set at most ONCE per process — the
# ``log=False`` argument only silences the warning, it does not enable
# override. So once any test (or the gateway lifespan) installs a provider,
# every later fixture's ``set_tracer_provider`` call is a silent no-op and
# the in-memory exporter never receives spans.
#
# The fixture below does the hard reset required for real per-test isolation:
# it directly assigns the ``_TRACER_PROVIDER`` module global, resets the
# ``Once._done`` flag so the next legitimate ``set_tracer_provider`` call
# works, and yields the in-memory exporter for assertions. Tests that need
# to capture spans should depend on this fixture instead of calling
# ``set_tracer_provider`` themselves.


@pytest.fixture()
def otel_in_memory():
    """Install an in-memory OTel provider that captures every span.

    Yields the :class:`InMemorySpanExporter`; on teardown the provider is
    shut down and a fresh default provider re-installed so the next test
    starts from a clean slate. Uses the hard-reset path (direct global
    assignment + ``Once`` flag reset) because OTel offers no public API for
    per-test provider swap.
    """
    import opentelemetry.trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Hard reset: bypass the Once guard by assigning the global directly and
    # clearing the Once flag so a future set_tracer_provider can take effect.
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]

    try:
        yield exporter
    finally:
        provider.shutdown()
        fresh = TracerProvider()
        otel_trace._TRACER_PROVIDER = fresh  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# PR-032 — RBAC session-factory fixture
# ---------------------------------------------------------------------------
#
# ``require_rbac`` calls ``AuthorizeService.authorize()``, which JOINs
# role_bindings → roles on the DB. Real-authorize-mode router tests
# therefore need a live (isolated) SQLite engine. Defined here (the
# standard pytest fixture location) rather than in ``_router_auth_helpers``
# so test modules request it via parameter injection without an explicit
# import — mirrors the existing ``sf`` fixture in ``test_iam_authorize``.
# Pair with ``bootstrap_rbac`` from ``_router_auth_helpers`` to seed the
# org / roles / user / membership / binding rows.


@pytest.fixture
async def rbac_sf(tmp_path):
    """Boot an isolated SQLite DB; yield its session factory (PR-032).

    Counterpart of ``test_iam_authorize.sf`` for the router-test family.
    Every TestClient-based router test migrated to ``make_rbac_test_app``
    in real-authorize mode should depend on this fixture, then call
    ``bootstrap_rbac`` before building the app. The factory is also what
    ``make_rbac_test_app`` re-binds the ``AuthorizeService`` singleton
    against, so ``require_rbac``'s ``get_authorize_service()`` reads the
    rows this fixture seeds.
    """
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'rbac_router.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()
        # Drop the AuthorizeService singleton so it doesn't outlive this
        # test's session factory — ``make_rbac_test_app(sf=...)`` rebinds
        # it, and a stale singleton would send later tests' authorize()
        # calls at a closed engine. Mirrors the reset the e2e tests do in
        # their own ``_reset_process_singletons`` copies.
        from app.gateway.authorize import reset_authorize_service_for_testing

        reset_authorize_service_for_testing()
