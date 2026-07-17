"""Tests for the high-risk Feature Flag registry (PR-025B).

Pins the ci-cd §11 eight-field metadata discipline (every high-risk flag
records name/owner/default/environment/dependencies/enable_criteria/
rollback_behavior/expires_at) and the live-state accessor
``current_multi_org_phase``. The registry is static metadata; the live value
comes from config — these tests assert both halves stay in sync and complete.
"""

from __future__ import annotations

from datetime import date

import pytest

from deerflow.tenancy.feature_flags import (
    MULTI_ORG_FLAG,
    current_multi_org_phase,
    get_feature_flag,
    get_feature_flags,
)

# ci-cd §11 mandated fields. Asserted as a tuple so a missing field is a loud
# test failure naming the offender, not a silent None.
_CI_CD_FIELDS = (
    "name",
    "owner",
    "default",
    "environment",
    "dependencies",
    "enable_criteria",
    "rollback_behavior",
    "expires_at",
)


# ===========================================================================
# Registry shape
# ===========================================================================


class TestRegistryShape:
    def test_registry_returns_tuple(self):
        flags = get_feature_flags()
        assert isinstance(flags, tuple)
        assert len(flags) >= 1

    def test_registry_is_immutable_at_runtime(self):
        # Returned container is a tuple, so it has no append/setitem mutation
        # API; the FeatureFlag dataclass is separately frozen (tested below).
        flags = get_feature_flags()
        assert isinstance(flags, tuple)
        with pytest.raises(AttributeError):
            flags.append(MULTI_ORG_FLAG)  # type: ignore[attr-defined]

    def test_registry_names_are_unique(self):
        names = [f.name for f in get_feature_flags()]
        assert len(names) == len(set(names))

    def test_multi_org_flag_is_registered(self):
        assert MULTI_ORG_FLAG in get_feature_flags()


# ===========================================================================
# ci-cd §11 metadata completeness (all 8 fields non-empty)
# ===========================================================================


class TestMultiOrgFlagMetadata:
    def test_all_ci_cd_fields_present_and_non_empty(self):
        for field_name in _CI_CD_FIELDS:
            value = getattr(MULTI_ORG_FLAG, field_name)
            assert value, f"MULTI_ORG_FLAG.{field_name} is empty (ci-cd §11 requires it)"

    def test_name_is_multi_org(self):
        assert MULTI_ORG_FLAG.name == "multi_org"

    def test_default_is_disabled(self):
        # The safe default must match the config schema default.
        assert MULTI_ORG_FLAG.default == "disabled"

    def test_dependencies_list_prerequisites(self):
        # Must reference the Track B prerequisites so the enable gate is
        # auditable; at minimum every entry mentions an org_id-related PR.
        assert isinstance(MULTI_ORG_FLAG.dependencies, list)
        assert len(MULTI_ORG_FLAG.dependencies) >= 1
        joined = " ".join(MULTI_ORG_FLAG.dependencies)
        assert "org_id" in joined.lower() or "PR-02" in joined

    def test_enable_criteria_is_actionable_list(self):
        assert isinstance(MULTI_ORG_FLAG.enable_criteria, list)
        assert len(MULTI_ORG_FLAG.enable_criteria) >= 1

    def test_rollback_behavior_describes_disabled_reversal(self):
        # The reversibility contract: flipping to disabled is the rollback.
        assert "disabled" in MULTI_ORG_FLAG.rollback_behavior.lower()

    def test_expires_at_is_a_future_iso_date(self):
        # ci-cd §11: temporary flags carry a cleanup date. Must parse as a
        # real date and be in the future relative to when this test was
        # written (not a hardcoded past date that was never updated).
        parsed = date.fromisoformat(MULTI_ORG_FLAG.expires_at)
        assert parsed > date(2026, 7, 17), "expires_at must be a future date"


# ===========================================================================
# Lookup helpers
# ===========================================================================


class TestLookup:
    def test_get_feature_flag_by_name(self):
        flag = get_feature_flag("multi_org")
        assert flag is MULTI_ORG_FLAG

    def test_get_feature_flag_unknown_returns_none(self):
        assert get_feature_flag("does_not_exist") is None


# ===========================================================================
# Live phase accessor
# ===========================================================================


class TestCurrentPhase:
    def test_returns_disabled_when_config_not_loaded(self):
        # Without a config.yaml loaded, the accessor must fall back to the
        # safe default rather than raise.
        assert current_multi_org_phase() == "disabled"

    def test_returns_disabled_under_default_appconfig(self, monkeypatch):
        # Pointing the accessor at an AppConfig that has no tenancy section
        # resolves to disabled via the schema default.
        from deerflow.config.app_config import AppConfig

        cfg = AppConfig(sandbox={"use": "LocalSandboxProvider"})
        monkeypatch.setattr("deerflow.config.get_app_config", lambda: cfg, raising=False)
        assert current_multi_org_phase() == "disabled"


# ===========================================================================
# FeatureFlag dataclass is frozen
# ===========================================================================


class TestFeatureFlagFrozen:
    def test_flag_is_frozen(self):
        with pytest.raises(Exception):
            MULTI_ORG_FLAG.name = "tampered"  # type: ignore[misc]
