"""Observability configuration consumed by the logging / tracing setup (PR-062).

Introduces the ``observability:`` config section that gates structured JSON
logging and OpenTelemetry export (``docs/ops/observability-and-slo.md`` §2/§3/§5).

Defaults are deliberately today's behaviour — ``log_format="text"`` keeps the
existing plain-text ``%(asctime)s - %(name)s - %(levelname)s - %(message)s``
output, and ``otel.exporter_endpoint=None`` keeps the OTel tracer as a no-op
(API-layer proxy). Operators opt in to structured output and OTLP export
explicitly via ``config.yaml`` so PR-062 is reversible: turning both off is a
pure config change with no code rollback, mirroring the Feature-Flag discipline
of PR-025B.

Cross-references:

* §2 关联 ID — correlation id field set that flows from this config's
  ``service_name`` / ``environment`` / ``deployment_version``.
* §3 结构化日志 — JSON field shape; ``log_format`` toggles between text and
  JSON.
* §5 Trace — OTel SDK / Collector wiring (``OtelConfig``).
* §15 实现映射 — item 1 (OTel SDK / Collector config) and item 5 (sampling
  config) are backfilled by this module's defaults.

This module lives in ``deerflow.config`` (not ``deerflow.observability``) to
match the established split where typed config models live alongside their
peers (``tenancy_config.py``, ``production_config.py``, …) while the runtime
package (``deerflow.observability``) holds the machinery that consumes them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Canonical log format values. Exported as a tuple so callers (doctor, tests)
# can validate exhaustively without importing the Literal type.
LOG_FORMATS: tuple[str, ...] = ("text", "json")


class OtelConfig(BaseModel):
    """OpenTelemetry SDK / Collector configuration (observability-and-slo §5).

    ``exporter_endpoint=None`` keeps the SDK uninitialised; ``trace.get_tracer``
    then returns the API-layer no-op tracer so any ``start_as_current_span``
    call site is a zero-cost context manager. Setting an endpoint opts the
    process into OTLP export via a ``BatchSpanProcessor``.

    ``sampler_ratio`` is the head-sampling ratio for ordinary traffic. The §5.4
    tail-based rule ("errors / Policy deny / Sandbox violations 100% retained")
    is **not** implemented in PR-062 — it requires the deny / violation code
    paths to exist (Track C / Track E). The default ``ParentBased(TraceIdRatioBased)``
    head sampler shipped here is the documented fallback until tail sampling
    lands in a follow-up PR; the TODO in ``tracing.init_tracing`` records it.
    """

    exporter_endpoint: str | None = Field(
        default=None,
        description=("OTLP/gRPC or OTLP/HTTP endpoint URL for span export. None (default) keeps the tracer a no-op; setting it enables export."),
    )
    sampler_ratio: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Head-sampling ratio for ordinary traffic (0.0–1.0). Ignored when exporter_endpoint is None.",
    )
    service_namespace: str = Field(
        default="deernexus",
        description="OTel resource service.namespace attribute (groups DeerNexus services).",
    )

    model_config = ConfigDict(extra="forbid")


class MetricsConfig(BaseModel):
    """Prometheus metrics endpoint configuration (observability-and-slo §4).

    Unlike OTel tracing (no-op by default because export has non-trivial cost),
    metrics are **enabled by default**: the cost of in-process counter/histogram
    bumps is negligible compared to traces, and every §6 SLO is computed from
    the full-counters §4.2–§4.6 metrics (§6 "所有 SLI 分子和分母来自 §4 的全量
    计数 Metrics … 不得用采样 Trace / 日志替代"). Disabling metrics therefore
    blinds the SLO dashboards and is an operator opt-out, not the safe default.

    ``route`` is public (no auth) — industry convention; §4.1 already forbids
    high-cardinality id labels (org_id / user_id / run_id / …) so the endpoint
    carries no sensitive data. Deployments that want to restrict it should use
    an ingress rule rather than auth (which would make Prometheus scrapes
    fiddlier). Setting ``enabled=false`` 404s the route and short-circuits the
    bump call sites.
    """

    enabled: bool = Field(
        default=True,
        description="Expose /metrics and bump counters/histograms/gauges. Default true — metrics are cheap and every SLO depends on them.",
    )
    route: str = Field(
        default="/metrics",
        description="HTTP path for the Prometheus scrape endpoint. Public (no auth); restrict via ingress if needed.",
    )

    model_config = ConfigDict(extra="forbid")


class ObservabilityConfig(BaseModel):
    """Top-level ``observability:`` config section. Additive; defaults are safe.

    ``service_name`` / ``environment`` / ``deployment_version`` become the
    constant correlation fields on every log line (§2) and the OTel resource
    attributes (§5). ``deployment_version`` is intentionally empty by default
    — CI injects it; an empty value suppresses the field rather than writing a
    placeholder (the formatter omits empty deployment_version to avoid
    polluting queries).
    """

    log_format: Literal["text", "json"] = Field(
        default="text",
        description=("Log output format. 'text' (default) keeps today's human-readable format; 'json' emits one JSON object per line per observability-and-slo §3.1."),
    )
    service_name: str = Field(
        default="deer-flow-gateway",
        description="Service identity used in log records and the OTel resource.",
    )
    environment: str = Field(
        default="development",
        description="Deployment environment (development / staging / production).",
    )
    deployment_version: str = Field(
        default="",
        description="Application deployment version (CI-injected). Empty suppresses the field.",
    )
    otel: OtelConfig = Field(
        default_factory=OtelConfig,
        description="OpenTelemetry SDK / Collector configuration (PR-062). No-op by default.",
    )
    metrics: MetricsConfig = Field(
        default_factory=MetricsConfig,
        description="Prometheus metrics endpoint (PR-063). Enabled by default — metrics are cheap and every SLO depends on them.",
    )

    model_config = ConfigDict(extra="forbid")
