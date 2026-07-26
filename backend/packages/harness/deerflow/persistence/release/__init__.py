"""Agent-artifact control-plane: ORM models + repository + storage + import + channels (PR-050 / PR-052 / PR-051 / PR-053).

Re-exports:

* the ORM row classes (PR-050/053) so ``deerflow.persistence.models`` registers
  them with ``Base.metadata`` in a single import;
* the repository write path (PR-052): CRUD, published-immutability,
  session passthrough, inline/object threshold routing;
* the digest + ObjectStore primitives + the inventory reconciler (PR-052);
* the file-state importer (PR-051): projects ``config.yaml`` + ``SOUL.md``
  into a ``Manifest``, computes the canonical-JSON digest, dedupes by digest,
  and creates the parent Package + draft Version in one transactional path;
* the channel layer (PR-053): ``release_channels`` pointer CAS +
  ``release_events`` append-only history + promote/rollback.

The app layer (``app/gateway/routers/agent_artifacts.py``) imports the
repository + inventory + importer + channel primitives from here rather than
reaching into submodules, so the package boundary is the import surface.
"""

from deerflow.persistence.release.digest import compute_artifact_digest
from deerflow.persistence.release.idempotency import (  # noqa: E402 — PR-055 replay store
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IdempotencyConflictError,
    compute_request_hash,
    get_idempotency_record,
    insert_idempotency_record,
    resolve_idempotency_outcome,
)
from deerflow.persistence.release.importer import (
    AGENT_ENTRY_SOUL,
    FILE_IMPORT_SCHEMA_VERSION,
    MAX_SOURCE_FILE_BYTES,
    SOURCE_FILE_IMPORT,
    ArtifactTooLargeError,
    ImportPathError,
    canonical_manifest_json,
    import_agent_from_file,
)
from deerflow.persistence.release.inventory import ReconcileReport, reconcile_versions
from deerflow.persistence.release.model import (
    CHANNEL_DEV,
    CHANNEL_PROD,
    CHANNEL_STAGING,
    EVENT_ACTION_PROMOTE,
    EVENT_ACTION_ROLLBACK,
    AgentPackageRow,
    AgentVersionRow,
    ReleaseChannelRow,
    ReleaseEventRow,
)
from deerflow.persistence.release.repository import (
    PACKAGE_ACTIVE,
    PACKAGE_ARCHIVED,
    VERSION_ARCHIVED,
    VERSION_DRAFT,
    VERSION_PUBLISHED,
    VERSION_REVIEWED,
    VERSION_REVOKED,
    ChannelGateError,
    IllegalVersionTransitionError,
    ReleaseConflictError,
    VersionImmutableError,
    archive_agent_package,
    count_versions_by_org,
    create_agent_package,
    create_agent_version,
    get_agent_package,
    get_agent_package_by_name,
    get_agent_version,
    get_agent_version_by_digest,
    get_channel,
    get_or_create_channel,
    list_agent_packages,
    list_agent_versions,
    list_channels,
    list_events,
    promote_channel,
    rollback_channel,
    set_version_status,
    update_agent_package,
    update_agent_version,
)
from deerflow.persistence.release.storage import (
    DEFAULT_WORKSPACE_SEGMENT,
    InlineObjectStore,
    ObjectStore,
    compute_object_key,
)

__all__ = [
    # ORM models (PR-050 / PR-053)
    "AgentPackageRow",
    "AgentVersionRow",
    "ReleaseChannelRow",
    "ReleaseEventRow",
    # digest (PR-052)
    "compute_artifact_digest",
    # storage (PR-052)
    "DEFAULT_WORKSPACE_SEGMENT",
    "InlineObjectStore",
    "ObjectStore",
    "compute_object_key",
    # repository (PR-052)
    "PACKAGE_ACTIVE",
    "PACKAGE_ARCHIVED",
    "VERSION_ARCHIVED",
    "VERSION_DRAFT",
    "VERSION_PUBLISHED",
    "VERSION_REVOKED",
    "VERSION_REVIEWED",
    "IllegalVersionTransitionError",
    "VersionImmutableError",
    "archive_agent_package",
    "count_versions_by_org",
    "create_agent_package",
    "create_agent_version",
    "get_agent_package",
    "get_agent_package_by_name",
    "get_agent_version",
    "get_agent_version_by_digest",
    "list_agent_packages",
    "list_agent_versions",
    "set_version_status",
    "update_agent_package",
    "update_agent_version",
    # inventory (PR-052)
    "ReconcileReport",
    "reconcile_versions",
    # importer (PR-051)
    "AGENT_ENTRY_SOUL",
    "FILE_IMPORT_SCHEMA_VERSION",
    "MAX_SOURCE_FILE_BYTES",
    "SOURCE_FILE_IMPORT",
    "ArtifactTooLargeError",
    "ImportPathError",
    "canonical_manifest_json",
    "import_agent_from_file",
    # channel layer (PR-053)
    "CHANNEL_DEV",
    "CHANNEL_PROD",
    "CHANNEL_STAGING",
    "EVENT_ACTION_PROMOTE",
    "EVENT_ACTION_ROLLBACK",
    "ChannelGateError",
    "ReleaseConflictError",
    "get_channel",
    "get_or_create_channel",
    "list_channels",
    "list_events",
    "promote_channel",
    "rollback_channel",
    # idempotency replay store (PR-055)
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "IdempotencyConflictError",
    "compute_request_hash",
    "get_idempotency_record",
    "insert_idempotency_record",
    "resolve_idempotency_outcome",
]
