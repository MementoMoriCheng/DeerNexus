"""Read-side membership lookup for request-path tenant resolution (PR-025C+).

The gateway's tenant resolver (:func:`app.gateway.tenant.resolve_tenant_context`)
switches from single-Org bootstrap to Membership-based org resolution when
``tenancy.multi_org.phase`` leaves ``disabled``. That switch needs a read helper
for ``OrgMembershipRow`` — none existed before this module (only the write-side
:func:`deerflow.tenancy.bootstrap.ensure_admin_membership` probe, which inserts
if absent). This module provides it.

Single-membership-strict semantics
----------------------------------

data-model §4.5 permits a user to hold active memberships in multiple orgs.
Multi-membership *selection* (workspace routing, OIDC-group mapping) is a
later concern; this helper deliberately does NOT pick one silently. It returns:

* ``None`` — the user has zero active memberships (the caller fail-closes);
* the single ``OrgMembershipRow`` — exactly one active membership (unambiguous);
* raises :class:`MultiMembershipError` — more than one active membership (the
  selection policy is undefined here, so the caller fail-closes rather than
  guessing).

This keeps the TEN-008 invariant intact (never synthesize a default org) and
makes "the resolver picked the wrong org for a multi-org user" a loud 503,
not a silent cross-org leak.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.orgs.model import OrgMembershipRow


class MultiMembershipError(Exception):
    """Raised when a user has more than one active OrgMembership.

    Carries the ``user_id`` and ``count`` so the caller (and the 503 response
    path) can surface a precise reason rather than a generic failure. The
    resolver lets this propagate; the middleware's fail-closed 503 wrapper
    catches it.
    """

    def __init__(self, *, user_id: str, count: int) -> None:
        self.user_id = user_id
        self.count = count
        super().__init__(f"user {user_id!r} has {count} active OrgMemberships; multi-membership selection is not implemented (PR-025C+ is single-membership-strict).")


async def get_active_membership(
    sf: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
) -> OrgMembershipRow | None:
    """Return the user's unique active OrgMembership, or None.

    Single-membership-strict (see module docstring):

    - 0 active  → None (caller fail-closes — no membership to bind).
    - 1 active  → that row (its ``org_id`` binds the TenantContext).
    - >1 active → raises :class:`MultiMembershipError` (caller fail-closes).

    Reads only (no commit). The query hits ``idx_org_memberships_user_status``
    on ``(user_id, status)``. Ordered by ``created_at`` ASC so the
    single-membership case is deterministic and a future multi-membership PR
    has a stable baseline to extend.

    Note: ``status="active"`` is the only status data-model §4.5 allows to bind
    a TenantContext (``invited``/``suspended``/``removed`` must not), so the
    filter is on the literal, not a parameter.
    """
    async with sf() as session:
        stmt = (
            select(OrgMembershipRow)
            .where(
                OrgMembershipRow.user_id == user_id,
                OrgMembershipRow.status == "active",
            )
            .order_by(OrgMembershipRow.created_at.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()

    if len(rows) == 0:
        return None
    if len(rows) == 1:
        return rows[0]
    raise MultiMembershipError(user_id=user_id, count=len(rows))


__all__ = ["MultiMembershipError", "get_active_membership"]
