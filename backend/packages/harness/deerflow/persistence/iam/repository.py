"""DB CRUD for the IAM control-plane tables (PR-034 / PR-036).

Pure data-access layer — no audit, no cache, no authz. The app layer
(``app/gateway/routers/iam.py``) is responsible for emitting audit
events and invalidating the AuthorizeService cache after writes; this
module owns only the DB mutation and the Org-scoped read filter.

Conventions (mirror ``tenancy/bootstrap.py``):

* Each function opens its own ``AsyncSession`` from the supplied
  ``async_sessionmaker`` so a parent commit lands before any child
  read. Multi-statement transactions (``delete_service_account``) wrap
  the whole sequence in a single ``async with sf() as session:`` block
  so the commit is atomic — ADR §12 requires SA deletion and full Key
  revocation in the same transaction.
* All writes commit before returning; the app layer is responsible for
  the post-commit ``emit_tenant_event`` + ``invalidate_principal`` calls.
* Reads always filter by ``org_id`` (ADR §8 "列表与查询强制 Org 过滤") —
  a missing ``org_id`` in a get/list is a programming error and the
  helper raises ``ValueError`` rather than silently scanning all Orgs.

Polymorphic principal note (data-model.md §5.2): ``role_bindings.principal_id``
has NO FK to ``users.id`` or ``service_accounts.id``. SA deletion must
explicitly DELETE the ``role_bindings`` rows for the principal — the
DB does not cascade them. ``api_keys.service_account_id`` IS a real FK
with ``ondelete=CASCADE`` (0004_iam_tables.py:151-155), so the SA
delete implicitly removes dependent keys.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.iam.model import (
    ApiKeyRow,
    OidcGroupMappingRow,
    RoleBindingRow,
    ServiceAccountRow,
)
from deerflow.persistence.orgs.model import OrgMembershipRow

#: Status values allowed by the ``ck_service_accounts_status`` CHECK.
#: ``deleted`` is NOT a row state — SA deletion is a hard DELETE (ADR §12
#: requires SA deletion + Key revocation in the same transaction, not a
#: soft-delete tombstone).
SERVICE_ACCOUNT_ACTIVE = "active"
SERVICE_ACCOUNT_DISABLED = "disabled"
_ALLOWED_STATUSES: frozenset[str] = frozenset({SERVICE_ACCOUNT_ACTIVE, SERVICE_ACCOUNT_DISABLED})

#: Status values allowed by the ``ck_org_memberships_status`` CHECK
#: (data-model §4.5). The caller-facing lifecycle helpers
#: (``set_membership_status``) only exercise ``active ↔ suspended``;
#: ``invited`` / ``removed`` are reached by invite / remove flows not
#: in this PR's scope.
MEMBERSHIP_ACTIVE = "active"
MEMBERSHIP_SUSPENDED = "suspended"
_ALLOWED_MEMBERSHIP_STATUSES: frozenset[str] = frozenset({MEMBERSHIP_ACTIVE, MEMBERSHIP_SUSPENDED})

#: Fields the app layer may PATCH on a ServiceAccount via ``update_service_account``.
#: ``status`` is deliberately NOT in this set — it has its own
#: ``set_service_account_status`` helper so the lifecycle transition is
#: explicit at the call site (matches ADR §9.1 "active ↔ disabled → deleted"
#: state machine).
_UPDATABLE_FIELDS: frozenset[str] = frozenset({"name", "description", "owner_user_id", "purpose", "system", "environment", "expires_at"})

#: Sampling window for ``touch_api_key_last_used``. At most one ``UPDATE``
#: per Key per window so a high-QPS API-key-authenticated request stream
#: does not turn into a per-request DB write. Matches the ADR §11 60s
#: cache/TTL granularity — anything finer buys nothing because cached
#: authz decisions are refreshed on at most the same cadence.
_LAST_USED_SAMPLE_WINDOW = timedelta(seconds=60)

#: ``OidcGroupMappingRow.mode`` values allowed by the
#: ``ck_oidc_group_mappings_mode`` CHECK (ADR-0003 §10). MVP ships
#: ``additive`` only; ``authoritative`` is stored but the mapping service
#: refuses to enact it (ADR §10 "authoritative 模式需单独启用").
MAPPING_MODE_ADDITIVE = "additive"
MAPPING_MODE_AUTHORITATIVE = "authoritative"
_ALLOWED_MAPPING_MODES: frozenset[str] = frozenset({MAPPING_MODE_ADDITIVE, MAPPING_MODE_AUTHORITATIVE})

#: Fields the app layer may PATCH on an OIDC group mapping via
#: ``update_oidc_group_mapping``. ``mode`` is included so an operator can
#: flip a rule to ``authoritative`` in storage (the service still refuses
#: to *enact* authoritative until a future "separately enabled" PR).
_MAPPING_UPDATABLE_FIELDS: frozenset[str] = frozenset({"group_claim", "group_value", "target_role_id", "mode", "description"})


def _new_id() -> str:
    """Generate a 36-char hex id matching the ``String(36)`` convention."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# ServiceAccount CRUD
# ---------------------------------------------------------------------------


async def create_service_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    name: str,
    description: str | None = None,
    owner_user_id: str | None = None,
    purpose: str | None = None,
    system: str | None = None,
    environment: str | None = None,
    expires_at: datetime | None = None,
    created_by: str | None = None,
    session: AsyncSession | None = None,
) -> ServiceAccountRow:
    """Insert one ``ServiceAccountRow`` with ``status="active"``.

    The ``(org_id, name)`` unique constraint (``uq_service_accounts_org_name``)
    raises ``IntegrityError`` on collision; the app layer maps that to 409.

    Pass an open ``session`` to stage the insert inside a caller-owned
    transaction (the Class A same-transaction path, ADR §7.1): the row is
    ``session.add`` + ``flush``-ed so the unique-constraint collision surfaces
    here, but NOT committed (the caller commits the business write + outbox
    atomically). Without ``session`` the helper opens/commits its own.
    """
    row = ServiceAccountRow(
        id=_new_id(),
        org_id=org_id,
        name=name,
        description=description,
        status=SERVICE_ACCOUNT_ACTIVE,
        created_by=created_by,
        owner_user_id=owner_user_id,
        purpose=purpose,
        system=system,
        environment=environment,
        expires_at=expires_at,
    )
    if session is not None:
        session.add(row)
        await session.flush()
        return row
    async with sf() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def get_service_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    service_account_id: str,
) -> ServiceAccountRow | None:
    """Return the row or ``None``. Caller MUST scope the result by ``org_id``."""
    async with sf() as session:
        return await session.get(ServiceAccountRow, service_account_id)


async def list_service_accounts(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
) -> list[ServiceAccountRow]:
    """All SAs in ``org_id``, ordered by ``created_at`` for stable display.

    ADR §8 "列表与查询强制 Org 过滤" — ``org_id`` is required, not
    optional. Use ``get_service_account`` + a manual filter if you need
    a cross-Org lookup (only the doctor / system-admin path does).
    """
    async with sf() as session:
        rows = (await session.execute(select(ServiceAccountRow).where(ServiceAccountRow.org_id == org_id).order_by(ServiceAccountRow.created_at.asc()))).scalars().all()
    return list(rows)


async def update_service_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    service_account_id: str,
    session: AsyncSession | None = None,
    **fields: object,
) -> ServiceAccountRow:
    """PATCH the updatable fields on a ServiceAccount row.

    Only members of :data:`_UPDATABLE_FIELDS` are honoured — ``status`` is
    rejected here so a caller cannot accidentally bypass the lifecycle
    helper. Unknown keys raise ``ValueError`` to surface programming
    errors at the boundary rather than silently dropping the field.

    Raises ``ValueError`` if the row does not exist (the app layer maps
    that to 404; a soft miss here is never silent).

    Pass an open ``session`` (keyword-only) to stage the mutation in a
    caller-owned transaction (Class A same-transaction path, ADR §7.1);
    without it the helper opens/commits its own session.
    """
    bad = set(fields) - _UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"update_service_account rejects non-updatable fields: {sorted(bad)}")

    if session is not None:
        row = await session.get(ServiceAccountRow, service_account_id)
        if row is None:
            raise ValueError(f"ServiceAccount {service_account_id!r} not found")
        for key, value in fields.items():
            setattr(row, key, value)
        await session.flush()
        return row
    async with sf() as session:
        row = await session.get(ServiceAccountRow, service_account_id)
        if row is None:
            raise ValueError(f"ServiceAccount {service_account_id!r} not found")
        for key, value in fields.items():
            setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
    return row


async def set_service_account_status(
    sf: async_sessionmaker[AsyncSession],
    *,
    service_account_id: str,
    status: str,
    session: AsyncSession | None = None,
) -> ServiceAccountRow:
    """Transition a SA's status. Only ``active`` / ``disabled`` are allowed.

    ADR §9.1: ``active ↔ disabled`` is the only in-row transition. The
    ``active | disabled → deleted`` transition is a hard DELETE via
    :func:`delete_service_account` (no tombstone state).

    Raises ``ValueError`` on unknown status (defensive — the CHECK
    constraint would also reject it at commit, but failing early gives a
    clearer error than a SQLAlchemy ``IntegrityError``).

    Pass an open ``session`` to stage the transition in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"Unknown ServiceAccount status {status!r}; allowed: {sorted(_ALLOWED_STATUSES)}")
    if session is not None:
        row = await session.get(ServiceAccountRow, service_account_id)
        if row is None:
            raise ValueError(f"ServiceAccount {service_account_id!r} not found")
        row.status = status
        await session.flush()
        return row
    async with sf() as session:
        row = await session.get(ServiceAccountRow, service_account_id)
        if row is None:
            raise ValueError(f"ServiceAccount {service_account_id!r} not found")
        row.status = status
        await session.commit()
        await session.refresh(row)
    return row


async def delete_service_account(
    sf: async_sessionmaker[AsyncSession],
    *,
    service_account_id: str,
    session: AsyncSession | None = None,
) -> ServiceAccountRow | None:
    """Hard-delete a ServiceAccount and its role bindings (atomic).

    ADR §12 requires SA deletion and Key revocation to land in the same
    controlled transaction. ``api_keys.service_account_id`` carries
    ``ondelete=CASCADE`` (0004_iam_tables), so the SA delete implicitly
    removes dependent keys; ``role_bindings.principal_id`` has no FK
    (polymorphic, §5.2) so the binding rows must be DELETEd explicitly
    in the same transaction.

    No-op (not an error) if the SA does not exist — a re-entrant delete
    after a partial failure must not raise. Returns the pre-delete row
    (so the caller can build an audit event from the SA's identity) or
    ``None`` when the row was absent; the row is detached/expired once
    the caller commits, so read audit-relevant fields before committing.

    Pass an open ``session`` to stage the delete inside a caller-owned
    transaction (Class A same-transaction path, ADR §7.1). The caller
    reads ``row`` attributes, enqueues the outbox event, then commits.
    """

    async def _delete(session: AsyncSession) -> ServiceAccountRow | None:
        row = await session.get(ServiceAccountRow, service_account_id)
        if row is None:
            return None
        # Same-transaction cleanup of the polymorphic bindings. ApiKey
        # rows cascade via FK; no explicit DELETE needed.
        await session.execute(
            delete(RoleBindingRow).where(
                RoleBindingRow.principal_type == "service_account",
                RoleBindingRow.principal_id == service_account_id,
            )
        )
        await session.delete(row)
        await session.flush()
        return row

    if session is not None:
        return await _delete(session)
    async with sf() as session:
        result = await _delete(session)
        await session.commit()
        return result


# ---------------------------------------------------------------------------
# RoleBinding helpers (polymorphic — used by both user and service_account)
# ---------------------------------------------------------------------------


async def create_role_binding(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    principal_type: str,
    principal_id: str,
    role_id: str,
    created_by: str | None = None,
    expires_at: datetime | None = None,
    session: AsyncSession | None = None,
) -> RoleBindingRow:
    """Insert one ``RoleBindingRow``. The CHECK constraint on
    ``principal_type`` accepts only ``'user'`` / ``'service_account'``.

    The ``(org_id, principal_type, principal_id, role_id)`` unique
    constraint raises ``IntegrityError`` on collision; the app layer
    maps that to 409.

    Pass an open ``session`` to stage the insert in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    row = RoleBindingRow(
        id=_new_id(),
        org_id=org_id,
        principal_type=principal_type,
        principal_id=principal_id,
        role_id=role_id,
        created_by=created_by,
        expires_at=expires_at,
    )
    if session is not None:
        session.add(row)
        await session.flush()
        return row
    async with sf() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def list_role_bindings(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    principal_type: str,
    principal_id: str,
) -> list[RoleBindingRow]:
    """All bindings for ``(org_id, principal_type, principal_id)``.

    ADR §8 "列表与查询强制 Org 过滤" — ``org_id`` is required.
    """
    async with sf() as session:
        rows = (
            (
                await session.execute(
                    select(RoleBindingRow).where(
                        RoleBindingRow.org_id == org_id,
                        RoleBindingRow.principal_type == principal_type,
                        RoleBindingRow.principal_id == principal_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def delete_role_binding(
    sf: async_sessionmaker[AsyncSession],
    *,
    binding_id: str,
    org_id: str,
    session: AsyncSession | None = None,
) -> RoleBindingRow | None:
    """Delete one binding, scoped by ``org_id``.

    Returns the pre-delete row (so the caller can build an audit event from
    the binding's identity: principal_type/principal_id/role_id), or ``None``
    if the binding does not exist. The Org filter prevents a cross-Org caller
    from deleting another Org's binding by guessing the id — ADR §8.

    Pass an open ``session`` to stage the delete in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1). The caller reads
    audit-relevant fields from the returned row, enqueues the outbox event,
    then commits.
    """

    async def _delete(session: AsyncSession) -> RoleBindingRow | None:
        row = (
            await session.execute(
                select(RoleBindingRow).where(
                    RoleBindingRow.id == binding_id,
                    RoleBindingRow.org_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        await session.delete(row)
        await session.flush()
        return row

    if session is not None:
        return await _delete(session)
    async with sf() as session:
        result = await _delete(session)
        await session.commit()
        return result


async def count_user_bindings_for_role(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    role_id: str,
    exclude_principal_id: str | None = None,
) -> int:
    """Count non-expired ``user``-principal bindings for ``role_id`` in ``org_id``.

    Last-admin guard read (ADR-0003 §7). ``exclude_principal_id`` removes
    the principal under consideration from the count so the caller can
    ask "after removing this user, how many admins remain?" — when that
    would hit zero, the removal must be refused by the policy layer.

    ``service_account`` principals are excluded by the ``principal_type``
    filter: a machine identity holding ``org:admin`` is not a human admin
    and does not satisfy "at least one human admin remains".

    Reads only (no commit). An expired binding (``expires_at <= now``)
    does not count — a role whose only holder is expired is effectively
    empty for the purposes of last-admin protection.
    """
    now = datetime.now(UTC)
    async with sf() as session:
        stmt = (
            select(func.count())
            .select_from(RoleBindingRow)
            .where(
                RoleBindingRow.org_id == org_id,
                RoleBindingRow.role_id == role_id,
                RoleBindingRow.principal_type == "user",
                (RoleBindingRow.expires_at.is_(None)) | (RoleBindingRow.expires_at > now),
            )
        )
        if exclude_principal_id is not None:
            stmt = stmt.where(RoleBindingRow.principal_id != exclude_principal_id)
        return int((await session.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# OrgMembership lifecycle (PR-037) — ADR-0003 §7 + §11
# ---------------------------------------------------------------------------
#
# Status mutation reuses the existing ``ck_org_memberships_status`` CHECK
# (invited/active/suspended/removed, data-model §4.5). No migration. The
# caller-facing endpoints exercise only ``active ↔ suspended`` (suspend
# re-enables authorization revocation; activate restores). ``invited`` /
# ``removed`` are reached by invite / remove flows not in this PR's scope.


async def set_membership_status(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    user_id: str,
    status: str,
    session: AsyncSession | None = None,
) -> OrgMembershipRow:
    """Transition a membership's ``status``. Only ``active`` / ``suspended``.

    ADR §7: a suspended membership is the revocation mechanism — the next
    ``authorize()`` after this commit sees ``status != "active"`` and denies
    (PR-031 ``_compute_user_permissions`` raises ``PERMISSION_DENIED``). The
    caller MUST invalidate the principal's authz cache post-commit so the
    revocation takes effect immediately rather than up to the ≤60s TTL
    (ADR §11 SLO).

    The row is looked up by the ``(org_id, user_id)`` UNIQUE constraint.
    Raises ``ValueError`` if no such membership exists (the app layer maps
    that to 404 — existence-hiding, matching the rest of the IAM API).

    Pass an open ``session`` to stage the transition in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    if status not in _ALLOWED_MEMBERSHIP_STATUSES:
        raise ValueError(f"Unknown membership status {status!r}; allowed: {sorted(_ALLOWED_MEMBERSHIP_STATUSES)}")

    async def _transition(session: AsyncSession) -> OrgMembershipRow:
        row = (
            await session.execute(
                select(OrgMembershipRow).where(
                    OrgMembershipRow.org_id == org_id,
                    OrgMembershipRow.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Membership for org={org_id!r} user={user_id!r} not found")
        if row.status == status:
            # Idempotent: re-suspending an already-suspended membership is a
            # no-op (matches the SA status helpers' posture).
            return row
        row.status = status
        await session.flush()
        return row

    if session is not None:
        return await _transition(session)
    async with sf() as session:
        row = await _transition(session)
        await session.commit()
        await session.refresh(row)
    return row


async def get_membership(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    user_id: str,
) -> OrgMembershipRow | None:
    """Return the ``(org_id, user_id)`` membership row, or ``None``.

    Thin lookup helper for the membership router (used to read the current
    status before a transition and to confirm existence for 404 mapping).
    Reads only (no commit).
    """
    async with sf() as session:
        return (
            await session.execute(
                select(OrgMembershipRow).where(
                    OrgMembershipRow.org_id == org_id,
                    OrgMembershipRow.user_id == user_id,
                )
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# API Key CRUD (PR-035)
# ---------------------------------------------------------------------------
#
# ``api_keys.service_account_id`` IS a real FK with ``ondelete=CASCADE``
# (0004_iam_tables), so SA deletion implicitly removes dependent keys.
# ``key_prefix`` is the lookup key (unique index ``uq_api_keys_key_prefix``)
# and ``key_hash`` is the HMAC-SHA256(pepper, plaintext) hex digest — the
# plaintext is NEVER persisted (ADR §9.2).


async def create_api_key(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    service_account_id: str,
    key_prefix: str,
    key_hash: str,
    scopes: list[str],
    expires_at: datetime,
    created_by: str | None = None,
    session: AsyncSession | None = None,
) -> ApiKeyRow:
    """Insert one ``ApiKeyRow``. ``revoked_at`` starts ``None``.

    The ``key_hash`` MUST already be HMAC'd by the caller
    (``app.gateway.auth.api_key.hash_api_key``); this layer never sees
    the plaintext. The unique ``key_prefix`` constraint raises
    ``IntegrityError`` on collision; the app layer retries once with a
    fresh prefix, then surfaces 409 if it still collides.

    Pass an open ``session`` to stage the insert in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    row = ApiKeyRow(
        id=_new_id(),
        org_id=org_id,
        service_account_id=service_account_id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=list(scopes),
        expires_at=expires_at,
    )
    # ``created_by`` is not on ApiKeyRow today (no column); the actor is
    # carried in the audit payload by the router. Kept as a parameter so
    # a future migration adding the column does not break callers.
    del created_by
    if session is not None:
        session.add(row)
        await session.flush()
        return row
    async with sf() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def get_api_key(
    sf: async_sessionmaker[AsyncSession],
    *,
    api_key_id: str,
) -> ApiKeyRow | None:
    """Return the row by primary key, or ``None``. Caller MUST scope by org_id."""
    async with sf() as session:
        return await session.get(ApiKeyRow, api_key_id)


async def get_api_key_by_prefix(
    sf: async_sessionmaker[AsyncSession],
    *,
    key_prefix: str,
) -> ApiKeyRow | None:
    """Look up by ``key_prefix``. Auth-middleware path.

    The unique index ``uq_api_keys_key_prefix`` guarantees at most one
    match. Cross-Org scope is NOT applied here — the middleware reads
    the row's ``org_id`` / ``service_account_id`` to drive subsequent
    checks (SA row, SA status, etc.). Callers that need to scope should
    use :func:`get_api_key` + a manual ``org_id`` comparison.
    """
    async with sf() as session:
        return (await session.execute(select(ApiKeyRow).where(ApiKeyRow.key_prefix == key_prefix))).scalar_one_or_none()


async def list_api_keys(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    service_account_id: str,
) -> list[ApiKeyRow]:
    """All keys for one SA in one Org. ADR §8 Org filter is required.

    Ordered by ``created_at`` descending so the newest keys (most
    relevant for an operator looking for "what's active") come first.
    """
    async with sf() as session:
        rows = (
            (
                await session.execute(
                    select(ApiKeyRow)
                    .where(
                        ApiKeyRow.org_id == org_id,
                        ApiKeyRow.service_account_id == service_account_id,
                    )
                    .order_by(ApiKeyRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def revoke_api_key(
    sf: async_sessionmaker[AsyncSession],
    *,
    api_key_id: str,
    org_id: str,
    session: AsyncSession | None = None,
) -> ApiKeyRow | None:
    """Set ``revoked_at = now`` on one key. Idempotent.

    Returns the updated row, or ``None`` if the key does not exist (or
    belongs to a different Org — existence-hiding via ``org_id`` filter,
    ADR §8). The app layer treats ``None`` as 204 anyway because revocation
    is idempotent: a repeated revoke after a partial failure must not
    surface as an error to the client.

    Setting ``revoked_at`` multiple times is harmless — the column is
    monotonic (we do not reset it). ADR §9.2 line 299 "revoked / expired
    Key 不可恢复" forbids un-revoking, which the absence of an un-revoke
    endpoint enforces structurally.

    Pass an open ``session`` to stage the revoke in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    now = datetime.now(UTC)

    async def _revoke(session: AsyncSession) -> ApiKeyRow | None:
        row = (
            await session.execute(
                select(ApiKeyRow).where(
                    ApiKeyRow.id == api_key_id,
                    ApiKeyRow.org_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.revoked_at is None:
            row.revoked_at = now
            await session.flush()
        return row

    if session is not None:
        return await _revoke(session)
    async with sf() as session:
        row = await _revoke(session)
        if row is not None:
            await session.commit()
            await session.refresh(row)
        else:
            await session.commit()
        return row


async def touch_api_key_last_used(
    sf: async_sessionmaker[AsyncSession],
    *,
    api_key_id: str,
) -> None:
    """Sampling UPDATE on ``last_used_at`` (PR-035 ADR §9.2 observability).

    At most one write per :data:`_LAST_USED_SAMPLE_WINDOW` per key, so
    a high-QPS request stream does not turn into per-request DB writes.
    The WHERE clause is the sampling gate: a row whose ``last_used_at``
    is already within the window is not touched. The condition
    ``last_used_at IS NULL OR last_used_at < :cutoff`` is evaluated by
    the DB, avoiding a read-modify-write race across concurrent
    requests (whichever request wins the race updates; the loser's
    UPDATE matches zero rows and is a cheap no-op).

    Fire-and-forget from the auth middleware: this function swallows
    nothing — the caller wraps it in ``try/except`` + ``asyncio.shield``
    so an observability-column write failure never fails the request.
    """
    cutoff = datetime.now(UTC) - _LAST_USED_SAMPLE_WINDOW
    async with sf() as session:
        await session.execute(
            update(ApiKeyRow)
            .where(
                ApiKeyRow.id == api_key_id,
                (ApiKeyRow.last_used_at.is_(None)) | (ApiKeyRow.last_used_at < cutoff),
            )
            .values(last_used_at=datetime.now(UTC))
        )
        await session.commit()


# ---------------------------------------------------------------------------
# OIDC group-mapping CRUD (PR-036) — ADR-0003 §10
# ---------------------------------------------------------------------------
#
# The row set IS the allowlist (§10 rule 1). Pure data access: the
# service layer (``deerflow.tenancy.oidc_group_mapping``) enforces the
# "target role must not carry system permissions" guard (rule 3) before
# insert — this layer only persists what it is told. The
# ``uq_oidc_group_mappings_issuer_group_org_role`` unique constraint
# raises ``IntegrityError`` on a duplicate allowlist entry.


async def create_oidc_group_mapping(
    sf: async_sessionmaker[AsyncSession],
    *,
    issuer: str,
    group_claim: str,
    group_value: str,
    target_org_id: str,
    target_role_id: str,
    mode: str = MAPPING_MODE_ADDITIVE,
    description: str | None = None,
    created_by: str | None = None,
    session: AsyncSession | None = None,
) -> OidcGroupMappingRow:
    """Insert one ``OidcGroupMappingRow`` (one allowlist entry).

    The ``(issuer, group_value, target_org_id, target_role_id)`` unique
    constraint raises ``IntegrityError`` on collision; the app layer maps
    that to 409. ``mode`` defaults to ``additive`` (the MVP default per
    ADR §10). The service layer MUST validate that ``target_role_id``
    points at a real, non-system role before calling (rule 3).

    Pass an open ``session`` to stage the insert in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    row = OidcGroupMappingRow(
        id=_new_id(),
        issuer=issuer,
        group_claim=group_claim,
        group_value=group_value,
        target_org_id=target_org_id,
        target_role_id=target_role_id,
        mode=mode,
        description=description,
        created_by=created_by,
    )
    if session is not None:
        session.add(row)
        await session.flush()
        return row
    async with sf() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def get_oidc_group_mapping(
    sf: async_sessionmaker[AsyncSession],
    *,
    mapping_id: str,
) -> OidcGroupMappingRow | None:
    """Return the row by primary key, or ``None``. Caller MUST scope by ``org_id``."""
    async with sf() as session:
        return await session.get(OidcGroupMappingRow, mapping_id)


async def list_oidc_group_mappings(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str | None = None,
    issuer: str | None = None,
) -> list[OidcGroupMappingRow]:
    """List mapping rows, optionally scoped by ``org_id`` and/or ``issuer``.

    ADR §8 "列表与查询强制 Org 过滤" — pass ``org_id`` for the admin
    listing (a cross-Org system-admin view is the only caller that omits
    it today). Ordered by ``created_at`` for stable display.

    The mapping engine calls this with ``issuer`` only (it needs every
    target-org row for one issuer to evaluate the allowlist), so
    ``org_id`` is optional here unlike the other list helpers — the
    engine applies its own org scoping against the user's membership.
    """
    async with sf() as session:
        stmt = select(OidcGroupMappingRow)
        if org_id is not None:
            stmt = stmt.where(OidcGroupMappingRow.target_org_id == org_id)
        if issuer is not None:
            stmt = stmt.where(OidcGroupMappingRow.issuer == issuer)
        stmt = stmt.order_by(OidcGroupMappingRow.created_at.asc())
        rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def update_oidc_group_mapping(
    sf: async_sessionmaker[AsyncSession],
    *,
    mapping_id: str,
    session: AsyncSession | None = None,
    **fields: object,
) -> OidcGroupMappingRow:
    """PATCH the updatable fields on an OIDC group-mapping row.

    Only members of :data:`_MAPPING_UPDATABLE_FIELDS` are honoured —
    ``target_org_id`` and ``issuer`` are deliberately NOT patchable: a
    rule's identity (which issuer, which org) is immutable; to retarget
    delete + recreate so the audit trail shows the change cleanly.
    Unknown keys raise ``ValueError``.

    Raises ``ValueError`` if the row does not exist (the app layer maps
    that to 404 for existence-hiding).

    Pass an open ``session`` to stage the mutation in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """
    bad = set(fields) - _MAPPING_UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"update_oidc_group_mapping rejects non-updatable fields: {sorted(bad)}")

    if session is not None:
        row = await session.get(OidcGroupMappingRow, mapping_id)
        if row is None:
            raise ValueError(f"OidcGroupMapping {mapping_id!r} not found")
        for key, value in fields.items():
            setattr(row, key, value)
        await session.flush()
        return row
    async with sf() as session:
        row = await session.get(OidcGroupMappingRow, mapping_id)
        if row is None:
            raise ValueError(f"OidcGroupMapping {mapping_id!r} not found")
        for key, value in fields.items():
            setattr(row, key, value)
        await session.commit()
        await session.refresh(row)
    return row


async def delete_oidc_group_mapping(
    sf: async_sessionmaker[AsyncSession],
    *,
    mapping_id: str,
    org_id: str,
    session: AsyncSession | None = None,
) -> OidcGroupMappingRow | None:
    """Delete one mapping row, scoped by ``target_org_id``.

    Returns the pre-delete row (so the caller can build an audit event from
    the mapping's identity), or ``None`` if the mapping does not exist or
    belongs to another Org. The Org filter is on ``target_org_id`` — that
    is the scoping column for this table, mirroring ``org_id`` on the other
    IAM tables.

    Pass an open ``session`` to stage the delete in a caller-owned
    transaction (Class A same-transaction path, ADR §7.1).
    """

    async def _delete(session: AsyncSession) -> OidcGroupMappingRow | None:
        row = (
            await session.execute(
                select(OidcGroupMappingRow).where(
                    OidcGroupMappingRow.id == mapping_id,
                    OidcGroupMappingRow.target_org_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        await session.delete(row)
        await session.flush()
        return row

    if session is not None:
        return await _delete(session)
    async with sf() as session:
        result = await _delete(session)
        await session.commit()
        return result


__all__ = [
    "MAPPING_MODE_ADDITIVE",
    "MAPPING_MODE_AUTHORITATIVE",
    "MEMBERSHIP_ACTIVE",
    "MEMBERSHIP_SUSPENDED",
    "SERVICE_ACCOUNT_ACTIVE",
    "SERVICE_ACCOUNT_DISABLED",
    "count_user_bindings_for_role",
    "create_api_key",
    "create_oidc_group_mapping",
    "create_role_binding",
    "create_service_account",
    "delete_oidc_group_mapping",
    "delete_role_binding",
    "delete_service_account",
    "get_api_key",
    "get_api_key_by_prefix",
    "get_membership",
    "get_oidc_group_mapping",
    "get_service_account",
    "list_api_keys",
    "list_oidc_group_mappings",
    "list_role_bindings",
    "list_service_accounts",
    "revoke_api_key",
    "set_membership_status",
    "set_service_account_status",
    "touch_api_key_last_used",
    "update_oidc_group_mapping",
    "update_service_account",
]
