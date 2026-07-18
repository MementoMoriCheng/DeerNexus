import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.gateway.auth_disabled import warn_if_auth_disabled_enabled
from app.gateway.auth_middleware import AuthMiddleware
from app.gateway.config import DEFAULT_ORG_NAME, DEFAULT_ORG_SLUG, get_gateway_config
from app.gateway.correlation_middleware import CorrelationMiddleware
from app.gateway.csrf_middleware import CSRFMiddleware, get_configured_cors_origins
from app.gateway.deps import langgraph_runtime
from app.gateway.routers import admin as admin_router
from app.gateway.routers import (
    agents,
    artifacts,
    assistants_compat,
    auth,
    channel_connections,
    channels,
    feedback,
    mcp,
    memory,
    models,
    runs,
    skills,
    suggestions,
    thread_runs,
    threads,
    uploads,
)
from app.gateway.routers import metrics as metrics_router
from app.gateway.tenant import TenantResolutionMiddleware
from deerflow.config import app_config as deerflow_app_config
from deerflow.config.app_config import apply_logging_level
from deerflow.config.observability_config import ObservabilityConfig
from deerflow.observability import configure_logging, init_tracing
from deerflow.observability.metrics import _set_constant_labels

AppConfig = deerflow_app_config.AppConfig
get_app_config = deerflow_app_config.get_app_config

# Install a formatter as early as possible so import-time log lines from
# gateway submodules hit a configured handler. The lifespan re-runs
# ``configure_logging`` with the operator's real ``observability`` config
# (which may switch to JSON), so this initial call only needs to match
# today's text behaviour. Defaults are explicit so a failure to read
# config at import time still produces a deterministic formatter.
configure_logging(ObservabilityConfig())

logger = logging.getLogger(__name__)

# Upper bound (seconds) each lifespan shutdown hook is allowed to run.
# Bounds worker exit time so uvicorn's reload supervisor does not keep
# firing signals into a worker that is stuck waiting for shutdown cleanup.
_SHUTDOWN_HOOK_TIMEOUT_SECONDS = 5.0


async def _ensure_admin_user(app: FastAPI) -> None:
    """Startup hook: handle first boot and migrate orphan threads otherwise.

    After admin creation, migrate orphan threads from the LangGraph
    store (metadata.user_id unset) to the admin account. This is the
    "no-auth → with-auth" upgrade path: users who ran DeerFlow without
    authentication have existing LangGraph thread data that needs an
    owner assigned.
        First boot (no admin exists):
            - Does NOT create any user accounts automatically.
            - The operator must visit ``/setup`` to create the first admin.

    Subsequent boots (admin already exists):
      - Runs the one-time "no-auth → with-auth" orphan thread migration for
        existing LangGraph thread metadata that has no user_id.

    No SQL persistence migration is needed: the four user_id columns
    (threads_meta, runs, run_events, feedback) only come into existence
    alongside the auth module via create_all, so freshly created tables
    never contain NULL-owner rows.
    """
    from sqlalchemy import select

    from app.gateway.deps import get_local_provider
    from deerflow.persistence.engine import get_session_factory
    from deerflow.persistence.user.model import UserRow

    try:
        provider = get_local_provider()
    except RuntimeError:
        # Auth persistence may not be initialized in some test/boot paths.
        # Skip admin migration work rather than failing gateway startup.
        logger.warning("Auth persistence not ready; skipping admin bootstrap check")
        return

    sf = get_session_factory()
    if sf is None:
        return

    admin_count = await provider.count_admin_users()

    if admin_count == 0:
        logger.info("=" * 60)
        logger.info("  First boot detected — no admin account exists.")
        logger.info("  Visit /setup to complete admin account creation.")
        logger.info("=" * 60)
        return

    # Admin already exists — run orphan thread migration for any
    # LangGraph thread metadata that pre-dates the auth module.
    async with sf() as session:
        stmt = select(UserRow).where(UserRow.system_role == "admin").limit(1)
        row = (await session.execute(stmt)).scalar_one_or_none()

    if row is None:
        return  # Should not happen (admin_count > 0 above), but be safe.

    admin_id = str(row.id)

    # LangGraph store orphan migration — non-fatal.
    # This covers the "no-auth → with-auth" upgrade path for users
    # whose existing LangGraph thread metadata has no user_id set.
    store = getattr(app.state, "store", None)
    if store is not None:
        try:
            migrated = await _migrate_orphaned_threads(store, admin_id)
            if migrated:
                logger.info("Migrated %d orphan LangGraph thread(s) to admin", migrated)
        except Exception:
            logger.exception("LangGraph thread migration failed (non-fatal)")


async def _ensure_default_org(app: FastAPI) -> None:
    """Startup hook: materialise the default Organization + system admin role (PR-022).

    The single-Org tenant resolver (PR-013/014) already binds every request
    and channel dispatch to ``config.default_org_id``; this hook creates the
    matching ``organizations`` row so that FK targets exist for
    ``runs.org_id`` / ``feedback.org_id`` etc. (PR-021) and for the admin
    OrgMembership created later in ``/initialize``. The system-template
    ``org:admin`` role is created here too (no FK dependency) so the
    RoleBinding's ``role_id`` FK target exists before the first admin binds.

    Idempotent and non-fatal: a failure logs and continues (the resolver
    still hands out the configured org id; a later boot or operator can
    reconcile). Runs inside the lifespan after persistence is ready.
    """
    from deerflow.persistence.engine import get_session_factory
    from deerflow.tenancy import ensure_default_org, ensure_system_admin_role

    sf = get_session_factory()
    if sf is None:
        return  # Persistence not initialised (some test/boot paths).

    config = get_gateway_config()
    try:
        await ensure_default_org(
            sf,
            org_id=config.default_org_id,
            slug=DEFAULT_ORG_SLUG,
            name=DEFAULT_ORG_NAME,
        )
        await ensure_system_admin_role(sf)
    except Exception:
        logger.exception("Default Org bootstrap failed (non-fatal)")


async def _ensure_validation_org(app: FastAPI) -> None:
    """Startup hook: materialise the non-public validation Org (PR-025B).

    Called only when ``tenancy.multi_org.phase == "validation"``. The
    validation Org is the migration milestone for the validation phase
    (data-model §13.3, ci-cd §10.3): an audited, inert second Org whose row
    the operator can later bind the validation cohort to. It receives no
    traffic in PR-025B — the request-path tenant resolver is still single-Org
    and maps every request to ``default_org_id``; this hook only creates the
    ``organizations`` row, no Membership / RoleBinding.

    Not called for ``disabled`` (no validation Org wanted — today's behaviour)
    or ``active`` (multi-org is open; the validation Org has either been
    promoted to a real tenant or soft-deleted, both of which are operator
    decisions that happen after the flag leaves the validation phase).

    Idempotent and non-fatal, mirroring :func:`_ensure_default_org`: a failure
    logs and continues. The config's phase ↔ validation_org coupling is
    enforced at the pydantic layer (``MultiOrgConfig``), so by the time we
    read ``validation_org`` here it is guaranteed non-None.
    """
    startup_config = get_app_config()
    multi_org = startup_config.tenancy.multi_org
    if multi_org.phase != "validation":
        return

    validation_org = multi_org.validation_org
    if validation_org is None:  # pragma: no cover — pydantic invariant; defensive
        logger.warning("validation phase active but validation_org unset; skipping")
        return

    from deerflow.persistence.engine import get_session_factory
    from deerflow.tenancy import ensure_validation_org

    sf = get_session_factory()
    if sf is None:
        return  # Persistence not initialised (some test/boot paths).

    try:
        await ensure_validation_org(
            sf,
            org_id=validation_org.id,
            slug=validation_org.slug,
            name=validation_org.name,
        )
    except Exception:
        logger.exception("Validation Org bootstrap failed (non-fatal)")


async def _iter_store_items(store, namespace, *, page_size: int = 500):
    """Paginated async iterator over a LangGraph store namespace.

    Replaces the old hardcoded ``limit=1000`` call with a cursor-style
    loop so that environments with more than one page of orphans do
    not silently lose data. Terminates when a page is empty OR when a
    short page arrives (indicating the last page).
    """
    offset = 0
    while True:
        batch = await store.asearch(namespace, limit=page_size, offset=offset)
        if not batch:
            return
        for item in batch:
            yield item
        if len(batch) < page_size:
            return
        offset += page_size


async def _migrate_orphaned_threads(store, admin_user_id: str) -> int:
    """Migrate LangGraph store threads with no user_id to the given admin.

    Uses cursor pagination so all orphans are migrated regardless of
    count. Returns the number of rows migrated.
    """
    migrated = 0
    async for item in _iter_store_items(store, ("threads",)):
        metadata = item.value.get("metadata", {})
        if not metadata.get("user_id"):
            metadata["user_id"] = admin_user_id
            item.value["metadata"] = metadata
            await store.aput(("threads",), item.key, item.value)
            migrated += 1
    return migrated


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""

    # Load config and check necessary environment variables at startup.
    # `startup_config` is a local snapshot used only for one-shot bootstrap
    # work (logging level, langgraph_runtime engines, channels). Request-time
    # config resolution always routes through `get_app_config()` in
    # `app/gateway/deps.py::get_config()` so `config.yaml` edits become
    # visible without a process restart. We deliberately do NOT cache this
    # snapshot on `app.state` to keep that contract enforceable.
    try:
        startup_config = get_app_config()
        apply_logging_level(startup_config.log_level)
        # Re-run the formatter selection now that the real observability
        # config is loaded: the import-time ``configure_logging`` call above
        # used the safe default (text format); if the operator set
        # ``observability.log_format=json`` this is where it takes effect.
        configure_logging(startup_config.observability)
        # Seed the constant labels stamped on every Prometheus metric
        # (PR-063) — must happen before the first request so the registry
        # reflects the operator's service_name / environment / deployment_version.
        # Mirrors the OTel Resource attributes set by ``init_tracing`` below.
        _set_constant_labels(
            startup_config.observability.service_name,
            startup_config.observability.environment,
            startup_config.observability.deployment_version,
        )
        logger.info("Configuration loaded successfully")
        warn_if_auth_disabled_enabled()
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # Initialise OpenTelemetry SDK + OTLP exporter when the operator has set
    # ``observability.otel.exporter_endpoint``. Returns ``None`` (no-op
    # tracer) when the endpoint is unset, which is today's default. The
    # shutdown callable flushes the BatchSpanProcessor on exit; we keep it on
    # the stack so it runs even if a later lifespan step raises.
    tracing_shutdown = init_tracing(startup_config.observability)

    # PR-063: seed the §4.6 db_pool gauges once the engine is up. The langgraph
    # runtime context below initialises the engine; we refresh again there. A
    # periodic refresh is intentionally not wired here (gauge staleness is
    # bounded by pool churn, which is frequent in any real workload).
    try:
        from deerflow.persistence.engine import refresh_db_pool_metrics

        refresh_db_pool_metrics()
    except Exception:
        logger.debug("db pool metric refresh skipped at startup", exc_info=True)

    # Pre-warm tiktoken encoding cache so the first memory-injection request
    # never blocks on the BPE data download (which hits an OpenAI/Azure URL
    # that may be unreachable in restricted networks — see issue #3402).
    # When memory.token_counting is "char", token counting never touches
    # tiktoken, so skip the warm-up entirely (avoids even the 5s probe in
    # network-restricted deployments — see issue #3429).
    if startup_config.memory.token_counting == "char":
        logger.info("memory.token_counting='char'; skipping tiktoken warm-up (network-free token estimation)")
    else:
        try:
            from deerflow.agents.memory.prompt import warm_tiktoken_cache

            warmed = await asyncio.wait_for(
                asyncio.to_thread(warm_tiktoken_cache),
                timeout=5,
            )
            if warmed:
                logger.info("tiktoken encoding cache warmed successfully")
            else:
                logger.warning("tiktoken encoding cache warm-up failed; token counting will use character-based fallback until tiktoken loads successfully")
        except TimeoutError:
            logger.warning("tiktoken encoding cache warm-up timed out; token counting will use character-based fallback until tiktoken loads successfully")
        except Exception:
            logger.warning("tiktoken warm-up skipped", exc_info=True)

    # Initialize LangGraph runtime components (StreamBridge, RunManager, checkpointer, store)
    async with langgraph_runtime(app, startup_config):
        logger.info("LangGraph runtime initialised")
        # PR-063: engine is now up — refresh the §4.6 db_pool gauges so they
        # reflect the real pool size before the first request.
        try:
            from deerflow.persistence.engine import refresh_db_pool_metrics

            refresh_db_pool_metrics()
        except Exception:
            logger.debug("db pool metric refresh after runtime init skipped", exc_info=True)

        # Materialise the default Organization + system admin role (PR-022).
        # Must run BEFORE _ensure_admin_user so the Org row exists for any
        # admin-creation path, and before the first /initialize binds a
        # RoleBinding (the role_id FK target must already exist).
        await _ensure_default_org(app)

        # Seed the non-public validation Org (PR-025B) when the operator has
        # set tenancy.multi_org.phase=validation. No-op for disabled/active;
        # see _ensure_validation_org for the phase semantics.
        await _ensure_validation_org(app)

        # Check admin bootstrap state and migrate orphan threads after admin exists.
        # Must run AFTER langgraph_runtime so app.state.store is available for thread migration
        await _ensure_admin_user(app)

        # Start IM channel service if any channels are configured
        try:
            from app.channels.service import start_channel_service

            channel_service = await start_channel_service(startup_config)
            logger.info("Channel service started: %s", channel_service.get_status())
        except Exception:
            logger.exception("No IM channels configured or channel service failed to start")

        yield

        # Stop channel service on shutdown (bounded to prevent worker hang)
        try:
            from app.channels.service import stop_channel_service

            await asyncio.wait_for(
                stop_channel_service(),
                timeout=_SHUTDOWN_HOOK_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Channel service shutdown exceeded %.1fs; proceeding with worker exit.",
                _SHUTDOWN_HOOK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Failed to stop channel service")

    # Flush any in-flight spans before exit so they reach the collector.
    # No-op when tracing was never initialised (init_tracing returned None).
    if tracing_shutdown is not None:
        try:
            tracing_shutdown()
        except Exception:
            logger.warning("Tracing shutdown hook raised; continuing with gateway shutdown", exc_info=True)

    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    config = get_gateway_config()
    docs_url = "/docs" if config.enable_docs else None
    redoc_url = "/redoc" if config.enable_docs else None
    openapi_url = "/openapi.json" if config.enable_docs else None

    app = FastAPI(
        title="DeerFlow API Gateway",
        description="""
## DeerFlow API Gateway

API Gateway for DeerFlow - A LangGraph-based AI agent backend with sandbox execution capabilities.

### Features

- **Models Management**: Query and retrieve available AI models
- **MCP Configuration**: Manage Model Context Protocol (MCP) server configurations
- **Memory Management**: Access and manage global memory data for personalized conversations
- **Skills Management**: Query and manage skills and their enabled status
- **Artifacts**: Access thread artifacts and generated files
- **Health Monitoring**: System health check endpoints

### Architecture

LangGraph-compatible requests are routed through nginx to this gateway.
This gateway provides runtime endpoints for agent runs plus custom endpoints for models, MCP configuration, skills, and artifacts.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "mcp",
                "description": "Manage Model Context Protocol (MCP) server configurations",
            },
            {
                "name": "memory",
                "description": "Access and manage global memory data for personalized conversations",
            },
            {
                "name": "skills",
                "description": "Manage skills and their configurations",
            },
            {
                "name": "artifacts",
                "description": "Access and download thread artifacts and generated files",
            },
            {
                "name": "uploads",
                "description": "Upload and manage user files for threads",
            },
            {
                "name": "threads",
                "description": "Manage DeerFlow thread-local filesystem data",
            },
            {
                "name": "agents",
                "description": "Create and manage custom agents with per-agent config and prompts",
            },
            {
                "name": "suggestions",
                "description": "Generate follow-up question suggestions for conversations",
            },
            {
                "name": "channels",
                "description": "Manage IM channel integrations (Feishu, Slack, Telegram)",
            },
            {
                "name": "assistants-compat",
                "description": "LangGraph Platform-compatible assistants API (stub)",
            },
            {
                "name": "runs",
                "description": "LangGraph Platform-compatible runs lifecycle (create, stream, cancel)",
            },
            {
                "name": "admin",
                "description": "Org Console API: per-Org stats, runs listing, and token usage for the Admin Console UI (PR-060).",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
        ],
    )

    # Tenant: resolve and bind TenantContext after auth (PR-013). Note
    # BaseHTTPMiddleware runs in reverse add order inside call_next: the
    # middleware added LAST runs FIRST. To run tenant resolution AFTER auth,
    # register it BEFORE AuthMiddleware here.
    app.add_middleware(TenantResolutionMiddleware)

    # Auth: reject unauthenticated requests to non-public paths (fail-closed safety net)
    app.add_middleware(AuthMiddleware)

    # CSRF: Double Submit Cookie pattern for state-changing requests
    app.add_middleware(CSRFMiddleware)

    # CORS: the unified nginx endpoint is same-origin by default. Split-origin
    # browser clients must opt in with this explicit Gateway allowlist so CORS
    # and CSRF origin checks share the same source of truth.
    cors_origins = sorted(get_configured_cors_origins())
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Correlation: outermost middleware (added last → runs first). Binds the
    # per-request correlation id, opens the HTTP root span (§5.1) and emits
    # ``gateway.request.completed`` (§3.4). Fail-open — observability is never
    # a correctness gate; TenantResolutionMiddleware stays the fail-closed gate.
    # See app/gateway/correlation_middleware.py for the full contract.
    app.add_middleware(CorrelationMiddleware)

    # Include routers
    # Models API is mounted at /api/models
    app.include_router(models.router)

    # MCP API is mounted at /api/mcp
    app.include_router(mcp.router)

    # Memory API is mounted at /api/memory
    app.include_router(memory.router)

    # Skills API is mounted at /api/skills
    app.include_router(skills.router)

    # Artifacts API is mounted at /api/threads/{thread_id}/artifacts
    app.include_router(artifacts.router)

    # Uploads API is mounted at /api/threads/{thread_id}/uploads
    app.include_router(uploads.router)

    # Thread cleanup API is mounted at /api/threads/{thread_id}
    app.include_router(threads.router)

    # Agents API is mounted at /api/agents
    app.include_router(agents.router)

    # Suggestions API is mounted at /api/threads/{thread_id}/suggestions
    app.include_router(suggestions.router)

    # User-facing IM channel connection API is mounted at /api/channels
    app.include_router(channel_connections.router)

    # Channels API is mounted at /api/channels
    app.include_router(channels.router)

    # Assistants compatibility API (LangGraph Platform stub)
    app.include_router(assistants_compat.router)

    # Auth API is mounted at /api/v1/auth
    app.include_router(auth.router)

    # Org Console API (PR-060) is mounted at /api/v1/admin. Read-only stats /
    # runs / usage endpoints scoped to the caller's active Org; gated by the
    # temporary ``require_admin_user`` helper until Track C RBAC lands.
    app.include_router(admin_router.router)

    # Feedback API is mounted at /api/threads/{thread_id}/runs/{run_id}/feedback
    app.include_router(feedback.router)

    # Thread Runs API (LangGraph Platform-compatible runs lifecycle)
    app.include_router(thread_runs.router)

    # Stateless Runs API (stream/wait without a pre-existing thread)
    app.include_router(runs.router)

    # Prometheus scrape endpoint (PR-063). Public (no auth) — §4.1 forbids
    # high-cardinality id labels so the payload carries no sensitive data.
    # Gated on observability.metrics.enabled; disabled 404s the route.
    try:
        observability_cfg = get_app_config().observability
        metrics_enabled = observability_cfg.metrics.enabled
    except Exception:
        metrics_enabled = True  # safe default — metrics are cheap, every SLO depends on them
    if metrics_enabled:
        app.include_router(metrics_router.router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        """Health check endpoint.

        Returns:
            Service health status information.
        """
        return {"status": "healthy", "service": "deer-flow-gateway"}

    return app


# Create app instance for uvicorn
app = create_app()
