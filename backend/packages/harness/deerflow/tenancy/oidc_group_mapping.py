"""OIDC group → Role mapping engine + last-admin policy (PR-036, ADR-0003 §10).

This module is the harness-layer core of PR-036. It turns an authenticated
OIDC subject's group claims into ``role_bindings`` rows (the **additive**
MVP mode) and guards the last-org-admin invariant (ADR-0003 §7). It lives
in ``deerflow.tenancy`` (not ``app``) so it imports only persistence +
contracts — the harness-boundary test (``test_harness_boundary``) enforces
that.

ADR §10 — additive MVP rules this module enforces
-------------------------------------------------

1. **allowlist only** — mapping rows ARE the allowlist; an unmatched
   ``(issuer, group)`` is ignored.
2. **no auto-create roles** — the service never creates a role; it
   references an existing ``roles.id``.
3. **no system permissions** — a target role carrying any ``system:*``
   permission is refused (defence-in-depth; the registry already forbids
   them on Org roles at write time, but a mapping must not widen one).
4. **union** — free: multiple groups for one user each ensure their own
   binding, and ``AuthorizeService._fetch_role_permissions`` already
   unions role_bindings.
5. **audit** — every applied / dry-run / skipped mapping emits a
   ``emit_tenant_event`` (logger shim; real outbox in PR-041).
6. **no auto-delete manual** — additive only *ensures* bindings; it never
   removes. The ``created_by`` sentinel records provenance but does not
   distinguish rows for removal because additive never removes.
7. **authoritative gated** — an ``authoritative`` mapping row is *logged
   and skipped*: the destructive "IdP group removed → delete binding"
   semantics are a separately-enabled future mode. The column is stored
   so that future mode can be switched on without a schema change.
8. **email domain no admin** — no email-domain mapping feature exists;
   only issuer + group maps.

IdP-agnostic input
------------------

The engine takes ``(issuer, groups: list[str])`` directly. The real OIDC
code-flow / JWKS transport (security baseline §3.1) is a separate PR;
this module has no dependency on it, so it can be tested with mock IdP
claims today (the PR-036 IdP E2E).

Multi-membership
----------------

The user's active membership resolves the *target org*. The engine acts
only when a mapping rule's ``target_org_id`` equals the user's existing
active-membership org — it never auto-provisions a new org membership
(multi-membership *selection* is explicitly deferred to a post-PR-036
PR per ``runtime-contracts.md``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.iam.model import (
    RoleBindingRow,
    RoleRow,
)
from deerflow.persistence.iam.repository import (
    MAPPING_MODE_AUTHORITATIVE,
    count_user_bindings_for_role,
    list_oidc_group_mappings,
)
from deerflow.persistence.orgs.model import ExternalIdentityRow
from deerflow.tenancy.audit_events import emit_tenant_event
from deerflow.tenancy.membership import (
    get_active_membership,
)

logger = logging.getLogger(__name__)

#: ``created_by`` sentinel stamped on every group-derived binding (ADR §10
#: rule 6 provenance). Additive never removes, so a dedicated ``source``
#: column is deferred until ``authoritative`` mode actually needs to
#: distinguish group-derived from manual rows for deletion. The sentinel
#: is a stable, greppable prefix so a future authoritative sweep can
#: identify candidate rows.
GROUP_MAPPING_PROVENANCE_PREFIX = "oidc-group-mapping"


def _provenance(issuer: str, group: str) -> str:
    """Build the ``created_by`` sentinel for one (issuer, group) mapping."""
    return f"{GROUP_MAPPING_PROVENANCE_PREFIX}:{issuer}:{group}"


class LastAdminError(Exception):
    """Raised when an action would leave an Org with zero ``org:admin`` bindings.

    ADR-0003 §7: "最后一个 ``org:admin`` 不得通过普通请求被删除、暂停或解绑".
    Carries the org/role/count so the caller (and the 409 response path)
    can surface a precise reason. Emergency removal of the last admin
    requires the system-admin dedicated flow + two-person approval
    record (§7), which this module deliberately does NOT provide.
    """

    def __init__(self, *, org_id: str, role_id: str, remaining: int) -> None:
        self.org_id = org_id
        self.role_id = role_id
        self.remaining = remaining
        super().__init__(f"Refusing to remove the last org:admin binding in org {org_id!r} (role {role_id!r}); remaining after removal would be {remaining}. Emergency last-admin removal requires the system-admin dedicated flow.")


@dataclass
class MappingOutcome:
    """One mapping rule's disposition for one user (applied or skipped + why)."""

    group_value: str
    target_role_id: str
    target_org_id: str
    applied: bool
    reason: str = ""


@dataclass
class MappingResult:
    """Aggregate disposition of ``apply_group_mapping`` for one login.

    ``planned`` lists what *would* happen (populated in both dry-run and
    live mode); ``applied`` lists what actually wrote a binding (empty in
    dry-run); ``skipped`` lists rules that were not evaluated (wrong org,
    authoritative mode, system-permission target, etc.). The dry-run
    preview endpoint returns this verbatim.
    """

    user_id: str
    issuer: str
    dry_run: bool
    planned: list[MappingOutcome] = field(default_factory=list)
    applied: list[MappingOutcome] = field(default_factory=list)
    skipped: list[MappingOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# last-admin policy primitive (ADR-0003 §7)
# ---------------------------------------------------------------------------


async def assert_not_last_admin(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    role_id: str,
    principal_id: str,
) -> None:
    """Refuse to remove the last ``role_id`` binding for a user principal.

    ADR-0003 §7: the last ``org:admin`` must not be removed via a normal
    request. This helper counts the *other* non-expired user bindings for
    ``role_id`` (excluding the principal under removal); if that count is
    zero, raises :class:`LastAdminError`. The caller MUST emit the audit
    event on the failure path (ADR §13 "最后管理员保护触发").

    ``role_id`` is parameterised — today every caller passes the
    ``org:admin`` role, but the guard is role-generic so a future
    "last-{role}" protection reuses it.

    Reads only on the success path; raises (no write) on the refusal
    path. The caller is responsible for any cache invalidation after the
    *permitted* removal.

    Precise semantics: the guard fires only when removal would *actually*
    leave the Org with zero bindings — i.e. the principal currently holds
    the role AND no other non-expired user binding exists. A removal of a
    principal that does NOT hold the role is a no-op and is permitted
    (so the bootstrap ``/initialize`` first-admin path, or a repeat of an
    already-applied removal, never trips the guard).
    """
    remaining = await count_user_bindings_for_role(
        sf,
        org_id=org_id,
        role_id=role_id,
        exclude_principal_id=principal_id,
    )
    if remaining > 0:
        return  # at least one other admin survives — safe to remove.
    # remaining == 0: removal would leave zero — UNLESS the principal has
    # no binding to remove. Distinguish so the guard never fires on a
    # phantom principal (total == 0 means there is nothing to protect).
    total = await count_user_bindings_for_role(sf, org_id=org_id, role_id=role_id)
    if total == 0:
        return
    # total > 0 and remaining == 0 ⇒ the principal is the sole admin.
    emit_tenant_event(
        "last_admin_protection_triggered",
        org_id=org_id,
        principal_id=principal_id,
        payload={"role_id": role_id, "remaining": 0},
    )
    raise LastAdminError(org_id=org_id, role_id=role_id, remaining=0)


# ---------------------------------------------------------------------------
# external identity upsert (brings §4.4 table to life)
# ---------------------------------------------------------------------------


async def upsert_external_identity(
    sf: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    issuer: str,
    subject: str,
    provider: str,
    claims_snapshot: dict,
) -> ExternalIdentityRow:
    """Insert or update the federated-identity link row (data-model §4.4).

    Probes the ``(issuer, subject)`` unique constraint; inserts if absent,
    otherwise overwrites ``claims_snapshot`` (the latest login wins —
    group membership drifts over time, so the snapshot must reflect the
    most recent IdP assertion). ``user_id`` is fixed by the unique key:
    a re-bind of an existing ``(issuer, subject)`` to a *different* user
    is a deployment anomaly that should surface as an explicit operator
    decision, not a silent overwrite, so this helper does not change it.

    Stores ONLY the allowlisted claim shape (the caller passes
    ``{group_claim: groups}``); it never stores raw tokens (security
    baseline §3.1 "claims_snapshot 仅允许白名单 claims,不含 Token").
    """
    async with sf() as session:
        stmt = select(ExternalIdentityRow).where(
            ExternalIdentityRow.issuer == issuer,
            ExternalIdentityRow.subject == subject,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.claims_snapshot = dict(claims_snapshot)
            await session.commit()
            await session.refresh(existing)
            return existing

        row = ExternalIdentityRow(
            id=uuid.uuid4().hex,
            user_id=user_id,
            issuer=issuer,
            subject=subject,
            provider=provider,
            claims_snapshot=dict(claims_snapshot),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# the mapping engine — ADR §10 additive apply
# ---------------------------------------------------------------------------


async def _target_role_safe(
    sf: async_sessionmaker[AsyncSession],
    *,
    role_id: str,
) -> RoleRow | None:
    """Return the target role row, or ``None`` if it carries system perms.

    ADR §10 rule 3: a group cannot map to a role that grants any
    ``system:*`` permission. The registry forbids system perms on Org
    roles at write time, but a mapping referencing a stale/mis-seeded
    role must not widen it — this is the defence-in-depth read.
    """
    from deerflow.contracts.rbac import SYSTEM_PERMISSION_PREFIX

    async with sf() as session:
        role = await session.get(RoleRow, role_id)
    if role is None:
        return None
    perms = role.permissions or []
    if any(isinstance(p, str) and p.startswith(SYSTEM_PERMISSION_PREFIX) for p in perms):
        return None
    return role


async def apply_group_mapping(
    sf: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    issuer: str,
    groups: list[str],
    provider: str = "oidc",
    subject: str | None = None,
    dry_run: bool = False,
) -> MappingResult:
    """Apply the OIDC group-mapping allowlist for one authenticated subject.

    The PR-036 MVP entry point. Walks every ``oidc_group_mappings`` row
    whose ``issuer`` matches and whose ``group_value`` is in ``groups``
    (rule 1 allowlist), and for each:

    * skips if ``mode == authoritative`` (rule 7 — not separately enabled);
    * skips if the rule's ``target_org_id`` != the user's active-membership
      org (multi-membership selection is deferred);
    * skips if the target role is missing or carries system permissions
      (rule 3);
    * otherwise records the binding as ``planned`` and, when not
      ``dry_run``, ensures the ``RoleBindingRow`` (idempotent) with the
      provenance ``created_by`` sentinel + upserts the federated-identity
      link.

    Returns a :class:`MappingResult` so the dry-run preview and the live
    path share one return shape. Every disposition emits an
    ``emit_tenant_event`` (rule 5 audit). The live path never removes a
    binding (rule 6), so last-admin protection is trivially satisfied
    for additive mapping — it is exercised separately by the removal path.

    ``subject`` is the OIDC ``sub`` claim; required for the
    ``external_identities`` upsert. When ``None`` the upsert is skipped
    (the binding still lands; the federated link is observability-only).
    """
    result = MappingResult(user_id=user_id, issuer=issuer, dry_run=dry_run)

    # Resolve the user's active membership to learn the target org. The
    # engine acts only on the user's existing active-membership org —
    # never auto-provisions a new org membership (multi-membership
    # selection deferred per runtime-contracts.md).
    membership = await get_active_membership(sf, user_id=user_id)  # may raise MultiMembershipError
    if membership is None:
        emit_tenant_event(
            "oidc_group_mapping_no_membership",
            org_id=None,
            principal_id=user_id,
            payload={"issuer": issuer, "reason": "no_active_membership"},
        )
        return result

    target_org_id = membership.org_id

    # Load every allowlist row for this issuer (the rule's group_value is
    # the second filter — applied against the IdP-provided groups list).
    rules = await list_oidc_group_mappings(sf, issuer=issuer)
    group_set = set(groups)

    for rule in rules:
        if rule.group_value not in group_set:
            continue  # rule 1: unmatched group is not mapped

        outcome = MappingOutcome(
            group_value=rule.group_value,
            target_role_id=rule.target_role_id,
            target_org_id=rule.target_org_id,
            applied=False,
        )

        # rule 7: authoritative mode is stored but NOT enacted.
        if rule.mode == MAPPING_MODE_AUTHORITATIVE:
            outcome.reason = "authoritative_mode_not_enabled"
            emit_tenant_event(
                "oidc_group_mapping_authoritative_not_enabled",
                org_id=rule.target_org_id,
                principal_id=user_id,
                payload={"mapping_id": rule.id, "group_value": rule.group_value},
            )
            result.skipped.append(outcome)
            continue

        # Multi-membership deferral: only act on the user's existing org.
        if rule.target_org_id != target_org_id:
            outcome.reason = "target_org_not_user_membership"
            emit_tenant_event(
                "oidc_group_mapping_skipped_org",
                org_id=rule.target_org_id,
                principal_id=user_id,
                payload={"mapping_id": rule.id, "user_org": target_org_id},
            )
            result.skipped.append(outcome)
            continue

        # rule 3: the target role must exist and carry no system perms.
        role = await _target_role_safe(sf, role_id=rule.target_role_id)
        if role is None:
            outcome.reason = "target_role_missing_or_system"
            emit_tenant_event(
                "oidc_group_mapping_skipped_role",
                org_id=target_org_id,
                principal_id=user_id,
                payload={"mapping_id": rule.id, "role_id": rule.target_role_id},
            )
            result.skipped.append(outcome)
            continue

        result.planned.append(outcome)

        if dry_run:
            outcome.applied = False
            outcome.reason = "dry_run"
            emit_tenant_event(
                "oidc_group_mapping_dry_run",
                org_id=target_org_id,
                principal_id=user_id,
                payload={
                    "mapping_id": rule.id,
                    "group_value": rule.group_value,
                    "role_id": rule.target_role_id,
                    "role_name": role.name,
                },
            )
            continue

        # Live apply: ensure the binding (idempotent on the unique
        # constraint) with the provenance sentinel.
        await _ensure_group_role_binding(
            sf,
            org_id=target_org_id,
            user_id=user_id,
            role_id=rule.target_role_id,
            provenance=_provenance(issuer, rule.group_value),
        )
        outcome.applied = True
        emit_tenant_event(
            "oidc_group_mapping_applied",
            org_id=target_org_id,
            principal_id=user_id,
            payload={
                "mapping_id": rule.id,
                "group_value": rule.group_value,
                "role_id": rule.target_role_id,
                "role_name": role.name,
            },
        )
        result.applied.append(outcome)

    # Upsert the federated link once per apply (not per rule) — the
    # claims_snapshot is the full group set, the binding fan-out above is
    # what materializes roles. Skipped in dry-run (no state change) and
    # when subject is unavailable.
    if not dry_run and subject is not None and (result.applied or result.planned):
        rule_claim = next((r.group_claim for r in rules if r.group_value in group_set), "groups")
        await upsert_external_identity(
            sf,
            user_id=user_id,
            issuer=issuer,
            subject=subject,
            provider=provider,
            claims_snapshot={rule_claim: list(groups)},
        )

    return result


async def _ensure_group_role_binding(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    user_id: str,
    role_id: str,
    provenance: str,
) -> RoleBindingRow:
    """Idempotently ensure a user-principal RoleBinding with a provenance stamp.

    Probes the ``(org_id, principal_type='user', principal_id, role_id)``
    unique constraint before inserting, mirroring
    :func:`deerflow.tenancy.bootstrap._ensure_role_binding`. Uses
    :func:`create_role_binding` (the repository writer) so the row carries
    the ``created_by`` provenance sentinel — the bootstrap helper does not
    accept ``created_by`` and is reserved for the first-admin path.

    Re-entrant: a binding that already exists (manual or from a prior
    login) is left as-is — its ``created_by`` is NOT overwritten, so a
    manual binding keeps its human attribution.
    """
    async with sf() as session:
        stmt = select(RoleBindingRow).where(
            RoleBindingRow.org_id == org_id,
            RoleBindingRow.principal_type == "user",
            RoleBindingRow.principal_id == user_id,
            RoleBindingRow.role_id == role_id,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        row = RoleBindingRow(
            id=uuid.uuid4().hex,
            org_id=org_id,
            principal_type="user",
            principal_id=user_id,
            role_id=role_id,
            created_by=provenance,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


__all__ = [
    "GROUP_MAPPING_PROVENANCE_PREFIX",
    "LastAdminError",
    "MappingOutcome",
    "MappingResult",
    "apply_group_mapping",
    "assert_not_last_admin",
    "upsert_external_identity",
]
