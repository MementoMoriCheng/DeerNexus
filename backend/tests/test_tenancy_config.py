"""Tests for ``tenancy.multi_org`` Feature Flag config (PR-025B).

Pins the pydantic schema for the tri-state phase + validation_org coupling
mandated by ``docs/engineering/ci-cd.md`` §11 and
``docs/ops/production-runbook.md`` §5.2, and the additive-wiring onto
``AppConfig`` (no ``tenancy:`` key → safe defaults, phase=disabled).

These are pure schema tests — no DB, no engine. The bootstrap / lifespan /
audit behaviour that consumes this config lives in
``test_validation_org_bootstrap.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig
from deerflow.config.tenancy_config import (
    MULTI_ORG_PHASES,
    MultiOrgConfig,
    TenancyConfig,
    ValidationOrgConfig,
)

_VALID_ORG = {"id": "validation", "slug": "validation", "name": "Validation Org"}


# ===========================================================================
# Defaults & wiring
# ===========================================================================


class TestDefaults:
    def test_multi_org_default_phase_is_disabled(self):
        cfg = MultiOrgConfig()
        assert cfg.phase == "disabled"
        assert cfg.validation_org is None

    def test_tenancy_default_is_safe(self):
        cfg = TenancyConfig()
        assert cfg.multi_org.phase == "disabled"
        assert cfg.multi_org.validation_org is None

    def test_appconfig_has_tenancy_field_with_safe_default(self):
        # AppConfig requires sandbox; supply a minimal one. The point of this
        # test is that the tenancy field exists and defaults safely.
        cfg = AppConfig(sandbox={"use": "LocalSandboxProvider"})
        assert cfg.tenancy.multi_org.phase == "disabled"
        assert cfg.tenancy.multi_org.validation_org is None

    def test_phases_constant_is_exhaustive(self):
        # The registry / doctor validate exhaustively against this tuple; it
        # must stay in lockstep with the Literal type.
        assert MULTI_ORG_PHASES == ("disabled", "validation", "active")


# ===========================================================================
# phase ↔ validation_org coupling (the core invariant)
# ===========================================================================


class TestPhaseOrgConsistency:
    def test_validation_requires_validation_org(self):
        with pytest.raises(ValidationError) as exc_info:
            MultiOrgConfig(phase="validation")
        msg = str(exc_info.value)
        assert "validation" in msg
        assert "validation_org" in msg

    def test_active_requires_validation_org(self):
        with pytest.raises(ValidationError) as exc_info:
            MultiOrgConfig(phase="active")
        msg = str(exc_info.value)
        assert "active" in msg
        assert "validation_org" in msg

    def test_disabled_forbids_validation_org(self):
        with pytest.raises(ValidationError) as exc_info:
            MultiOrgConfig(phase="disabled", validation_org=_VALID_ORG)
        msg = str(exc_info.value)
        assert "disabled" in msg
        assert "validation_org" in msg

    def test_validation_with_org_accepts(self):
        cfg = MultiOrgConfig(phase="validation", validation_org=_VALID_ORG)
        assert cfg.phase == "validation"
        assert isinstance(cfg.validation_org, ValidationOrgConfig)
        assert cfg.validation_org.id == "validation"

    def test_active_with_org_accepts(self):
        cfg = MultiOrgConfig(phase="active", validation_org=_VALID_ORG)
        assert cfg.phase == "active"
        assert cfg.validation_org is not None


# ===========================================================================
# extra=forbid on every level (drift protection)
# ===========================================================================


class TestExtraForbidden:
    def test_validation_org_rejects_extra_keys(self):
        with pytest.raises(ValidationError):
            ValidationOrgConfig(id="v", slug="v", name="v", bogus=True)

    def test_multi_org_rejects_extra_keys(self):
        with pytest.raises(ValidationError):
            MultiOrgConfig(phase="disabled", bogus=True)

    def test_tenancy_rejects_extra_keys(self):
        with pytest.raises(ValidationError):
            TenancyConfig(boguous_section={})

    def test_invalid_phase_rejected(self):
        with pytest.raises(ValidationError):
            MultiOrgConfig(phase="enabled")  # not a valid phase


# ===========================================================================
# YAML round-trip via AppConfig.from_file (the real parse path)
# ===========================================================================


class TestYamlRoundTrip:
    def _write_config(self, tmp_path: Path, tenancy_yaml: str) -> Path:
        # Minimal valid config: sandbox is the only required AppConfig field.
        path = tmp_path / "config.yaml"
        path.write_text(
            "\n".join(
                [
                    "config_version: 16",
                    "sandbox:",
                    "  use: LocalSandboxProvider",
                    "models: []",
                    tenancy_yaml,
                ]
            )
        )
        return path

    def test_disabled_default_when_section_absent(self, tmp_path, monkeypatch):
        path = self._write_config(tmp_path, "")  # no tenancy: key at all
        monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(path))
        cfg = AppConfig.from_file(str(path))
        assert cfg.tenancy.multi_org.phase == "disabled"
        assert cfg.tenancy.multi_org.validation_org is None

    def test_validation_phase_parses_with_org(self, tmp_path, monkeypatch):
        path = self._write_config(
            tmp_path,
            "\n".join(
                [
                    "tenancy:",
                    "  multi_org:",
                    "    phase: validation",
                    "    validation_org:",
                    "      id: validation",
                    "      slug: validation",
                    "      name: Validation Org",
                ]
            ),
        )
        monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(path))
        cfg = AppConfig.from_file(str(path))
        assert cfg.tenancy.multi_org.phase == "validation"
        assert cfg.tenancy.multi_org.validation_org.id == "validation"

    def test_validation_phase_without_org_rejected_at_load(self, tmp_path, monkeypatch):
        path = self._write_config(
            tmp_path,
            "\n".join(["tenancy:", "  multi_org:", "    phase: validation"]),
        )
        monkeypatch.setenv("DEER_FLOW_CONFIG_PATH", str(path))
        with pytest.raises(ValidationError):
            AppConfig.from_file(str(path))
