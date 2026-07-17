"""Tenant / multi-org configuration consumed by the Feature Flag machinery (PR-025B).

Introduces the ``tenancy.multi_org.phase`` tri-state Feature Flag and the
optional non-public validation Org declared in
``docs/architecture/data-model.md`` §13.3 and
``docs/engineering/ci-cd.md`` §10.3 / §11. The flag gates operator intent; the
request-path tenant resolver (PR-013/014, ``app/gateway/tenant.py``) stays
single-Org in PR-025B and does **not** consume this flag — that switch is
deferred to PR-025C+. Keeping the resolver untouched is what makes PR-025B
reversible: ``phase=disabled`` is bit-for-bit today's behaviour.

Phase semantics (runbook §5.2 "租户迁移状态判定"):

- ``disabled`` (default): single-Org mode. Exactly one Org exists
  (``default_org_id`` from ``app/gateway/config.py``). Every request still
  resolves to it. No validation Org is seeded.
- ``validation``: operator has confirmed the Enforce prerequisites
  (``org_id NOT NULL`` live, dual-Org isolation matrix green in production,
  zero NULL ``org_id`` rows) and wants a non-public second Org created so the
  validation cohort can exercise the isolation boundary at runtime. The
  resolver is still single-Org — the validation Org receives no traffic in
  PR-025B; it exists as an audited target whose row the operator can later
  bind principals to.
- ``active``: multi-org is open to real tenants. Reached only by an explicit
  operator action after the CD gate in ci-cd §10.3. PR-025B only lands the
  mechanism; it never flips to ``active``.

Invariants enforced at the pydantic layer (``_check_phase_org_consistency``):

- ``validation`` / ``active`` **require** ``validation_org`` to be set (the
  validation Org is the migration milestone; its absence in an open state is
  a misconfiguration, not a runtime default).
- ``disabled`` **forbids** ``validation_org`` (a leftover validation Org
  declaration while disabled signals drift the operator should resolve
  explicitly rather than silently ignored).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Canonical phase values. Exported as a tuple so callers (doctor, tests) can
# validate exhaustively without importing the Literal type.
MULTI_ORG_PHASES: tuple[str, ...] = ("disabled", "validation", "active")


class ValidationOrgConfig(BaseModel):
    """Identity of the non-public validation Org (data-model §13.3).

    ``id`` / ``slug`` / ``name`` are the same three fields
    :func:`deerflow.tenancy.bootstrap.ensure_default_org` takes; the bootstrap
    helper receives them verbatim so no translation layer sits between config
    and DB row. ``slug`` is platform-unique among non-deleted orgs
    (``OrganizationRow.uq_organizations_slug_active``), so it must not collide
    with the default Org's slug.
    """

    id: str = Field(description="Stable Org id (String(36)); must not equal the default org id.")
    slug: str = Field(description="Platform-unique slug among non-deleted orgs; must not equal the default org slug.")
    name: str = Field(description="Human-readable name for the validation Org.")

    model_config = ConfigDict(extra="forbid")


class MultiOrgConfig(BaseModel):
    """The multi-org Feature Flag + its validation-Org payload."""

    phase: Literal["disabled", "validation", "active"] = Field(
        default="disabled",
        description=(
            "Tri-state Feature Flag (PR-025B). 'disabled' = single-Org (today's behaviour); "
            "'validation' = seed the non-public validation Org, resolver still single-Org; "
            "'active' = multi-org open to real tenants (operator CD action, gated by ci-cd §10.3)."
        ),
    )
    validation_org: ValidationOrgConfig | None = Field(
        default=None,
        description="Required when phase is 'validation' or 'active'; forbidden when 'disabled'.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_phase_org_consistency(self) -> MultiOrgConfig:
        """Enforce the phase ↔ validation_org coupling documented at module top.

        Split by phase rather than a blanket rule so the error message names
        the offending phase, which is what an operator triaging a startup
        failure needs to act on.
        """
        if self.phase in ("validation", "active"):
            if self.validation_org is None:
                raise ValueError(f"tenancy.multi_org.phase='{self.phase}' requires tenancy.multi_org.validation_org to be set (the non-public validation Org is the migration milestone for this phase).")
        else:  # disabled
            if self.validation_org is not None:
                raise ValueError("tenancy.multi_org.phase='disabled' forbids tenancy.multi_org.validation_org (remove the validation_org block or set phase to 'validation'/'active').")
        return self


class TenancyConfig(BaseModel):
    """Top-level ``tenancy:`` config section. Additive; defaults are safe."""

    multi_org: MultiOrgConfig = Field(
        default_factory=MultiOrgConfig,
        description="Multi-org Feature Flag + validation Org (PR-025B).",
    )

    model_config = ConfigDict(extra="forbid")
