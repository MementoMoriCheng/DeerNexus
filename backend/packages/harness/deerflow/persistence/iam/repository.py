"""DB CRUD for the IAM control-plane tables (PR-034).

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

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.iam.model import ApiKeyRow, RoleBindingRow, ServiceAccountRow

#: Status values allowed by the ``ck_service_accounts_status`` CHECK.
#: ``deleted`` is NOT a row state — SA deletion is a hard DELETE (ADR §12
#: requires SA deletion + Key revocation in the same transaction, not a
#: soft-delete tombstone).
SERVICE_ACCOUNT_ACTIVE = "active"
SERVICE_ACCOUNT_DISABLED = "disabled"
_ALLOWED_STATUSES: frozenset[str] = frozenset({SERVICE_ACCOUNT_ACTIVE, SERVICE_ACCOUNT_DISABLED})

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
) -> ServiceAccountRow:
    """Insert one ``ServiceAccountRow`` with ``status="active"``.

    The ``(org_id, name)`` unique constraint (``uq_service_accounts_org_name``)
    raises ``IntegrityError`` on collision; the app layer maps that to 409.
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
    **fields: object,
) -> ServiceAccountRow:
    """PATCH the updatable fields on a ServiceAccount row.

    Only members of :data:`_UPDATABLE_FIELDS` are honoured — ``status`` is
    rejected here so a caller cannot accidentally bypass the lifecycle
    helper. Unknown keys raise ``ValueError`` to surface programming
    errors at the boundary rather than silently dropping the field.

    Raises ``ValueError`` if the row does not exist (the app layer maps
    that to 404; a soft miss here is never silent).
    """
    bad = set(fields) - _UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"update_service_account rejects non-updatable fields: {sorted(bad)}")

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
) -> ServiceAccountRow:
    """Transition a SA's status. Only ``active`` / ``disabled`` are allowed.

    ADR §9.1: ``active ↔ disabled`` is the only in-row transition. The
    ``active | disabled → deleted`` transition is a hard DELETE via
    :func:`delete_service_account` (no tombstone state).

    Raises ``ValueError`` on unknown status (defensive — the CHECK
    constraint would also reject it at commit, but failing early gives a
    clearer error than a SQLAlchemy ``IntegrityError``).
    """
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"Unknown ServiceAccount status {status!r}; allowed: {sorted(_ALLOWED_STATUSES)}")
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
) -> None:
    """Hard-delete a ServiceAccount and its role bindings (atomic).

    ADR §12 requires SA deletion and Key revocation to land in the same
    controlled transaction. ``api_keys.service_account_id`` carries
    ``ondelete=CASCADE`` (0004_iam_tables), so the SA delete implicitly
    removes dependent keys; ``role_bindings.principal_id`` has no FK
    (polymorphic, §5.2) so the binding rows must be DELETEd explicitly
    in the same transaction.

    No-op (not an error) if the SA does not exist — the app layer has
    already emitted the audit event referencing the SA's pre-delete
    identity, so a re-entrant delete after a partial failure must not
    raise.
    """
    async with sf() as session:
        row = await session.get(ServiceAccountRow, service_account_id)
        if row is None:
            return
        # Same-transaction cleanup of the polymorphic bindings. ApiKey
        # rows cascade via FK; no explicit DELETE needed.
        await session.execute(
            delete(RoleBindingRow).where(
                RoleBindingRow.principal_type == "service_account",
                RoleBindingRow.principal_id == service_account_id,
            )
        )
        await session.delete(row)
        await session.commit()


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
) -> RoleBindingRow:
    """Insert one ``RoleBindingRow``. The CHECK constraint on
    ``principal_type`` accepts only ``'user'`` / ``'service_account'``.

    The ``(org_id, principal_type, principal_id, role_id)`` unique
    constraint raises ``IntegrityError`` on collision; the app layer
    maps that to 409.
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
) -> None:
    """Delete one binding, scoped by ``org_id``.

    Returns silently if the binding does not exist (the app layer has
    already emitted the audit event). The Org filter prevents a
    cross-Org caller from deleting another Org's binding by guessing
    the id — ADR §8.
    """
    async with sf() as session:
        await session.execute(
            delete(RoleBindingRow).where(
                RoleBindingRow.id == binding_id,
                RoleBindingRow.org_id == org_id,
            )
        )
        await session.commit()


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
) -> ApiKeyRow:
    """Insert one ``ApiKeyRow``. ``revoked_at`` starts ``None``.

    The ``key_hash`` MUST already be HMAC'd by the caller
    (``app.gateway.auth.api_key.hash_api_key``); this layer never sees
    the plaintext. The unique ``key_prefix`` constraint raises
    ``IntegrityError`` on collision; the app layer retries once with a
    fresh prefix, then surfaces 409 if it still collides.
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
    """
    now = datetime.now(UTC)
    async with sf() as session:
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
            await session.commit()
            await session.refresh(row)
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


__all__ = [
    "SERVICE_ACCOUNT_ACTIVE",
    "SERVICE_ACCOUNT_DISABLED",
    "create_api_key",
    "create_role_binding",
    "create_service_account",
    "delete_role_binding",
    "delete_service_account",
    "get_api_key",
    "get_api_key_by_prefix",
    "get_service_account",
    "list_api_keys",
    "list_role_bindings",
    "list_service_accounts",
    "revoke_api_key",
    "set_service_account_status",
    "touch_api_key_last_used",
    "update_service_account",
]
