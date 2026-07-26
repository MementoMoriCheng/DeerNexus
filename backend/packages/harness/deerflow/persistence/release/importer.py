"""File-state Agent import (PR-051, ADR-0004 §10).

The orchestration layer that turns an on-disk custom agent (``config.yaml`` +
``SOUL.md``) into an immutable ``AgentVersion`` (and a parent
``AgentPackage`` if absent). It reuses the PR-052 repository write path
(``create_agent_package`` / ``create_agent_version`` / digest / storage
routing) and the PR-050 ORM tables — no new schema lands here.

ADR-0004 §10 flow, implemented in :func:`import_agent_from_file`:

    discover file asset
    → validate path / schema
    → materialize exact artifact
    → calculate digest
    → create Package if needed
    → create draft Version
    → review / publish              (caller-driven; not part of this function)

Scope cut (documented against ADR §10 step 6): the Catalog index entry
(``catalog_entries`` table) is deferred to PR-054, where the table, the
``GET /catalog`` reader, and the cross-resource (agents + skills + mcp +
tools) discovery model land together. Provenance is recorded here only via
``Manifest.source_metadata`` so the import is self-evidencing without
pre-empting PR-054's schema.

Artifact bytes & digest
-----------------------
The DeerFlow file-state agent is ``config.yaml`` (5 ``AgentConfig`` fields) +
``SOUL.md`` (free-form prompt). ADR §3.3 mandates a richer 12-field
``Manifest``. The importer **projects** the file state into a Manifest
(SOUL.md inlined into ``soul_or_prompt_ref``) and computes the digest over
the deterministic canonical-JSON serialisation of that Manifest
(``sort_keys=True, separators=(",", ":")``). The digest is therefore stable
for the same logical content regardless of YAML key order or whitespace,
matching the PR-052 ``Manifest``-as-artifact contract. The persisted
``content`` passed to the repository is the same canonical-JSON string.

Reviewed gate (ADR §9.1)
------------------------
This PR implements the four file-handling gates: path-traversal containment,
symlink rejection, artifact size cap, and digest verification. The remaining
five (dangerous-binary, dependency-lock, Tool/MCP risk, network/Secret,
load-test) are deferred — most depend on richer manifests or the file-import
walk that PR-051 itself unlocks.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.config.agents_config import SOUL_FILENAME, AgentConfig, validate_agent_name
from deerflow.config.paths import Paths
from deerflow.contracts.agent_artifact import Manifest
from deerflow.persistence.release.digest import compute_artifact_digest
from deerflow.persistence.release.model import AgentPackageRow, AgentVersionRow
from deerflow.persistence.release.repository import (
    _DEFAULT_INLINE_THRESHOLD,
    create_agent_package,
    create_agent_version,
    get_agent_package,
    get_agent_package_by_name,
    get_agent_version_by_digest,
)

#: Sentinel entry-point identifier for a file-imported agent whose runtime
#: entry is the inlined SOUL prompt (no separate script). Distinct from a
#: future code-bearing artifact whose ``agent_entry`` would name a callable.
AGENT_ENTRY_SOUL = "soul"

#: Manifest schema version stamped on every file-imported Version. Bumped
#: only if the projection mapping below changes shape (which would itself
#: change every digest — operators must re-import under the new schema).
FILE_IMPORT_SCHEMA_VERSION = "1.0"

#: Provenance tag written into ``Manifest.source_metadata["source"]``.
SOURCE_FILE_IMPORT = "file_import"

#: Hard cap on a single source file (SOUL.md). Independent of the inline
#: threshold (which routes storage, not validity) — this rejects a runaway
#: SOUL before it can dominate the artifact. 1 MiB is generous for prompts.
MAX_SOURCE_FILE_BYTES = 1024 * 1024


class ImportPathError(Exception):
    """Raised when the resolved agent path escapes ``base_dir`` or is a symlink.

    ADR §9.1 (path traversal + symlink gates). The router maps this to 400.
    """


class ArtifactTooLargeError(Exception):
    """Raised when a single source file exceeds :data:`MAX_SOURCE_FILE_BYTES`.

    The router maps this to 413. The inline/object threshold routes storage
    but does not reject large artifacts; this cap rejects unbounded inputs.
    """


def _project_manifest(
    *,
    agent_config: AgentConfig,
    soul: str | None,
    agent_dir: Path,
) -> Manifest:
    """Project a file-state ``AgentConfig`` + SOUL into a :class:`Manifest`.

    Mapping (see ADR §3.3 for the target shape):

    * ``schema_version`` ← ``FILE_IMPORT_SCHEMA_VERSION``
    * ``agent_entry`` ← ``AGENT_ENTRY_SOUL`` (SOUL is the entry prompt)
    * ``soul_or_prompt_ref`` ← SOUL.md body (``""`` if absent — the field is
      nullable but we persist an empty string so the manifest is self-describing;
      a ``None`` would read as "no prompt" which is misleading for an agent)
    * ``model_requirements`` ← ``[{"name": model}]`` when ``model`` set
    * ``skills`` ← ``[{"name": s} for s in skills]`` when ``skills`` is a list;
      ``None`` (load-all fallback) projects to ``[]`` — the manifest is a
      snapshot, not a resolver, so "all skills" is recorded as "none pinned"
    * ``tools`` ← ``tool_groups or []``
    * ``mcp_servers`` / ``dependencies`` / ``network_requirements`` /
      ``secret_requirements`` ← ``[]`` (the file format carries none of these;
      AgentConfig strips unknown fields at load, and Q3 of the PR-051 design
      confirmed no secret-bearing fields exist)
    * ``runtime_limits`` ← ``None``
    * ``source_metadata`` ← provenance dict (path / files / imported_at /
      source); ``upstream_commit`` is omitted because ``base_dir`` is not
      assumed to be a git repo
    """
    model_requirements: list[dict[str, Any]] = [{"name": agent_config.model}] if agent_config.model else []
    skills = [{"name": s} for s in agent_config.skills] if isinstance(agent_config.skills, list) else []
    tools = list(agent_config.tool_groups or [])
    source_metadata = {
        "source": SOURCE_FILE_IMPORT,
        "path": str(agent_dir),
        "files": ["config.yaml", SOUL_FILENAME],
        "imported_at": datetime.now(UTC).isoformat(),
    }
    return Manifest(
        schema_version=FILE_IMPORT_SCHEMA_VERSION,
        agent_entry=AGENT_ENTRY_SOUL,
        soul_or_prompt_ref=soul or "",
        model_requirements=model_requirements,
        skills=skills,
        tools=tools,
        mcp_servers=[],
        dependencies=[],
        network_requirements=[],
        secret_requirements=[],
        runtime_limits=None,
        source_metadata=source_metadata,
    )


def canonical_manifest_json(manifest: Manifest) -> str:
    """Return the deterministic serialisation used as the artifact bytes.

    ``sort_keys=True`` + ``separators=(",", ":")`` removes key-order and
    whitespace variance so the digest is stable across YAML reformatting and
    across Python dict insertion order. The same string is persisted as the
    Version ``content`` (→ ``content_inline`` or object_key via the
    repository) so the digest is provably over the stored bytes.

    ``source_metadata`` is **excluded** from the digest: it carries
    provenance (path / files / ``imported_at``), which is non-deterministic
    (a fresh timestamp on every call) and is not part of the execution
    identity (ADR §3.2 "digest 对存储的精确制品字节计算" — the artifact bytes
    are the agent content, not its import receipt). Excluding it is what
    makes the digest reproducible and the ADR §10 "重复 digest 导入幂等"
    guarantee hold: re-importing identical content yields the identical
    digest regardless of when the second import runs. The full manifest
    (with ``source_metadata``) is still persisted in the ``manifest`` JSON
    column for provenance — only the digested ``content`` omits it.
    """
    data = manifest.model_dump(mode="json")
    data.pop("source_metadata", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _resolve_agent_dir(
    *,
    base_dir: Path,
    name: str,
    user_id: str | None,
) -> Path:
    """Resolve the on-disk agent dir, mirroring ``resolve_agent_dir``.

    Reimplemented locally (rather than calling
    :func:`deerflow.config.agents_config.resolve_agent_dir`) so the caller can
    inject ``base_dir`` for tests — the global loader hard-binds to the
    ``get_paths()`` singleton. The per-user layout wins over the legacy
    shared layout when both exist, matching the production resolver.
    """
    paths = Paths(base_dir=base_dir)
    effective_user = user_id or "default"
    user_path = paths.user_agent_dir(effective_user, name)
    legacy_path = paths.agent_dir(name)
    if (user_path / "config.yaml").exists():
        return user_path
    if (legacy_path / "config.yaml").exists():
        return legacy_path
    # Neither exists — return the per-user path so the caller's FileNotFoundError
    # message points at where a new agent would land (existence-hiding is the
    # router's concern; the importer surfaces the missing dir honestly).
    return user_path


def _validate_path(
    *,
    base_dir: Path,
    agent_dir: Path,
    config_file: Path,
    soul_file: Path,
) -> None:
    """Enforce the path-traversal, symlink, and size gates (ADR §9.1).

    * ``agent_dir`` (resolved) must stay inside ``base_dir`` (resolved).
    * ``agent_dir`` / ``config_file`` / ``soul_file`` must not be symlinks
      (a symlink could point outside ``base_dir`` even when the link itself
      sits inside it).
    * Each source file must be under :data:`MAX_SOURCE_FILE_BYTES`.
    """
    base_resolved = base_dir.resolve()
    agent_resolved = agent_dir.resolve()
    try:
        agent_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ImportPathError(f"Agent directory {agent_dir} resolves outside base_dir {base_dir}; refusing import.") from exc

    for path in (agent_dir, config_file, soul_file):
        if path.is_symlink():
            raise ImportPathError(f"{path} is a symlink; symlink agents are refused (ADR-0004 §9.1).")

    for path in (config_file, soul_file):
        if path.exists() and path.stat().st_size > MAX_SOURCE_FILE_BYTES:
            raise ArtifactTooLargeError(f"{path.name} is {path.stat().st_size} bytes (> {MAX_SOURCE_FILE_BYTES}); refusing import.")


def _load_config_yaml(config_file: Path, name: str) -> AgentConfig:
    """Read + validate ``config.yaml`` (mirrors ``load_agent_config``).

    Strips unknown keys before validation so a legacy ``config.yaml`` carrying
    extra fields does not fail import (matches the production loader). Raises
    ``FileNotFoundError`` if the file is absent.
    """
    if not config_file.exists():
        raise FileNotFoundError(f"Agent config not found: {config_file}")
    try:
        with config_file.open(encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse agent config {config_file}: {exc}") from exc
    if "name" not in data:
        data["name"] = name
    known_fields = set(AgentConfig.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}
    try:
        return AgentConfig(**data)
    except ValidationError as exc:
        raise ValueError(f"Agent config {config_file} failed validation: {exc}") from exc


def _load_soul(soul_file: Path) -> str | None:
    """Read ``SOUL.md`` if present (mirrors ``load_agent_soul``)."""
    if not soul_file.exists():
        return None
    content = soul_file.read_text(encoding="utf-8").strip()
    return content or None


async def import_agent_from_file(
    sf: async_sessionmaker[AsyncSession],
    *,
    org_id: str,
    name: str,
    version: str,
    user_id: str | None = None,
    display_name: str | None = None,
    description: str | None = None,
    workspace_id: str | None = None,
    created_by: str | None = None,
    base_dir: Path | str | None = None,
    inline_size_threshold: int = _DEFAULT_INLINE_THRESHOLD,
    session: AsyncSession | None = None,
) -> tuple[AgentPackageRow, AgentVersionRow, str, bool, dict]:
    """Import one file-state agent into the artifact store (ADR-0004 §10).

    Resolves the agent directory under ``base_dir`` (default
    ``get_paths().base_dir`` — pass a path to override, e.g. from tests),
    validates path/symlink/size, projects ``config.yaml`` + ``SOUL.md`` into a
    :class:`Manifest`, computes the digest over the canonical Manifest JSON,
    and:

    * If a Version with the same digest already exists in ``org_id`` (idempotent
      re-import, ADR §10 "重复 digest 导入幂等"), returns
      ``(existing_package, existing_version, digest, imported=False, source_metadata)``
      without writing.
    * Otherwise creates the parent ``AgentPackage`` if absent (looked up by
      ``(org_id, name)``) and a fresh ``draft`` ``AgentVersion`` whose
      ``content`` is the canonical Manifest JSON. Returns
      ``(package, version, digest, imported=True, source_metadata)``.

    The caller (router) owns the Class A audit-outbox enqueue; pass an open
    ``session`` to stage the create + audit row in one atomic commit
    (ADR-0005 §7.1). ``display_name`` / ``description`` default from the file
    config when the package is freshly created; they are NOT applied to an
    existing package (PATCH is a separate endpoint).

    Raises :class:`ImportPathError` (path traversal / symlink),
    :class:`ArtifactTooLargeError` (file > 1 MiB), or ``FileNotFoundError``
    (agent dir absent). All four map to 4xx in the router.
    """
    # 1. Resolve + validate the on-disk agent.
    name = validate_agent_name(name)
    resolved_base = Path(base_dir).resolve() if base_dir is not None else Paths().base_dir
    agent_dir = _resolve_agent_dir(base_dir=resolved_base, name=name, user_id=user_id)
    if not agent_dir.exists():
        raise FileNotFoundError(f"Agent directory not found: {agent_dir}")
    config_file = agent_dir / "config.yaml"
    soul_file = agent_dir / SOUL_FILENAME
    _validate_path(base_dir=resolved_base, agent_dir=agent_dir, config_file=config_file, soul_file=soul_file)

    agent_config = _load_config_yaml(config_file, name)
    soul = _load_soul(soul_file)

    # 2. Project + canonicalise + digest.
    manifest = _project_manifest(
        agent_config=agent_config,
        soul=soul,
        agent_dir=agent_dir,
    )
    canonical = canonical_manifest_json(manifest)
    digest = compute_artifact_digest(canonical)

    # 3. Idempotent dedupe by digest (ADR §10). The lookup is Org-scoped, so a
    #    foreign Org's identical-content Version does not short-circuit here.
    existing_version = await get_agent_version_by_digest(sf, org_id=org_id, digest=digest)
    if existing_version is not None:
        existing_package = await get_agent_package(sf, package_id=existing_version.package_id, org_id=org_id)
        # FK is ON DELETE RESTRICT + the version is Org-scoped, so the parent
        # MUST exist in this Org. A None here is an invariant violation — let
        # it raise loudly rather than return a half-empty report.
        assert existing_package is not None  # noqa: S101 — invariant guard, not user input
        return existing_package, existing_version, digest, False, manifest.source_metadata or {}

    # 4. Create the parent package if absent.
    pkg = await get_agent_package_by_name(sf, org_id=org_id, name=name, session=session)
    if pkg is None:
        pkg = await create_agent_package(
            sf,
            org_id=org_id,
            name=name,
            display_name=display_name or name,
            description=description if description is not None else (agent_config.description or None),
            workspace_id=workspace_id,
            created_by=created_by,
            session=session,
        )

    # 5. Create the draft Version. ``content`` is the canonical Manifest JSON
    #    so the stored bytes are byte-identical to the digested bytes.
    new_version = await create_agent_version(
        sf,
        org_id=org_id,
        package_id=pkg.id,
        version=version,
        manifest=manifest.model_dump(mode="json"),
        content=canonical,
        workspace_id=workspace_id if workspace_id is not None else pkg.workspace_id,
        created_by=created_by,
        inline_size_threshold=inline_size_threshold,
        session=session,
    )
    return pkg, new_version, digest, True, manifest.source_metadata or {}
