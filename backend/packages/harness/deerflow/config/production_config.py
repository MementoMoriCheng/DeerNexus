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
    limits: ProductionLimitsConfig = Field(default_factory=ProductionLimitsConfig)
    gateway_security: ProductionGatewaySecurityConfig = Field(default_factory=ProductionGatewaySecurityConfig)
    log_redaction_enabled: bool = False

    model_config = ConfigDict(extra="forbid")
