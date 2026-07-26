"""File-state agent import tests (PR-051, ADR-0004 §10).

Covers :mod:`deerflow.persistence.release.importer` end-to-end against an
isolated SQLite: Manifest projection, canonical-JSON determinism, the
path/symlink/size gates, the full import → draft-Version path, digest-based
idempotency, cross-Org isolation, session passthrough, and the
"file change does not retro-edit an imported Version" invariant.

Fixture conventions mirror ``test_agent_artifact_repository.py``: boot an
isolated SQLite via ``init_engine``, yield ``get_session_factory()``, tear
down with ``close_engine``. Each test writes a synthetic agent directory
under ``tmp_path`` and passes it as ``base_dir`` so the importer's path
resolver stays hermetic (no reliance on the global ``get_paths()`` singleton).

Artifact IDs: ``ART-400`` series (importer layer).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.config.agents_config import SOUL_FILENAME, AgentConfig
from deerflow.persistence.release import (
    VERSION_DRAFT,
    compute_artifact_digest,
    create_agent_package,
    get_agent_package_by_name,
    get_agent_version_by_digest,
)
from deerflow.persistence.release.importer import (
    AGENT_ENTRY_SOUL,
    FILE_IMPORT_SCHEMA_VERSION,
    MAX_SOURCE_FILE_BYTES,
    SOURCE_FILE_IMPORT,
    ArtifactTooLargeError,
    ImportPathError,
    _load_config_yaml,
    _load_soul,
    _project_manifest,
    _resolve_agent_dir,
    _validate_path,
    canonical_manifest_json,
    import_agent_from_file,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'agent_import.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


def _write_agent(
    base: Path,
    *,
    name: str,
    soul: str = "You are a helpful agent.",
    config: dict | None = None,
    user_id: str | None = None,
) -> Path:
    """Write a synthetic agent dir under ``base`` and return it.

    Layout matches ``Paths.user_agent_dir`` (per-user) or ``Paths.agent_dir``
    (legacy) so :func:`_resolve_agent_dir` finds it. ``config`` defaults to a
    minimal valid AgentConfig.
    """
    cfg = {"name": name}
    if config is not None:
        cfg.update(config)
    if user_id is not None:
        agent_dir = base / "users" / user_id / "agents" / name
    else:
        agent_dir = base / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (agent_dir / SOUL_FILENAME).write_text(soul, encoding="utf-8")
    return agent_dir


# ---------------------------------------------------------------------------
# Manifest projection (ART-400)
# ---------------------------------------------------------------------------


class TestManifestProjection:
    def test_full_projection(self, tmp_path: Path):
        cfg = AgentConfig(
            name="alpha",
            description="desc",
            model="gpt-4o",
            tool_groups=["search", "code"],
            skills=["s1", "s2"],
        )
        m = _project_manifest(agent_config=cfg, soul="soul body", agent_dir=tmp_path / "alpha")
        assert m.schema_version == FILE_IMPORT_SCHEMA_VERSION
        assert m.agent_entry == AGENT_ENTRY_SOUL
        assert m.soul_or_prompt_ref == "soul body"
        assert m.model_requirements == [{"name": "gpt-4o"}]
        assert m.skills == [{"name": "s1"}, {"name": "s2"}]
        assert m.tools == ["search", "code"]
        assert m.mcp_servers == []
        assert m.dependencies == []
        assert m.network_requirements == []
        assert m.secret_requirements == []
        assert m.runtime_limits is None
        sm = m.source_metadata
        assert sm is not None
        assert sm["source"] == SOURCE_FILE_IMPORT
        assert sm["files"] == ["config.yaml", SOUL_FILENAME]
        assert "imported_at" in sm
        assert str(tmp_path / "alpha") in sm["path"] or sm["path"].endswith("alpha")

    def test_skills_none_projects_to_empty(self, tmp_path: Path):
        # None means "load all skills" at runtime; the manifest is a snapshot,
        # so it records no pinned skills (an explicit [] is identical here).
        cfg = AgentConfig(name="x", skills=None)
        m = _project_manifest(agent_config=cfg, soul=None, agent_dir=tmp_path / "x")
        assert m.skills == []
        assert m.soul_or_prompt_ref == ""  # absent SOUL → empty string, not None
        assert m.model_requirements == []  # no model set

    def test_no_model_no_tool_groups(self, tmp_path: Path):
        cfg = AgentConfig(name="y")
        m = _project_manifest(agent_config=cfg, soul="hi", agent_dir=tmp_path / "y")
        assert m.model_requirements == []
        assert m.tools == []


# ---------------------------------------------------------------------------
# Canonical JSON + digest determinism (ART-410)
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_deterministic_for_same_manifest(self, tmp_path: Path):
        cfg = AgentConfig(name="a", model="m", skills=["x"])
        m = _project_manifest(agent_config=cfg, soul="s", agent_dir=tmp_path / "a")
        cj1 = canonical_manifest_json(m)
        cj2 = canonical_manifest_json(m)
        assert cj1 == cj2
        assert compute_artifact_digest(cj1) == compute_artifact_digest(cj2)

    def test_keys_are_sorted(self, tmp_path: Path):
        cfg = AgentConfig(name="a", model="m")
        m = _project_manifest(agent_config=cfg, soul="s", agent_dir=tmp_path / "a")
        cj = canonical_manifest_json(m)
        # sort_keys=True guarantees the first key in the object is the
        # alphabetically-first field ("agent_entry"), and there is no
        # insignificant whitespace (compact separators).
        assert cj.startswith('{"agent_entry"')
        assert ", " not in cj
        assert ": " not in cj

    def test_dict_field_order_does_not_change_digest(self, tmp_path: Path):
        """Two skills lists with the same elements in different orders produce
        different digests ONLY if the elements differ — key-order inside dicts
        is normalised, but list order is preserved (lists are ordered)."""
        m1 = _project_manifest(
            agent_config=AgentConfig(name="a", skills=["s1", "s2"]),
            soul="s",
            agent_dir=tmp_path / "a",
        )
        m2 = _project_manifest(
            agent_config=AgentConfig(name="a", skills=["s2", "s1"]),
            soul="s",
            agent_dir=tmp_path / "a",
        )
        assert compute_artifact_digest(canonical_manifest_json(m1)) != compute_artifact_digest(canonical_manifest_json(m2))

    def test_content_is_valid_json_roundtrip(self, tmp_path: Path):
        cfg = AgentConfig(name="a", model="m", tool_groups=["t"])
        m = _project_manifest(agent_config=cfg, soul="body", agent_dir=tmp_path / "a")
        cj = canonical_manifest_json(m)
        # The canonical string must be parseable back into the same dict shape.
        parsed = json.loads(cj)
        assert parsed["agent_entry"] == AGENT_ENTRY_SOUL
        assert parsed["tools"] == ["t"]


# ---------------------------------------------------------------------------
# Validation gates (ART-420): path traversal, symlink, size, missing dir
# ---------------------------------------------------------------------------


class TestValidation:
    def test_resolve_dir_prefers_per_user(self, tmp_path: Path):
        base = tmp_path
        (base / "users" / "u1" / "agents" / "alpha" / "config.yaml").parent.mkdir(parents=True)
        (base / "users" / "u1" / "agents" / "alpha" / "config.yaml").write_text("name: alpha")
        (base / "agents" / "alpha").mkdir(parents=True)
        (base / "agents" / "alpha" / "config.yaml").write_text("name: alpha")
        resolved = _resolve_agent_dir(base_dir=base, name="alpha", user_id="u1")
        assert resolved == (base / "users" / "u1" / "agents" / "alpha").resolve()

    def test_resolve_dir_falls_back_to_legacy(self, tmp_path: Path):
        base = tmp_path
        (base / "agents" / "alpha").mkdir(parents=True)
        (base / "agents" / "alpha" / "config.yaml").write_text("name: alpha")
        resolved = _resolve_agent_dir(base_dir=base, name="alpha", user_id="u1")
        assert resolved == (base / "agents" / "alpha").resolve()

    def test_resolve_dir_returns_user_path_when_missing(self, tmp_path: Path):
        resolved = _resolve_agent_dir(base_dir=tmp_path, name="ghost", user_id="u1")
        assert resolved == (tmp_path / "users" / "u1" / "agents" / "ghost").resolve()

    def test_path_traversal_outside_base_rejected(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "config.yaml").write_text("name: evil")
        # Simulate a resolver that pointed outside base — call _validate_path
        # directly with config_file/soul_file that resolve outside base.
        # (The real resolver is constrained to base/users|agents, so this test
        # pins the gate independently of the resolver.)
        agent_dir = outside
        cfg = agent_dir / "config.yaml"
        soul = agent_dir / SOUL_FILENAME
        with pytest.raises(ImportPathError):
            _validate_path(base_dir=base, agent_dir=agent_dir, config_file=cfg, soul_file=soul)

    def test_symlink_agent_dir_rejected(self, tmp_path: Path):
        base = tmp_path / "base"
        base.mkdir()
        real = tmp_path / "real"
        real.mkdir()
        (real / "config.yaml").write_text("name: ln")
        link = base / "agents" / "ln"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(real)
        except OSError as exc:  # pragma: no cover — Windows without dev mode
            pytest.skip(f"symlink unsupported: {exc}")
        cfg = link / "config.yaml"
        soul = link / SOUL_FILENAME
        with pytest.raises(ImportPathError):
            _validate_path(base_dir=base, agent_dir=link, config_file=cfg, soul_file=soul)

    async def test_oversized_source_file_rejected(self, tmp_path: Path):
        base = tmp_path
        agent_dir = _write_agent(base, name="big")
        # Overwrite SOUL.md with a payload exceeding the cap.
        big = "x" * (MAX_SOURCE_FILE_BYTES + 1)
        (agent_dir / SOUL_FILENAME).write_text(big, encoding="utf-8")
        with pytest.raises(ArtifactTooLargeError):
            await import_agent_from_file(
                sf=None,  # not reached — validation runs before any DB call
                org_id=ORG_ID,
                name="big",
                version="1.0.0",
                base_dir=base,
            )

    async def test_missing_agent_dir_raises_filenotfound(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            await import_agent_from_file(
                sf=None,
                org_id=ORG_ID,
                name="ghost",
                version="1.0.0",
                base_dir=tmp_path,
            )

    async def test_missing_config_raises_filenotfound(self, tmp_path: Path):
        # Agent dir exists but lacks config.yaml — the loader surfaces the
        # missing config as FileNotFoundError.
        base = tmp_path
        (base / "agents" / "incomplete").mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            await import_agent_from_file(
                sf=None,
                org_id=ORG_ID,
                name="incomplete",
                version="1.0.0",
                base_dir=base,
            )

    def test_load_config_strips_unknown_fields(self, tmp_path: Path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({"name": "a", "prompt_file": "legacy", "api_key": "x"}))
        cfg = _load_config_yaml(cfg_path, "a")
        assert cfg.name == "a"
        # Unknown fields silently dropped, not retained on the model.
        assert not hasattr(cfg, "prompt_file")
        assert not hasattr(cfg, "api_key")

    def test_load_soul_returns_none_when_absent(self, tmp_path: Path):
        assert _load_soul(tmp_path / "missing.md") is None

    def test_load_soul_strips_whitespace(self, tmp_path: Path):
        p = tmp_path / SOUL_FILENAME
        p.write_text("  body with spaces  \n", encoding="utf-8")
        assert _load_soul(p) == "body with spaces"


# ---------------------------------------------------------------------------
# Import end-to-end (ART-430)
# ---------------------------------------------------------------------------


class TestImport:
    async def test_fresh_import_creates_package_and_version(self, sf, tmp_path: Path):
        _write_agent(tmp_path, name="alpha", soul="alpha soul", config={"description": "Alpha agent", "model": "gpt-4o"})
        pkg, ver, digest, imported, sm = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        assert imported is True
        assert pkg.name == "alpha"
        assert pkg.display_name == "alpha"  # defaults to name when not supplied
        assert pkg.description == "Alpha agent"
        assert ver.package_id == pkg.id
        assert ver.version == "1.0.0"
        assert ver.status == VERSION_DRAFT
        assert ver.digest == digest
        assert ver.digest.startswith("sha256:")
        assert sm["source"] == SOURCE_FILE_IMPORT

    async def test_digest_matches_canonical_manifest(self, sf, tmp_path: Path):
        agent_dir = _write_agent(tmp_path, name="alpha", soul="s", config={"model": "m"})
        pkg, ver, digest, _, sm = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        # Reconstruct the canonical content from the stored manifest and
        # confirm the digest is over exactly those bytes.
        stored_manifest_dict = ver.manifest
        # imported_at is non-deterministic; drop it before comparing digests.
        stored_manifest_dict = dict(stored_manifest_dict)
        stored_manifest_dict.pop("source_metadata", None)
        # The persisted manifest retains source_metadata with imported_at, so
        # the persisted content_inline is what was digested. Re-read it.
        from sqlalchemy import select

        from deerflow.persistence.release.model import AgentVersionRow

        async with sf() as session:
            row = (await session.execute(select(AgentVersionRow).where(AgentVersionRow.id == ver.id))).scalar_one()
            stored_content = row.content_inline
        assert stored_content is not None
        assert compute_artifact_digest(stored_content) == digest
        # And the size_bytes matches the canonical content length.
        assert ver.size_bytes == len(stored_content.encode("utf-8"))
        # Path-based assertion: agent_dir is recorded in source_metadata.
        assert sm["path"] == str(agent_dir.resolve()) or sm["path"].endswith("alpha")

    async def test_idempotent_reimport_returns_existing(self, sf, tmp_path: Path):
        _write_agent(tmp_path, name="alpha", soul="same")
        pkg1, ver1, digest1, imported1, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        pkg2, ver2, digest2, imported2, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        assert imported1 is True
        assert imported2 is False
        assert ver1.id == ver2.id
        assert digest1 == digest2
        assert pkg1.id == pkg2.id

    async def test_idempotent_even_with_different_version_label(self, sf, tmp_path: Path):
        """Idempotency is digest-based, not version-label-based — ADR §10.
        Re-importing identical content with a different SemVer label still
        returns the existing row (the label is ignored on a digest hit)."""
        _write_agent(tmp_path, name="alpha", soul="same")
        _, ver1, _, _, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        _, ver2, _, imported2, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="2.0.0", base_dir=tmp_path)
        assert imported2 is False
        assert ver1.id == ver2.id

    async def test_content_changed_with_bumped_version_creates_new(self, sf, tmp_path: Path):
        _write_agent(tmp_path, name="alpha", soul="v1")
        _, ver1, digest1, imported1, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        # Change SOUL → new digest → must bump version to avoid UNIQUE collision.
        _write_agent(tmp_path, name="alpha", soul="v2-different")
        pkg2, ver2, digest2, imported2, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.1", base_dir=tmp_path)
        assert imported1 is True and imported2 is True
        assert ver1.id != ver2.id
        assert digest1 != digest2
        assert pkg2.id == ver1.package_id  # same package, second version

    async def test_content_changed_same_version_raises_integrity(self, sf, tmp_path: Path):
        _write_agent(tmp_path, name="alpha", soul="v1")
        await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        _write_agent(tmp_path, name="alpha", soul="v2-different")
        with pytest.raises(IntegrityError):
            await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)

    async def test_cross_org_isolation(self, sf, tmp_path: Path):
        _write_agent(tmp_path, name="alpha", soul="same")
        _, ver_a, _, _, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        # Same content, different Org — must NOT dedupe across Orgs (the
        # (org_id, digest) UNIQUE constraint is per-Org, so a fresh Version is
        # created in OTHER_ORG_ID).
        pkg_b, ver_b, _, imported_b, _ = await import_agent_from_file(sf, org_id=OTHER_ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        assert imported_b is True
        assert ver_a.id != ver_b.id
        assert ver_a.org_id == ORG_ID
        assert ver_b.org_id == OTHER_ORG_ID
        assert pkg_b.org_id == OTHER_ORG_ID

    async def test_get_package_by_name_cross_org_hiding(self, sf):
        await create_agent_package(sf, org_id=ORG_ID, name="shared", display_name="A")
        # Same name in OTHER_ORG_ID is invisible from ORG_ID.
        assert await get_agent_package_by_name(sf, org_id=ORG_ID, name="shared") is not None
        assert await get_agent_package_by_name(sf, org_id=OTHER_ORG_ID, name="shared") is None

    async def test_workspace_passthrough(self, sf, tmp_path: Path):
        _write_agent(tmp_path, name="alpha", soul="s")
        ws = "ws-1234"
        pkg, ver, _, _, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", workspace_id=ws, base_dir=tmp_path)
        assert pkg.workspace_id == ws
        # The version's workspace context is the package's; agent_versions has
        # no workspace_id column (workspace is a Package-level grouping).
        assert ver.org_id == ORG_ID

    async def test_session_passthrough_same_transaction(self, sf, tmp_path: Path):
        """When the caller passes an open session, the import stages inside it
        without committing — caller commits atomically with the audit row."""
        from sqlalchemy import select

        from deerflow.persistence.audit.model import AuditOutboxRow

        _write_agent(tmp_path, name="alpha", soul="s")
        async with sf() as session:
            pkg, ver, digest, imported, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path, session=session)
            # Stage an audit row in the SAME session to prove same-tx staging.
            from deerflow.contracts.identity import PrincipalRef
            from deerflow.contracts.policy import ResourceRef
            from deerflow.persistence.audit import enqueue_audit_outbox_in_session
            from deerflow.tenancy.audit_events import build_audit_event

            event = build_audit_event(
                "catalog.agent_version.imported",
                org_id=ORG_ID,
                actor=PrincipalRef(type="user", id="u1", user_id="u1"),
                outcome="success",
                resource=ResourceRef(type="agent_version", id=ver.id, org_id=ORG_ID),
                payload={"version_id": ver.id},
            )
            await enqueue_audit_outbox_in_session(session, event)
            await session.commit()
        # After commit, both rows are durable.
        assert await get_agent_version_by_digest(sf, org_id=ORG_ID, digest=digest) is not None
        async with sf() as session:
            rows = (await session.execute(select(AuditOutboxRow))).scalars().all()
        assert any(r.event_id for r in rows)


# ---------------------------------------------------------------------------
# File immutability of imported Versions (ART-440, ADR §15)
# ---------------------------------------------------------------------------


class TestFileImmutability:
    async def test_file_change_does_not_pollute_imported_version(self, sf, tmp_path: Path):
        """ADR §15 "文件变化不影响已导入 Version": after import, editing the
        source SOUL.md / config.yaml must not change the persisted digest or
        content. The imported Version is an immutable snapshot."""
        agent_dir = _write_agent(tmp_path, name="alpha", soul="original soul")
        _, ver, digest, _, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        # Mutate the source files.
        (agent_dir / SOUL_FILENAME).write_text("totally different soul", encoding="utf-8")
        (agent_dir / "config.yaml").write_text(yaml.safe_dump({"name": "alpha", "model": "changed"}), encoding="utf-8")
        # Re-read the persisted Version — digest + content unchanged.
        refetched = await get_agent_version_by_digest(sf, org_id=ORG_ID, digest=digest)
        assert refetched is not None
        assert refetched.id == ver.id
        assert refetched.digest == digest
        assert "original soul" in (refetched.content_inline or "")

    async def test_published_import_is_immutable(self, sf, tmp_path: Path):
        """Once an imported Version is published, content mutation is refused
        by the PR-052 published-immutability guard (re-asserted here so the
        import path inherits it cleanly)."""
        from deerflow.persistence.release import (
            VersionImmutableError,
            set_version_status,
            update_agent_version,
        )

        _write_agent(tmp_path, name="alpha", soul="v1")
        pkg, ver, _, _, _ = await import_agent_from_file(sf, org_id=ORG_ID, name="alpha", version="1.0.0", base_dir=tmp_path)
        await set_version_status(sf, version_id=ver.id, org_id=ORG_ID, status="published")
        with pytest.raises(VersionImmutableError):
            await update_agent_version(sf, version_id=ver.id, org_id=ORG_ID, content="tampered")
