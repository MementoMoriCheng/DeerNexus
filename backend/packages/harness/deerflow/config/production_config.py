"""Typed production deployment declarations used by preflight checks.

These models describe operator intent. They do not probe infrastructure or
make a deployment production-ready by themselves.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DeploymentProfileConfig(BaseModel):
    """Declared Gateway/Worker topology and its validation evidence."""

    profile: Literal["S", "H", "W"] = "S"
    gateway_profile: Literal["S", "H"] | None = None
    gateway_replicas: int = Field(default=1, ge=1)
    worker_replicas: int = Field(default=0, ge=0)
    ha_waiver_id: str | None = None
    profile_h_evidence: str | None = None
    #: PR-074 (ADR-0006 §3.5/§11): operator-declared completed HA soak hours.
    #: Profile H admission requires >= 24h; the real soak runs in the release
    #: pipeline / runbook — the doctor only verifies the declaration is complete.
    profile_h_soak_hours: int = Field(default=0, ge=0)
    #: PR-074 (ADR-0006 §11): link to the production-equivalent fault-injection
    #: drill record (runbook URL / fault-test report). Required for Profile H.
    profile_h_fault_injection_evidence: str | None = None
    profile_w_evidence: str | None = None
    profile_w_rollback_evidence: str | None = None
    profile_w_soak_hours: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")


class ProductionOidcConfig(BaseModel):
    issuer: str
    audience: str
    jwks_uri: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProductionRedisConfig(BaseModel):
    url: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProductionBackupConfig(BaseModel):
    """Operator declarations for the application-level backup Job (PR-065).

    These describe operator intent for the DeerNexus backup evidence layer
    (runbook §9 / §17). They are **declarations**, not the physical DB
    platform backup (pg_dump/WAL/PITR is the DB platform's responsibility —
    runbook §9.1); ``destination_dir`` is where the Job writes its manifest +
    content files so the operator's cron can move them into a separate,
    encrypted failure domain.
    """

    enabled: bool = False
    declared_rpo_hours: int = Field(default=24, ge=1, le=24)
    pitr_enabled: bool = False
    #: Where ``scripts/backup.py`` writes its manifest + per-table content
    #: files. Required (non-null) when ``enabled=True`` — the doctor probe
    #: and the Job both locate the latest manifest here. Defaults to None so
    #: existing configs (pre-PR-065) load unchanged.
    destination_dir: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProductionSecretStoreConfig(BaseModel):
    provider: Literal["env_dev_only", "kubernetes", "vault", "cloud_secret_manager"] = "env_dev_only"
    references_only: bool = False

    model_config = ConfigDict(extra="forbid")


class ProductionArtifactConfig(BaseModel):
    """Agent artifact storage declarations (PR-052, ADR-0004 §11).

    Small artifacts (≤ ``inline_size_threshold`` bytes) are stored inline in
    the ``agent_versions.content_inline`` column; larger artifacts are
    addressed by ``object_key`` and routed through the ``ObjectStore``
    abstraction (``persistence/release/storage.py``). The threshold is an
    operator declaration that enters the capacity plan (ADR §11.1 "阈值由
    生产配置定义并进入压测").

    ``object_store_backend`` selects the storage backend. Only ``inline`` is
    shipped in the MVP (the InlineObjectStore — content_inline is the source of
    truth, no external infra). ``s3`` is a declared future value: a real
    S3/MinIO backend + its doctor probe (private/encrypted guarantees,
    ADR §11.2) land in a follow-up PR, at which point this literal widens.
    """

    inline_size_threshold: int = Field(default=65536, ge=0, description="Bytes; ≤ threshold → inline column, > → object_key.")
    object_store_backend: Literal["inline", "s3"] = "inline"

    model_config = ConfigDict(extra="forbid")


class ProductionAgentReleaseConfig(BaseModel):
    """Run admission release-gate declarations (PR-056, ADR-0004 §6/§12).

    Controls whether ``start_run`` resolves and pins a ``ReleaseRef`` onto
    each new run, and whether legacy (unpinned) runs are gated from
    resume / continue in production.

    ``enforce`` defaults **false** so deploying this code is a pure no-op:
    the resolver is never called, new runs are written with
    ``legacy_unpinned = true`` (the column default), and the resume gate is a
    no-op. An operator flips ``enforce`` to ``true`` only after backfilling
    / confirming the legacy-unpinned count is zero (runbook §14.2), at which
    point ``start_run`` calls ``ReleaseResolver.resolve`` and the resume gate
    returns ``409 release_unpinned`` for any legacy run.

    ``default_channel`` is the only channel source today — sourced from config
    rather than the request body so a client cannot self-select ``dev`` to
    bypass the ``prod`` gate. Per-request channel selection is a follow-up.
    """

    enforce: bool = False
    default_channel: Literal["dev", "staging", "prod"] = "dev"

    model_config = ConfigDict(extra="forbid")


class ProductionLimitsConfig(BaseModel):
    max_concurrent_runs: int = Field(default=1, ge=1)
    max_sandbox_replicas: int = Field(default=1, ge=1)

    model_config = ConfigDict(extra="forbid")


class ProductionGatewaySecurityConfig(BaseModel):
    tls_enabled: bool = False
    cors_origins: list[str] = Field(default_factory=list)
    csrf_enabled: bool = False
    rate_limit_enabled: bool = False

    model_config = ConfigDict(extra="forbid")


class ProductionConfig(BaseModel):
    """Production preflight declarations.

    ``enabled`` defaults to false so the upstream development configuration
    remains safe and backwards compatible.
    """

    enabled: bool = False
    environment: Literal["development", "staging", "production"] = "development"
    deployment: DeploymentProfileConfig = Field(default_factory=DeploymentProfileConfig)
    oidc: ProductionOidcConfig | None = None
    redis: ProductionRedisConfig = Field(default_factory=ProductionRedisConfig)
    backup: ProductionBackupConfig = Field(default_factory=ProductionBackupConfig)
    secret_store: ProductionSecretStoreConfig = Field(default_factory=ProductionSecretStoreConfig)
    artifact: ProductionArtifactConfig = Field(default_factory=ProductionArtifactConfig)
    agent_release: ProductionAgentReleaseConfig = Field(default_factory=ProductionAgentReleaseConfig)
    limits: ProductionLimitsConfig = Field(default_factory=ProductionLimitsConfig)
    gateway_security: ProductionGatewaySecurityConfig = Field(default_factory=ProductionGatewaySecurityConfig)
    log_redaction_enabled: bool = False

    model_config = ConfigDict(extra="forbid")
