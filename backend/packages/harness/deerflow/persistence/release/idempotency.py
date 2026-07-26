"""Idempotency-Key replay store for promote/rollback (ADR-0004 §7, PR-055).

This is the "store the full response" replay shape: when a client retries a
promote/rollback with the **same** ``Idempotency-Key``, we return the original
result verbatim — no second ``_move_channel`` (so no second
``release_channels`` CAS and no second ``release_events`` row) and no second
``release.agent.published`` / ``release.agent.rolled_back`` audit outbox row.
A same key with a **different** request surfaces ``IdempotencyConflictError``
(router → 409 ``idempotency_conflict``).

Concurrency model
-----------------

The router does, **inside the promote/rollback session**:

1. ``get_idempotency_record(org_id, key)`` — a read on the caller's session.
2. If a record exists:
   * same ``request_hash`` → replay the stored ``response_payload`` (skip
     ``_move_channel`` and audit entirely);
   * different ``request_hash`` → raise ``IdempotencyConflictError``.
3. If no record: run ``_move_channel`` + Class A audit, then
   ``insert_idempotency_record`` (same session) and commit.

The ``UNIQUE(org_id, idempotency_key)`` constraint is the concurrency fence.
Two concurrent same-key requests cannot both reach step 3's insert: the loser
hits ``IntegrityError`` at the commit/flush boundary. The router catches that
``IntegrityError`` **within the same session**, rolls the session transaction
state back to a savepoint, re-reads, and either replays (the winner already
stored an identical request) or conflicts (the winner stored a different
request). See ``_handle_idempotency_race`` in the router.

Request fingerprint
-------------------

``compute_request_hash`` hashes the **semantically-meaningful** request
identity (action, package_id, channel, target_version_id, workspace_id,
reason). ``expected_channel_version`` is deliberately EXCLUDED — a client
retrying after a CAS miss will send a *new* expected version, but that is the
same logical request and must replay the original result, not conflict.
``actor_id`` is excluded so a token refresh (different session, same user)
does not spuriously conflict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.release.model import ReleaseIdempotencyRecordRow


class IdempotencyConflictError(Exception):
    """Same Idempotency-Key, semantically different request (ADR-0004 §7).

    Router maps this to 409 ``idempotency_conflict``. Distinct from
    ``ReleaseConflictError`` (a CAS miss on ``row_version``) — an idempotency
    conflict is a client programming error, not a retryable race, and is
    therefore NOT in the retryable set (``errors._RETRYABLE_CODES``).
    """

    def __init__(self, *, org_id: str, idempotency_key: str) -> None:
        self.org_id = org_id
        self.idempotency_key = idempotency_key
        super().__init__(
            f"idempotency conflict: key {idempotency_key!r} already used by a different request in org {org_id!r}",
        )


def compute_request_hash(
    *,
    action: str,
    package_id: str,
    channel: str,
    target_version_id: str,
    workspace_id: str | None,
    reason: str | None,
) -> str:
    """Stable sha256 hex of the semantically-meaningful request identity.

    See module docstring: ``expected_channel_version`` and ``actor_id`` are
    deliberately excluded so a legitimate retry (after a CAS miss, or after a
    token refresh) replays the original result instead of conflicting. The
    hash is canonicalized (sorted keys, compact separators) so field order in
    the caller's kwargs does not perturb equality.
    """
    payload: dict[str, Any] = {
        "action": action,
        "package_id": package_id,
        "channel": channel,
        "target_version_id": target_version_id,
        "workspace_id": workspace_id,
        "reason": reason,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def get_idempotency_record(
    session: AsyncSession,
    *,
    org_id: str,
    idempotency_key: str,
) -> ReleaseIdempotencyRecordRow | None:
    """Return the replay record for ``(org_id, idempotency_key)``, or ``None``.

    Runs on the **caller's session** so the read participates in the same
    transaction as the promote/rollback — a concurrent same-key writer that
    has not yet committed is invisible to this read (READ COMMITTED on
    Postgres; SQLite is serial by nature), which is exactly why the
    ``UNIQUE`` constraint + ``insert_idempotency_record`` race handling is the
    authoritative fence, not this read.
    """
    if not org_id:
        raise ValueError("org_id is required for idempotency reads")
    if not idempotency_key:
        raise ValueError("idempotency_key is required for idempotency reads")
    stmt = select(ReleaseIdempotencyRecordRow).where(
        ReleaseIdempotencyRecordRow.org_id == org_id,
        ReleaseIdempotencyRecordRow.idempotency_key == idempotency_key,
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def insert_idempotency_record(
    session: AsyncSession,
    *,
    org_id: str,
    idempotency_key: str,
    request_hash: str,
    response_payload: dict[str, Any],
    status_code: int,
    record_id: str,
) -> ReleaseIdempotencyRecordRow:
    """Insert a replay record on the **caller's session** (no commit).

    The caller has already run ``_move_channel`` + Class A audit in this same
    session; this insert must succeed in the same transaction so a crash
    between audit enqueue and replay-store insert cannot leave a
    half-committed promote (the whole session commits atomically or not at
    all — the §7.1 fail-rollback contract).

    Raises ``IntegrityError`` on a same-``(org_id, idempotency_key)`` race;
    the router's ``_handle_idempotency_race`` catches that, re-reads, and
    either replays or raises :class:`IdempotencyConflictError`.
    """
    row = ReleaseIdempotencyRecordRow(
        id=record_id,
        org_id=org_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_payload=response_payload,
        status_code=status_code,
    )
    session.add(row)
    await session.flush()  # surface UNIQUE collision inside this transaction
    return row


async def resolve_idempotency_outcome(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[str, ReleaseIdempotencyRecordRow | None]:
    """After a UNIQUE race, re-read and classify the winner's outcome.

    Opens a **fresh** session (the racing session is poisoned by the failed
    insert). Returns:

    * ``("replay", record)`` — same key, same request → router replays
      ``record.response_payload``;
    * ``("conflict", None)`` — same key, different request → router raises
      :class:`IdempotencyConflictError`.

    Used by ``_handle_idempotency_race`` in the router; not intended for the
    happy path (the happy-path read happens on the caller's session before
    ``_move_channel`` runs).
    """
    async with sf() as session:
        record = await get_idempotency_record(session, org_id=org_id, idempotency_key=idempotency_key)
    if record is None:
        # The winner rolled back (their commit failed after our insert
        # collision) — extremely rare. Treat as a clean miss so the caller can
        # retry the whole request; we do NOT re-attempt _move_channel here.
        return ("miss", None)
    if record.request_hash == request_hash:
        return ("replay", record)
    return ("conflict", None)


#: HTTP header name for the client-supplied replay key.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

#: Maximum length we accept for the Idempotency-Key (matches the column width).
IDEMPOTENCY_KEY_MAX_LENGTH = 128

__all__ = [
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "IdempotencyConflictError",
    "compute_request_hash",
    "get_idempotency_record",
    "insert_idempotency_record",
    "resolve_idempotency_outcome",
]
