"""Fail-closed production declaration checks and live-probe placeholders."""

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.doctor.models import DoctorCheckResult, DoctorReport, DoctorStatus
from deerflow.config.app_config import AppConfig

ProductionCheck = Callable[[AppConfig], DoctorCheckResult]
SECRET_REFERENCE_PATTERN = re.compile(r"^\$[A-Z][A-Z0-9_]*$")


def _result(
    check_id: str,
    status: DoctorStatus,
    component: str,
    message: str,
    config_source: str,
    remediation: str | None = None,
) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=check_id,
        status=status,
        component=component,
        message=message,
        remediation=remediation,
        config_source=config_source,
    )


def check_production_enabled(config: AppConfig) -> DoctorCheckResult:
    production = config.production
    valid = production.enabled and production.environment == "production"
    return _result(
        "config.production_enabled",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "configuration",
        "Production declarations are enabled." if valid else "Production mode is not explicitly enabled.",
        "config.yaml:production.enabled,production.environment",
        None if valid else "Set production.enabled=true and production.environment=production.",
    )


def check_postgres_declared(config: AppConfig) -> DoctorCheckResult:
    valid = config.database.backend == "postgres" and bool(config.database.postgres_url.strip())
    return _result(
        "postgres.declared",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "database",
        "PostgreSQL is declared as the persistence backend." if valid else "Production requires a PostgreSQL backend and Secret reference.",
        "config.yaml:database.backend,database.postgres_url",
        None if valid else "Set database.backend=postgres and database.postgres_url to an environment-backed Secret.",
    )


def check_persistence_limits(config: AppConfig) -> DoctorCheckResult:
    valid = config.database.pool_size > 0 and config.production.limits.max_concurrent_runs > 0 and config.production.limits.max_sandbox_replicas > 0 and config.run_events.backend == "db"
    return _result(
        "runtime.finite_limits",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "runtime",
        "Database, Run, Sandbox, and run-event persistence limits are finite." if valid else "Production persistence or concurrency limits are unsafe.",
        "config.yaml:database.pool_size,run_events.backend,production.limits",
        None if valid else "Use run_events.backend=db and positive finite database, Run, and Sandbox limits.",
    )


def check_redis_declared(config: AppConfig) -> DoctorCheckResult:
    url = config.production.redis.url or ""
    valid = url.startswith("rediss://")
    return _result(
        "redis.declared",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "redis",
        "Redis dependency is declared." if valid else "Production Redis dependency is missing or invalid.",
        "config.yaml:production.redis.url",
        None if valid else "Declare production.redis.url using an environment-backed rediss:// URL.",
    )


def check_oidc_declared(config: AppConfig) -> DoctorCheckResult:
    oidc = config.production.oidc
    valid = oidc is not None and oidc.issuer.startswith("https://") and bool(oidc.audience.strip()) and (oidc.jwks_uri is None or oidc.jwks_uri.startswith("https://"))
    return _result(
        "oidc.declared",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "identity",
        "OIDC issuer and audience are declared." if valid else "Production OIDC declaration is incomplete.",
        "config.yaml:production.oidc",
        None if valid else "Declare an HTTPS OIDC issuer, non-empty audience, and optional HTTPS JWKS URI.",
    )


def check_auth_enabled(config: AppConfig) -> DoctorCheckResult:
    del config
    valid = os.environ.get("DEER_FLOW_AUTH_DISABLED") != "1"
    return _result(
        "auth.enabled",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "identity",
        "Gateway authentication is enabled." if valid else "Authentication bypass is enabled.",
        "environment:DEER_FLOW_AUTH_DISABLED",
        None if valid else "Unset DEER_FLOW_AUTH_DISABLED before any production deployment.",
    )


def check_sandbox_declared(config: AppConfig) -> DoctorCheckResult:
    sandbox = config.sandbox
    isolated_provider = "LocalSandboxProvider" not in sandbox.use
    valid = not sandbox.allow_host_bash and isolated_provider and bool(sandbox.provisioner_url) and sandbox.replicas is not None and 0 < sandbox.replicas <= config.production.limits.max_sandbox_replicas
    return _result(
        "sandbox.isolated",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "sandbox",
        "Host bash is disabled and a bounded isolated Sandbox Provisioner is declared." if valid else "Production Sandbox declaration is not isolated or bounded.",
        "config.yaml:sandbox,production.limits.max_sandbox_replicas",
        None if valid else "Disable host bash, use an isolated provider, declare provisioner_url, and set a positive bounded replica limit.",
    )


def check_backup_declared(config: AppConfig) -> DoctorCheckResult:
    backup = config.production.backup
    valid = backup.enabled and backup.declared_rpo_hours <= 24
    return _result(
        "backup.declared",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "backup",
        "Backup is enabled with an MVP-compatible declared RPO." if valid else "Backup or declared RPO is not production-ready.",
        "config.yaml:production.backup",
        None if valid else "Enable backups and declare an RPO between 1 and 24 hours.",
    )


def check_security_declarations(config: AppConfig) -> DoctorCheckResult:
    production = config.production
    gateway = production.gateway_security
    cors_valid = bool(gateway.cors_origins) and all(origin != "*" and origin.startswith("https://") for origin in gateway.cors_origins)
    valid = (
        config.log_level.lower() != "debug"
        and production.log_redaction_enabled
        and production.secret_store.provider != "env_dev_only"
        and production.secret_store.references_only
        and gateway.tls_enabled
        and gateway.csrf_enabled
        and gateway.rate_limit_enabled
        and cors_valid
    )
    return _result(
        "security.production_baseline",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "security",
        "Logging, Secret Store, TLS, CORS, CSRF, and rate-limit declarations satisfy the static baseline." if valid else "One or more production security declarations are unsafe.",
        "config.yaml:log_level,production.secret_store,production.gateway_security,production.log_redaction_enabled",
        None if valid else "Disable debug logging, enable redaction/TLS/CSRF/rate limiting, use an explicit CORS allowlist, and use a controlled Secret Store.",
    )


def check_secret_references(raw_config: Mapping[str, Any] | None) -> DoctorCheckResult:
    database = raw_config.get("database") if raw_config else None
    production = raw_config.get("production") if raw_config else None
    redis = production.get("redis") if isinstance(production, Mapping) else None
    postgres_url = database.get("postgres_url") if isinstance(database, Mapping) else None
    redis_url = redis.get("url") if isinstance(redis, Mapping) else None
    valid = all(isinstance(value, str) and SECRET_REFERENCE_PATTERN.fullmatch(value) for value in (postgres_url, redis_url))
    return _result(
        "secrets.references_only",
        DoctorStatus.PASS if valid else DoctorStatus.FAIL,
        "security",
        "Database and Redis credentials use environment-backed Secret references." if valid else "Database or Redis configuration contains a literal or unverifiable credential value.",
        "config.yaml:database.postgres_url,production.redis.url",
        None if valid else "Use exact $ENV_VAR references in versioned config and resolve them from a controlled Secret Store.",
    )


def check_deployment_profile(config: AppConfig) -> DoctorCheckResult:
    deployment = config.production.deployment
    status = DoctorStatus.FAIL
    message = "Deployment profile and replica declarations are inconsistent."
    remediation = "Declare a valid Profile S, H, or W topology and its required evidence references."

    if deployment.profile == "S" and deployment.gateway_profile is None and deployment.gateway_replicas == 1 and deployment.worker_replicas == 0:
        if deployment.ha_waiver_id:
            status = DoctorStatus.WARN
            message = "Profile S is explicit; the registered non-HA waiver must remain valid."
            remediation = "Keep the waiver current and do not claim high availability."
    elif deployment.profile == "H" and deployment.gateway_profile is None and deployment.gateway_replicas >= 2 and deployment.worker_replicas == 0 and deployment.profile_h_evidence:
        status = DoctorStatus.PASS
        message = "Profile H replicas and validation evidence are declared."
        remediation = None
    elif deployment.profile == "W":
        gateway_s_valid = deployment.gateway_profile == "S" and deployment.gateway_replicas == 1 and bool(deployment.ha_waiver_id)
        gateway_h_valid = deployment.gateway_profile == "H" and deployment.gateway_replicas >= 2 and bool(deployment.profile_h_evidence)
        worker_valid = deployment.worker_replicas >= 1 and bool(deployment.profile_w_evidence) and bool(deployment.profile_w_rollback_evidence) and deployment.profile_w_soak_hours >= 24
        if worker_valid and (gateway_s_valid or gateway_h_valid):
            status = DoctorStatus.WARN if gateway_s_valid else DoctorStatus.PASS
            message = "Profile W is declared with a non-HA Profile S Gateway waiver." if gateway_s_valid else "Profile W and its Profile H Gateway evidence are declared."
            remediation = "Keep the Gateway non-HA waiver current and do not claim Gateway high availability." if gateway_s_valid else None

    return _result(
        "deployment.profile_consistency",
        status,
        "deployment",
        message,
        "config.yaml:production.deployment",
        remediation,
    )


def check_feature_flag_expiry(config: AppConfig) -> DoctorCheckResult:
    """Surface approaching/expired high-risk Feature Flag cleanup dates (ci-cd §11).

    ci-cd §11 requires every temporary Feature Flag to carry a cleanup date
    (``expires_at``). This check makes that date operational: a flag nearing
    its expiry is a WARN (schedule its removal / Contract), and an expired
    flag is a FAIL (the temporary flag has overstayed and must be removed or
    re-justified). Reads the static registry in
    ``deerflow.tenancy.feature_flags`` — no live state, so it is a normal
    ``ProductionCheck`` (unlike the live-DB tenant migration probe).

    The 30-day WARN window is a judgement call: long enough that an on-call
    has time to land the Contract PR without a surprise, short enough that a
    stale flag does not WARN for months. Pinned here so the doctor output is
    deterministic.
    """
    from datetime import UTC, date, datetime

    from deerflow.tenancy.feature_flags import MULTI_ORG_FLAG

    # ``del`` signals this check does not consume ``config``; it reads the
    # static registry. Mirrors ``check_auth_enabled``'s convention.
    del config

    today = datetime.now(UTC).date()
    expires = date.fromisoformat(MULTI_ORG_FLAG.expires_at)
    days_left = (expires - today).days

    _config_source = "deerflow/tenancy/feature_flags.py:expires_at"
    if days_left < 0:
        return _result(
            "feature_flag.expiry",
            DoctorStatus.FAIL,
            "feature-flag",
            f"multi_org Feature Flag expired on {MULTI_ORG_FLAG.expires_at} ({abs(days_left)} day(s) overdue). Temporary flags must be removed or explicitly re-justified past their cleanup date.",
            _config_source,
            "Land the Contract cleanup (removing the flag) or update expires_at in deerflow/tenancy/feature_flags.py with a justified new date.",
        )
    if days_left <= 30:
        return _result(
            "feature_flag.expiry",
            DoctorStatus.WARN,
            "feature-flag",
            f"multi_org Feature Flag expires on {MULTI_ORG_FLAG.expires_at} ({days_left} day(s) left). Schedule its removal before the date.",
            _config_source,
            "Land the Contract cleanup (PR-025D) that removes the flag, or move expires_at out with an explicit reason.",
        )
    return _result(
        "feature_flag.expiry",
        DoctorStatus.PASS,
        "feature-flag",
        f"multi_org Feature Flag expires on {MULTI_ORG_FLAG.expires_at} ({days_left} day(s) left).",
        _config_source,
    )


STATIC_CHECKS: tuple[ProductionCheck, ...] = (
    check_production_enabled,
    check_postgres_declared,
    check_persistence_limits,
    check_redis_declared,
    check_oidc_declared,
    check_auth_enabled,
    check_sandbox_declared,
    check_feature_flag_expiry,
    check_backup_declared,
    check_security_declarations,
    check_deployment_profile,
)

DEFERRED_LIVE_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    (
        "postgres.connectivity",
        "database",
        "PostgreSQL connectivity, version, and migration probe is not implemented.",
        "config.yaml:database",
    ),
    (
        "redis.connectivity",
        "redis",
        "Redis connectivity and Stream capability probe is not implemented.",
        "config.yaml:production.redis",
    ),
    (
        "oidc.jwks_validation",
        "identity",
        "OIDC issuer, audience, and JWKS live validation is not implemented.",
        "config.yaml:production.oidc",
    ),
    (
        "sandbox.provisioner_create",
        "sandbox",
        "Sandbox Provisioner create/destroy probe is not implemented.",
        "config.yaml:sandbox",
    ),
    (
        "backup.freshness",
        "backup",
        "Backup/WAL freshness probe is not implemented.",
        "config.yaml:production.backup",
    ),
    (
        "deployment.evidence_validation",
        "deployment",
        "Profile H/W and HA waiver evidence validation is not implemented.",
        "config.yaml:production.deployment",
    ),
    (
        "secret_store.access",
        "security",
        "Controlled Secret Store access validation is not implemented.",
        "config.yaml:production.secret_store",
    ),
    (
        "gateway.security_validation",
        "gateway",
        "TLS, CORS, CSRF, and rate-limit runtime validation is not implemented.",
        "config.yaml:production.gateway_security",
    ),
    (
        "object_storage.security",
        "storage",
        "Object storage privacy, encryption, and read/write validation is not implemented.",
        "planned production object-storage declaration",
    ),
    (
        "agent.release_ref_enforcement",
        "release",
        "Published ReleaseRef-only production admission validation is not implemented.",
        "planned production agent-release declaration",
    ),
    (
        "audit.outbox",
        "audit",
        "Audit sink and transactional outbox validation is not implemented.",
        "planned production audit declaration",
    ),
    (
        "gateway.rate_limit_retry_after",
        "gateway",
        "Live 429 and Retry-After behavior validation is not implemented.",
        "config.yaml:production.gateway_security.rate_limit_enabled",
    ),
)


def run_production_checks(
    config: AppConfig,
    config_path: Path,
    raw_config: Mapping[str, Any] | None = None,
    extra_checks: tuple[DoctorCheckResult, ...] = (),
) -> DoctorReport:
    """Assemble the production doctor report.

    ``extra_checks`` carries pre-computed live-DB probes (e.g. the tenant
    migration-phase probe from ``app.doctor.tenant_probe``) that cannot be a
    plain ``ProductionCheck`` because they need an async DB connection. The
    caller awaits the probe and passes its ``DoctorCheckResult`` here; this
    keeps ``run_production_checks`` itself synchronous so the unit tests do
    not need to be async-ified. The extra checks land after the secret-
    references check and before the deferred placeholders, mirroring their
    logical role as "live verification of the static declarations".
    """
    checks = [check(config) for check in STATIC_CHECKS]
    checks.append(check_secret_references(raw_config))
    checks.extend(extra_checks)
    checks.extend(
        _result(
            check_id,
            DoctorStatus.FAIL,
            component,
            message,
            config_source,
            "Implement and verify this live probe in PR-064 before production admission.",
        )
        for check_id, component, message, config_source in DEFERRED_LIVE_CHECKS
    )
    return DoctorReport(profile="production", config_path=str(config_path), checks=tuple(checks))
