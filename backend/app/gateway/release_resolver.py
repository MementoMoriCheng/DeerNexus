"""Concrete ``ReleaseResolver`` adapter backed by the release tables (PR-054).

Implements ``deerflow.contracts.release.ReleaseResolver`` (the harness-facing
Protocol) against the PR-050/052/053 tables:

* ``agent_packages`` — agent_name → package_id lookup (``get_agent_package_by_name``)
* ``release_channels`` — (org, workspace, package, channel) → current_version_id
* ``agent_versions`` — current_version_id → version + digest (the execution identity)

The resolver is the **read side** of ADR-0004 §6 atomic flow steps 4-5
(resolve target Package + Channel; verify Version status / Org / digest /
object existence). The Run-pin write side (step 7: persist the resolved
``ReleaseRef`` into ``runs``) is a separate PR — this adapter only resolves.

Failure semantics
-----------------

Resolution failures raise :class:`ReleaseResolutionError` carrying a ``code``
that aligns 1:1 with the ``ErrorCode`` enum members in
``deerflow.contracts.errors`` (``release_not_found`` /
``release_not_published`` / ``release_revoked`` / ``release_tenant_mismatch``).
The Run-creation entry point (a follow-up PR) catches this and translates it
to a ``ContractError.from_code(...)`` envelope. This mirrors the existing
exception convention (``VersionImmutableError`` / ``ReleaseConflictError``):
server-side code raises; the HTTP boundary translates.

The resolver deliberately **existence-hides** corruption and cross-Org misses
as ``release_not_found`` so an unauthorised caller cannot distinguish "no such
agent" from "tampered artifact" — the message is identical from outside.

prod gate (ADR §9.2)
--------------------

For ``channel == "prod"`` the resolver enforces the subset of the §9.2 gate
that is verifiable at read time:

* Version ``status == "published"`` (else ``release_not_published``)
* Version ``status != "revoked"`` (else ``release_revoked``)
* Same-Org across Package / Version / Channel (enforced by the Org-scoped
  repository reads; a cross-Org row is invisible → ``release_not_found``)
* inline digest verification: when ``content_inline IS NOT NULL``, recompute
  ``compute_artifact_digest(content_inline)`` and compare to ``row.digest``;
  mismatch → ``release_not_found`` (existence-hidden corruption). The
  ``object_key`` path (``content_inline IS NULL``) skips this — S3 object
  existence + digest verification is a follow-up (the inline backend is the
  only store today).

dev / staging channels do NOT run the prod gate (ADR §5 channel policy: dev
allows draft/reviewed/published; staging allows reviewed/published).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from deerflow.contracts.context import TenantContext
from deerflow.contracts.release import ReleaseRef
from deerflow.contracts.versioning import CURRENT_SCHEMA_VERSION
from deerflow.persistence.release import (
    CHANNEL_PROD,
    VERSION_PUBLISHED,
    VERSION_REVOKED,
    compute_artifact_digest,
    get_agent_package_by_name,
    get_agent_version,
    get_channel,
)

#: Error codes — align 1:1 with ``ErrorCode`` members in
#: ``deerflow.contracts.errors`` (release_*). The Run-creation entry point
#: maps ``ReleaseResolutionError.code`` to ``ContractError.from_code``.
CODE_RELEASE_NOT_FOUND = "release_not_found"
CODE_RELEASE_NOT_PUBLISHED = "release_not_published"
CODE_RELEASE_REVOKED = "release_revoked"
CODE_RELEASE_TENANT_MISMATCH = "release_tenant_mismatch"


class ReleaseResolutionError(Exception):
    """Raised when the resolver cannot produce a valid ``ReleaseRef``.

    ``code`` is one of the ``CODE_RELEASE_*`` constants above. The Run-creation
    entry point catches this and translates it to a ``ContractError`` envelope
    via ``ContractError.from_code(code, ...)``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DbReleaseResolver:
    """DB-backed ``ReleaseResolver`` (ADR-0004 §6 read side).

    Construct with the app's session factory. ``inline_digest_check`` defaults
    True (ADR §9.2 prod gate); tests may disable it to exercise the object_key
    path without a real store. The class satisfies the
    :class:`deerflow.contracts.release.ReleaseResolver` Protocol structurally
    (duck-typed — no runtime ``isinstance`` against the Protocol needed).
    """

    def __init__(
        self,
        sf: async_sessionmaker,
        *,
        inline_digest_check: bool = True,
    ) -> None:
        self._sf = sf
        self._inline_digest_check = inline_digest_check

    async def resolve(
        self,
        tenant: TenantContext,
        agent_name: str,
        channel: str,
    ) -> ReleaseRef:
        """Resolve the current ``ReleaseRef`` for ``agent_name`` on ``channel``.

        See module docstring for the lookup chain + prod gate + failure codes.
        """
        org_id = getattr(tenant, "org_id", None)
        if not org_id:
            raise ReleaseResolutionError(
                CODE_RELEASE_TENANT_MISMATCH,
                "TenantContext has no bound org_id; cannot resolve release.",
            )
        workspace_id = getattr(tenant, "workspace_id", None)

        # 1. agent_name → package (cross-Org existence-hidden).
        pkg = await get_agent_package_by_name(self._sf, org_id=org_id, name=agent_name)
        if pkg is None:
            raise ReleaseResolutionError(
                CODE_RELEASE_NOT_FOUND,
                f"Agent {agent_name!r} not found in org {org_id!r}.",
            )

        # 2. (org, workspace, package, channel) → channel pointer.
        ch = await get_channel(
            self._sf,
            org_id=org_id,
            package_id=pkg.id,
            channel=channel,
            workspace_id=workspace_id,
        )
        if ch is None or ch.current_version_id is None:
            raise ReleaseResolutionError(
                CODE_RELEASE_NOT_FOUND,
                f"Channel {channel!r} for agent {agent_name!r} has no current version.",
            )

        # 3. current_version_id → Version (cross-Org existence-hidden).
        ver = await get_agent_version(self._sf, version_id=ch.current_version_id, org_id=org_id)
        if ver is None:
            raise ReleaseResolutionError(
                CODE_RELEASE_NOT_FOUND,
                f"Current version {ch.current_version_id!r} not found.",
            )

        # 4. prod gate (ADR §9.2). dev/staging skip this (ADR §5).
        if channel == CHANNEL_PROD:
            self._assert_prod_gate(ver)

        # 5. Build the immutable ReleaseRef. ``digest`` is the execution
        #    identity; ``version`` is the SemVer display string.
        return ReleaseRef(
            schema_version=CURRENT_SCHEMA_VERSION,
            org_id=org_id,
            workspace_id=workspace_id,
            package_id=pkg.id,
            agent_name=pkg.name,
            version=ver.version,
            digest=ver.digest,
            channel=channel,  # type: ignore[arg-type]
            resolved_at=datetime.now(UTC),
        )

    def _assert_prod_gate(self, ver) -> None:
        """Enforce the prod-channel gate (ADR §9.2 read-time subset)."""
        if ver.status == VERSION_REVOKED:
            raise ReleaseResolutionError(
                CODE_RELEASE_REVOKED,
                f"Version {ver.id!r} is revoked; cannot resolve for prod.",
            )
        if ver.status != VERSION_PUBLISHED:
            raise ReleaseResolutionError(
                CODE_RELEASE_NOT_PUBLISHED,
                f"Version {ver.id!r} status {ver.status!r} is not published; prod requires published.",
            )
        # Inline digest verification — the inline-backend subset of "digest
        # object exists & matches" (ADR §9.2). The object_key path skips this
        # (S3 existence check is a follow-up; the inline backend is the only
        # store today). Corruption is existence-hidden as release_not_found.
        if self._inline_digest_check and ver.content_inline is not None:
            recomputed = compute_artifact_digest(ver.content_inline)
            if recomputed != ver.digest:
                raise ReleaseResolutionError(
                    CODE_RELEASE_NOT_FOUND,
                    f"Version {ver.id!r} content digest mismatch (stored {ver.digest!r} != recomputed {recomputed!r}).",
                )
