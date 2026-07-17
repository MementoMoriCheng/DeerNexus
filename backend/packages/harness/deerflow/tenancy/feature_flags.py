"""High-risk Feature Flag registry (PR-025B).

Implements the documentation discipline mandated by ``docs/engineering/ci-cd.md``
§11: every high-risk Flag records eight structured fields (name / owner /
default / environment / dependencies / enable_criteria / rollback_behavior /
expires_at). The registry is the **static metadata** half of the flag — the
authoritative record of what the flag is and how it must be operated. The
**live state** (the current value a deployment sees) lives in config.yaml
under ``tenancy.multi_org.phase`` (see
:mod:`deerflow.config.tenancy_config`); it is read by
:func:`current_multi_org_phase` so callers (and the future doctor, PR-025C)
never have to guess which source is authoritative.

Design split, why:

* **Metadata is a dataclass, not config.** The eight fields are properties of
  the flag itself (who owns it, when it expires, what it depends on), not of
  a particular deployment. Baking them into config would let an operator
  silently change the rollback story or drop a dependency. Keeping them in
  code means they move through code review, exactly like the safety property
  they encode.
* **Live value is config, not code.** ``phase`` is per-environment state that
  changes without a redeploy of the binary's metadata, so it belongs in
  config.yaml where ops already manages it (runbook §4.2 lists "Feature Flag"
  under "非敏感运行配置 → 版本化配置").
* **Doctor reads both (PR-025C).** The runbook §5.2 rule "Doctor 必须读取明确
  迁移阶段，不得只根据 Feature Flag 猜测状态" is enforced by having the doctor
  cross-check the live phase against observed DB state; the registry's
  ``enable_criteria`` is the checklist it runs.

`expires_at` note: ci-cd §11 requires every temporary flag to carry a cleanup
date. The value below is the earliest safe removal (Contract / PR-025D landed
plus one stable window). doctor (PR-025C) will surface a WARN as this date
approaches and a FAIL past it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureFlag:
    """Static metadata for one high-risk Feature Flag (ci-cd §11).

    Frozen so the registry is immutable at runtime; the only way to change a
    flag's contract is a code change, which keeps the safety properties above.
    """

    name: str
    owner: str
    default: str
    environment: str
    dependencies: list[str]
    enable_criteria: list[str]
    rollback_behavior: str
    expires_at: str
    # Human-readable summary surfaced by doctor / docs. Kept separate from the
    # eight ci-cd §11 fields so the registry stays a 1:1 mapping of the spec.
    description: str = ""


# ---------------------------------------------------------------------------
# Registered flags
# ---------------------------------------------------------------------------

MULTI_ORG_FLAG = FeatureFlag(
    name="multi_org",
    owner="Track B / tenancy",
    default="disabled",
    environment="all",
    description=("Tri-state phase (disabled/validation/active) gating the multi-org rollout. PR-025B lands the mechanism; the request-path tenant resolver stays single-Org until PR-025C+ switches to membership-based org resolution."),
    dependencies=[
        "PR-021 org_id nullable column on 4 resource tables",
        "PR-023 default-org backfill (zero NULL org_id rows)",
        "PR-024 repository org-scope hard filter + fail-closed",
        "PR-025A enforce org_id NOT NULL + threads_meta compound unique (migration 0006)",
    ],
    enable_criteria=[
        "Enforce constraints live in production (PR-025A)",
        "Dual-Org isolation matrix green in production",
        "Zero NULL org_id rows across all 4 resource tables",
    ],
    rollback_behavior=(
        "Set tenancy.multi_org.phase=disabled. The resolver is already single-Org in "
        "PR-025B, so flipping back is a config change with no code rollback. The validation "
        "Org row may remain (harmless — non-public, receives no traffic); an operator may "
        "soft-delete it via the control plane. No historical data is unreadable when the "
        "flag is off (org_id NOT NULL is unaffected by the flag)."
    ),
    # Earliest safe removal: after Contract (PR-025D) lands and one stable
    # observation window passes. doctor (PR-025C) enforces a concrete date.
    expires_at="2026-10-31",
)


def get_feature_flags() -> tuple[FeatureFlag, ...]:
    """Return all registered high-risk Feature Flags.

    A tuple (immutable) so callers cannot mutate the registry in place. Order
    is registration order; tests pin it for stable doctor output.
    """
    return (MULTI_ORG_FLAG,)


def get_feature_flag(name: str) -> FeatureFlag | None:
    """Look up a flag by name, or ``None`` if unregistered."""
    for flag in get_feature_flags():
        if flag.name == name:
            return flag
    return None


def current_multi_org_phase() -> str:
    """Read the live ``tenancy.multi_org.phase`` from config.

    Deferred import of ``get_app_config`` so this module (which sits in the
    harness layer) does not pull config machinery at import time — tests that
    only want the static registry metadata should not need a loadable
    config.yaml. Returns ``"disabled"`` if config has not been loaded yet
    (matches the schema default and is the safe state).

    This is the single read-point for the live flag value; doctor (PR-025C)
    and any future consumer go through here rather than reaching into AppConfig
    directly, so the source-of-truth rule stays in one place.
    """
    try:
        from deerflow.config import get_app_config

        return get_app_config().tenancy.multi_org.phase
    except Exception:  # noqa: BLE001 — config not loaded; safe default
        return "disabled"


__all__ = [
    "MULTI_ORG_FLAG",
    "FeatureFlag",
    "current_multi_org_phase",
    "get_feature_flag",
    "get_feature_flags",
]
