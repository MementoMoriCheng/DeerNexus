"""DB CRUD for the agent-artifact control-plane tables (PR-052).

Pure data-access layer — no audit, no cache, no authz. The app layer
(``app/gateway/routers/agent_artifacts.py``) is responsible for emitting
audit events after writes; this module owns only the DB mutation, the
content digest, the inline/object storage routing, and the Org-scoped read
filter.

Conventions (mirror ``persistence/iam/repository.py``):

* Each function opens its own ``AsyncSession`` from the supplied
  ``async_sessionmaker``; multi-step writes commit before returning.
* All writes accept an optional ``session: AsyncSession | None = None``
  for the Class A same-transaction path (ADR-0005 §7.1) — pass an open
  session to stage the write + audit-outbox row in one atomic commit.
* Reads always filter by ``org_id`` (ADR §8); a missing ``org_id`` in a
  get/list is a programming error (``ValueError``).
* Cross-Org existence-hiding: a get/update/delete that misses (wrong Org or
  absent) returns ``None`` / raises ``ValueError`` so the router emits an
  identical 404 (never reveals existence across Orgs).

Published-immutability (ADR §3.2 / §4.3): once a Version's ``status``
enters ``published``, its content / manifest / digest / version are frozen.
This is enforced here at the write path: ``update_agent_version`` refuses
to mutate those fields on a published row, and the ``content`` of a
published version cannot be re-supplied. ``set_version_status`` may still
transition published → revoked (revocation is allowed; content mutation is
not). Draft / reviewed rows remain editable (ADR §4.1 / §4.2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.release.digest import compute_artifact_digest
from deerflow.persistence.release.model import AgentPackageRow, AgentVersionRow
from deerflow.persistence.release.storage import (
    InlineObjectStore,
    ObjectStore,
    compute_object_key,
)

# ---------------------------------------------------------------------------
# Status constants (mirror the DB CHECK values)
# ---------------------------------------------------------------------------

PACKAGE_ACTIVE = "active"
PACKAGE_ARCHIVED = "archived"
_ALLOWED_PACKAGE_STATUSES: frozenset[str] = frozenset({PACKAGE_ACTIVE, PACKAGE_ARCHIVED})

VERSION_DRAFT = "draft"
VERSION_REVIEWED = "reviewed"
VERSION_PUBLISHED = "published"
VERSION_REVOKED = "revoked"
VERSION_ARCHIVED = "archived"
_ALLOWED_VERSION_STATUSES: frozenset[str] = frozenset({VERSION_DRAFT, VERSION_REVIEWED, VERSION_PUBLISHED, VERSION_REVOKED, VERSION_ARCHIVED})

#: Fields the app layer may PATCH on a draft/reviewed AgentVersion.
#: ``status`` is excluded — transitions go through ``set_version_status``.
#: These fields are FROZEN once the version is published (see
#: ``_assert_mutable``); the set documents what a draft edit may touch.
_UPDATABLE_VERSION_FIELDS: frozenset[str] = frozenset({"version", "manifest", "content", "digest"})

#: Default inline threshold when the caller does not supply one. Mirrors the
#: ``ProductionArtifactConfig.inline_size_threshold`` default; the router
#: passes the configured value, tests may override.
_DEFAULT_INLINE_THRESHOLD = 65536


class VersionImmutableError(Exception):
    """Raised when a mutation targets the frozen fields of a published Version (ADR §3.2)."""


class IllegalVersionTransitionError(Exception):
    """Raised when a status transition is not permitted by the ADR §4 state machine."""


def _new_id() -> str:
    """Generate a 36-char hex id matching the ``String(36)`` convention."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# AgentPackage CRUD
# ---------------------------------------------------------------------------


async def create_agent_package(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    name: str,
    display_name: str,
    description: str | None = None,
    workspace_id: str | None = None,
    created_by: str | None = None,
    session: AsyncSession | None = None,
) -> AgentPackageRow:
    """Insert one ``AgentPackageRow`` with ``status="active"``.

    The ``(org_id, name)`` unique constraint (``uq_agent_packages_org_name``)
    raises ``IntegrityError`` on collision; the app layer maps that to 409.
    """
    row = AgentPackageRow(
        id=_new_id(),
        org_id=org_id,
        workspace_id=workspace_id,
        name=name,
        display_name=display_name,
        description=description,
        status=PACKAGE_ACTIVE,
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


async def get_agent_package(
    sf: async_sessionmaker[AsyncSession],
    *,
    package_id: str,
    org_id: str,
) -> AgentPackageRow | None:
    """Return the package scoped to ``org_id``, or ``None`` if absent/wrong Org.

    The router treats ``None`` as 404 (existence-hiding). A missing
    ``org_id`` is a programming error (``ValueError``).
    """
    if not org_id:
        raise ValueError("org_id is required for AgentPackage reads")
    async with sf() as session:
        row = await session.get(AgentPackageRow, package_id)
        if row is None or row.org_id != org_id:
            return None
        return row


async def get_agent_package_by_name(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    name: str,
    session: AsyncSession | None = None,
) -> AgentPackageRow | None:
    """Return the package named ``name`` in ``org_id``, or ``None``.

    Used by the file-state importer (PR-051) to detect "package already
    exists, just add a version" without relying on the UNIQUE-constraint
    IntegrityError as control flow. Cross-Org existence-hiding: a same-named
    package in another Org returns ``None``. ``include_archived=True`` semantics
    are not exposed here — an archived package is reused as-is (archival is a
    display concern, not an identity one); a caller that needs to exclude
    archived packages can branch on the returned ``status``.
    """
    if not org_id:
        raise ValueError("org_id is required for AgentPackage reads")

    async def _do(session: AsyncSession) -> AgentPackageRow | None:
        stmt = select(AgentPackageRow).where(
            AgentPackageRow.org_id == org_id,
            AgentPackageRow.name == name,
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    if session is not None:
        return await _do(session)
    async with sf() as session:
        return await _do(session)


async def list_agent_packages(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    include_archived: bool = False,
) -> list[AgentPackageRow]:
    """Return packages in ``org_id``, newest-first; archived excluded by default."""
    if not org_id:
        raise ValueError("org_id is required for AgentPackage reads")
    async with sf() as session:
        stmt = select(AgentPackageRow).where(AgentPackageRow.org_id == org_id)
        if not include_archived:
            stmt = stmt.where(AgentPackageRow.status == PACKAGE_ACTIVE)
        stmt = stmt.order_by(AgentPackageRow.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def update_agent_package(
    sf: async_sessionmaker[AsyncSession],
    *,
    package_id: str,
    org_id: str,
    display_name: str | None = None,
    description: str | None = None,
    session: AsyncSession | None = None,
) -> AgentPackageRow | None:
    """PATCH mutable display fields on a package. ``None`` if absent/wrong Org.

    Identity fields (``name`` / ``org_id`` / ``workspace_id``) are immutable
    post-create (ADR §3.1). ``status`` transitions go through
    :func:`archive_agent_package`.
    """
    fields: dict[str, object] = {}
    if display_name is not None:
        fields["display_name"] = display_name
    if description is not None:
        fields["description"] = description
    if not fields:
        # Nothing to update — return the current row so the router can echo it.
        return await get_agent_package(sf, package_id=package_id, org_id=org_id)

    async def _do(session: AsyncSession) -> AgentPackageRow | None:
        row = await session.get(AgentPackageRow, package_id)
        if row is None or row.org_id != org_id:
            return None
        for key, value in fields.items():
            setattr(row, key, value)
        await session.flush()
        return row

    if session is not None:
        return await _do(session)
    async with sf() as session:
        row = await _do(session)
        if row is None:
            return None
        await session.commit()
        await session.refresh(row)
    return row


async def archive_agent_package(
    sf: async_sessionmaker[AsyncSession],
    *,
    package_id: str,
    org_id: str,
    session: AsyncSession | None = None,
) -> AgentPackageRow | None:
    """Soft-archive a package (``status → archived``). ``None`` if absent/wrong Org.

    ADR §3.1 / §11.3: a package with existing Versions cannot be hard-deleted
    (the ``package_id`` FK is ``ON DELETE RESTRICT``). Archiving hides it from
    the default list without destroying Version history. Versions themselves
    are archived/revoked independently.
    """
    return await _set_package_status(sf, package_id=package_id, org_id=org_id, status=PACKAGE_ARCHIVED, session=session)


async def _set_package_status(
    sf: async_sessionmaker[AsyncSession],
    *,
    package_id: str,
    org_id: str,
    status: str,
    session: AsyncSession | None = None,
) -> AgentPackageRow | None:
    if status not in _ALLOWED_PACKAGE_STATUSES:
        raise ValueError(f"Unknown AgentPackage status {status!r}; allowed: {sorted(_ALLOWED_PACKAGE_STATUSES)}")

    async def _do(session: AsyncSession) -> AgentPackageRow | None:
        row = await session.get(AgentPackageRow, package_id)
        if row is None or row.org_id != org_id:
            return None
        row.status = status
        await session.flush()
        return row

    if session is not None:
        return await _do(session)
    async with sf() as session:
        row = await _do(session)
        if row is None:
            return None
        await session.commit()
        await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# AgentVersion CRUD — the immutable-content core
# ---------------------------------------------------------------------------


async def create_agent_version(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    package_id: str,
    version: str,
    manifest: dict,
    content: str,
    workspace_id: str | None = None,
    created_by: str | None = None,
    inline_size_threshold: int = _DEFAULT_INLINE_THRESHOLD,
    object_store: ObjectStore | None = None,
    session: AsyncSession | None = None,
) -> AgentVersionRow:
    """Create a ``draft`` AgentVersion, computing digest + routing storage.

    The caller supplies ``content`` (raw artifact payload). This computes the
    immutable ``digest`` (``sha256:<hex>`` over the content's UTF-8 bytes),
    routes storage by size: ≤ ``inline_size_threshold`` → ``content_inline``;
    larger → ``object_key`` via the ``object_store`` (default
    ``InlineObjectStore``, which stores nothing externally because the inline
    column holds the bytes for small artifacts; a real S3 backend will land
    in a follow-up). ADR §11.1 / §11.2.

    The ``(org_id, package_id, version)`` and ``(org_id, digest)`` unique
    constraints raise ``IntegrityError`` on collision; the app layer maps
    those to 409. Cross-Org: ``org_id`` is redundant on the row for forced
    isolation (data-model §6.3) — it MUST match the package's Org (the
    router verifies the package belongs to the caller's Org before calling).

    Pass an open ``session`` to stage the write + audit-outbox row in one
    atomic commit (Class A same-transaction path, ADR-0005 §7.1).
    """
    digest = compute_artifact_digest(content)
    size_bytes = len(content.encode("utf-8"))
    store = object_store if object_store is not None else InlineObjectStore()
    version_id = _new_id()

    content_inline: str | None
    object_key: str | None
    if size_bytes <= inline_size_threshold:
        content_inline = content
        object_key = None
    else:
        content_inline = None
        # The object key is scoped to the version (ADR §11.2), so the id is
        # generated before storage routing. Upload BEFORE the row commits
        # (ADR §11.2 "上传完成后再使 Version 可用"); a put failure aborts
        # the caller's transaction.
        object_key = compute_object_key(org_id=org_id, workspace_id=workspace_id, version_id=version_id)
        store.put(object_key=object_key, content=content.encode("utf-8"))

    row = AgentVersionRow(
        id=version_id,
        org_id=org_id,
        package_id=package_id,
        version=version,
        digest=digest,
        status=VERSION_DRAFT,
        manifest=manifest,
        content_inline=content_inline,
        object_key=object_key,
        size_bytes=size_bytes,
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


async def get_agent_version(
    sf: async_sessionmaker[AsyncSession],
    *,
    version_id: str,
    org_id: str,
) -> AgentVersionRow | None:
    """Return the version scoped to ``org_id``, or ``None`` if absent/wrong Org."""
    if not org_id:
        raise ValueError("org_id is required for AgentVersion reads")
    async with sf() as session:
        row = await session.get(AgentVersionRow, version_id)
        if row is None or row.org_id != org_id:
            return None
        return row


async def get_agent_version_by_digest(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    digest: str,
) -> AgentVersionRow | None:
    """Return the version in ``org_id`` with ``digest``, or ``None``.

    Used for idempotent import (PR-051: re-importing identical content hits
    the same digest and returns the existing version rather than colliding).
    """
    if not org_id:
        raise ValueError("org_id is required for AgentVersion reads")
    async with sf() as session:
        stmt = select(AgentVersionRow).where(
            AgentVersionRow.org_id == org_id,
            AgentVersionRow.digest == digest,
        )
        result = await session.execute(stmt)
        return result.scalars().first()


async def list_agent_versions(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    package_id: str | None = None,
    include_archived: bool = False,
) -> list[AgentVersionRow]:
    """Return versions in ``org_id``, optionally filtered by package; newest-first."""
    if not org_id:
        raise ValueError("org_id is required for AgentVersion reads")
    async with sf() as session:
        stmt = select(AgentVersionRow).where(AgentVersionRow.org_id == org_id)
        if package_id is not None:
            stmt = stmt.where(AgentVersionRow.package_id == package_id)
        if not include_archived:
            stmt = stmt.where(AgentVersionRow.status != VERSION_ARCHIVED)
        stmt = stmt.order_by(AgentVersionRow.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def update_agent_version(
    sf: async_sessionmaker[AsyncSession],
    *,
    version_id: str,
    org_id: str,
    version: str | None = None,
    manifest: dict | None = None,
    content: str | None = None,
    inline_size_threshold: int = _DEFAULT_INLINE_THRESHOLD,
    object_store: ObjectStore | None = None,
    session: AsyncSession | None = None,
) -> AgentVersionRow:
    """PATCH mutable fields on a draft/reviewed version. Refuses published.

    ADR §3.2 / §4.3: once ``status == 'published'`` (or ``revoked``), the
    content / manifest / digest / version are FROZEN. Attempting to update
    any of those raises :class:`VersionImmutableError` (the router maps it
    to 409). ``status`` transitions go through :func:`set_version_status`.

    When ``content`` is supplied, the digest is recomputed and storage is
    re-routed (the old object_key, if any, is superseded — GC of orphaned
    objects is ADR §11.3, a follow-up). Raises ``ValueError`` if the row is
    absent / wrong Org (router → 404).
    """
    store = object_store if object_store is not None else InlineObjectStore()

    async def _do(session: AsyncSession) -> AgentVersionRow:
        row = await session.get(AgentVersionRow, version_id)
        if row is None or row.org_id != org_id:
            raise ValueError(f"AgentVersion {version_id!r} not found in org {org_id!r}")
        _assert_mutable(row)
        if version is not None:
            row.version = version
        if manifest is not None:
            row.manifest = manifest
        if content is not None:
            digest = compute_artifact_digest(content)
            size_bytes = len(content.encode("utf-8"))
            if size_bytes <= inline_size_threshold:
                row.content_inline = content
                row.object_key = None
            else:
                object_key = compute_object_key(org_id=org_id, workspace_id=None, version_id=version_id)
                store.put(object_key=object_key, content=content.encode("utf-8"))
                row.content_inline = None
                row.object_key = object_key
            row.digest = digest
            row.size_bytes = size_bytes
        await session.flush()
        return row

    if session is not None:
        return await _do(session)
    async with sf() as session:
        row = await _do(session)
        await session.commit()
        await session.refresh(row)
    return row


async def set_version_status(
    sf: async_sessionmaker[AsyncSession],
    *,
    version_id: str,
    org_id: str,
    status: str,
    session: AsyncSession | None = None,
) -> AgentVersionRow:
    """Transition a version's status per the ADR §4 state machine.

    Legal transitions enforced here (not just the DB CHECK):

    * → ``reviewed``: from ``draft`` (content may still change afterwards).
    * → ``published``: from ``draft`` / ``reviewed``. Stamps ``published_at``.
      After this the content is frozen (ADR §4.3).
    * → ``revoked``: from ``published`` (and draft/reviewed). Stamps
      ``revoked_at``. A revoked version cannot create new Runs (PR-054) but
      history is preserved (ADR §4.4).
    * → ``archived``: from ``draft`` / ``reviewed`` (not published/revoked).
      ADR §4.5.

    Raises :class:`IllegalVersionTransitionError` on an illegal move;
    ``ValueError`` if absent / wrong Org (router → 404).
    """
    if status not in _ALLOWED_VERSION_STATUSES:
        raise ValueError(f"Unknown AgentVersion status {status!r}; allowed: {sorted(_ALLOWED_VERSION_STATUSES)}")

    async def _do(session: AsyncSession) -> AgentVersionRow:
        row = await session.get(AgentVersionRow, version_id)
        if row is None or row.org_id != org_id:
            raise ValueError(f"AgentVersion {version_id!r} not found in org {org_id!r}")
        _assert_transition(row.status, status)
        row.status = status
        if status == VERSION_PUBLISHED and row.published_at is None:
            row.published_at = datetime.now(UTC)
        if status == VERSION_REVOKED and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
        await session.flush()
        return row

    if session is not None:
        return await _do(session)
    async with sf() as session:
        row = await _do(session)
        await session.commit()
        await session.refresh(row)
    return row


def _assert_mutable(row: AgentVersionRow) -> None:
    """Raise if ``row`` is past the mutable window (published/revoked/archived)."""
    if row.status in {VERSION_PUBLISHED, VERSION_REVOKED, VERSION_ARCHIVED}:
        raise VersionImmutableError(f"AgentVersion {row.id!r} is {row.status!r}; content/manifest/digest/version are immutable (ADR-0004 §3.2). Create a new version to change content.")


# Legal source → target transitions (ADR §4). Draft/reviewed are mutable;
# published/revoked/archived are terminal for content purposes.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    VERSION_DRAFT: frozenset({VERSION_REVIEWED, VERSION_PUBLISHED, VERSION_REVOKED, VERSION_ARCHIVED}),
    VERSION_REVIEWED: frozenset({VERSION_DRAFT, VERSION_PUBLISHED, VERSION_REVOKED, VERSION_ARCHIVED}),
    VERSION_PUBLISHED: frozenset({VERSION_REVOKED}),
    VERSION_REVOKED: frozenset(),  # terminal
    VERSION_ARCHIVED: frozenset(),  # terminal
}


def _assert_transition(current: str, target: str) -> None:
    if target not in _LEGAL_TRANSITIONS.get(current, frozenset()):
        raise IllegalVersionTransitionError(f"Illegal AgentVersion transition {current!r} → {target!r} (ADR-0004 §4 state machine).")


async def count_versions_by_org(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
) -> int:
    """Return the number of versions in ``org_id`` (any status)."""
    if not org_id:
        raise ValueError("org_id is required for AgentVersion reads")
    async with sf() as session:
        stmt = select(func.count()).select_from(AgentVersionRow).where(AgentVersionRow.org_id == org_id)
        result = await session.execute(stmt)
        return int(result.scalar_one())
