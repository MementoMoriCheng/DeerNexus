"""Tests for ``deerflow.config.observability_config`` (PR-062).

Pins the pydantic schema for the ``observability:`` config section (§2/§3/§5)
and its additive wiring onto ``AppConfig`` (no ``observability:`` key → safe
defaults: text format + no-op tracer). Mirrors ``test_tenancy_config.py``'s
pure-schema-test style.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig
from deerflow.config.observability_config import (
    LOG_FORMATS,
    ObservabilityConfig,
    OtelConfig,
)

# ===========================================================================
# Defaults & wiring
# ===========================================================================


class TestDefaults:
    def test_default_log_format_is_text(self):
        # text keeps today's behaviour — the reversibility guarantee.
        cfg = ObservabilityConfig()
        assert cfg.log_format == "text"

    def test_default_otel_exporter_is_none(self):
        # None = no-op tracer = zero SDK cost on the hot path.
        cfg = ObservabilityConfig()
        assert cfg.otel.exporter_endpoint is None

    def test_default_service_name(self):
        assert ObservabilityConfig().service_name == "deer-flow-gateway"

    def test_default_environment(self):
        assert ObservabilityConfig().environment == "development"

    def test_default_deployment_version_empty(self):
        # Empty suppresses the field rather than writing a placeholder.
        assert ObservabilityConfig().deployment_version == ""

    def test_default_sampler_ratio(self):
        assert ObservabilityConfig().otel.sampler_ratio == 0.1

    def test_default_service_namespace(self):
        assert ObservabilityConfig().otel.service_namespace == "deernexus"

    def test_log_formats_constant_is_exhaustive(self):
        assert LOG_FORMATS == ("text", "json")


class TestAppConfigWiring:
    def test_appconfig_has_observability_field_with_safe_default(self):
        # AppConfig requires sandbox; supply a minimal one (mirrors
        # test_tenancy_config pattern).
        cfg = AppConfig(sandbox={"use": "LocalSandboxProvider"})
        assert cfg.observability.log_format == "text"
        assert cfg.observability.otel.exporter_endpoint is None

    def test_appconfig_accepts_json_log_format(self):
        cfg = AppConfig(sandbox={"use": "LocalSandboxProvider"}, observability={"log_format": "json"})
        assert cfg.observability.log_format == "json"

    def test_appconfig_accepts_otel_endpoint(self):
        cfg = AppConfig(
            sandbox={"use": "LocalSandboxProvider"},
            observability={"otel": {"exporter_endpoint": "http://collector:4318/v1/traces"}},
        )
        assert cfg.observability.otel.exporter_endpoint == "http://collector:4318/v1/traces"


# ===========================================================================
# Validation
# ===========================================================================


class TestValidation:
    def test_invalid_log_format_rejected(self):
        with pytest.raises(ValidationError):
            ObservabilityConfig.model_validate({"log_format": "xml"})

    def test_sampler_ratio_lower_bound_zero(self):
        cfg = ObservabilityConfig(otel=OtelConfig(sampler_ratio=0.0))
        assert cfg.otel.sampler_ratio == 0.0

    def test_sampler_ratio_upper_bound_one(self):
        cfg = ObservabilityConfig(otel=OtelConfig(sampler_ratio=1.0))
        assert cfg.otel.sampler_ratio == 1.0

    @pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
    def test_sampler_ratio_out_of_range_rejected(self, bad: float):
        with pytest.raises(ValidationError):
            ObservabilityConfig(otel=OtelConfig(sampler_ratio=bad))

    def test_extra_keys_in_otel_forbidden(self):
        with pytest.raises(ValidationError):
            OtelConfig.model_validate({"unexpected_field": "x"})

    def test_extra_keys_in_observability_forbidden(self):
        with pytest.raises(ValidationError):
            ObservabilityConfig.model_validate({"rogue": True})
