"""Catalog / Release audit tests (PR-043).

Covers ADR-0005 §5.1 minimal-set events whose code paths landed in this PR:

* ``catalog.skill.changed`` — ``routers/skills.py`` 5 write endpoints
  (install / edit / delete / rollback / enable-disable toggle);
* ``catalog.mcp.changed`` — ``routers/mcp.py`` ``PUT /mcp/config``.

Both persist to ``extensions_config.json`` (file IO, not a DB transaction),
so they CANNOT satisfy §7.1 Class A same-transaction coupling. They are
emitted best-effort via ``emit_class_b_audit`` (durable pending row, never
raises) AFTER the file write succeeds — see ADR §7.2 and the helper docstrings.

These tests assert the outbox row lands with the right action + verb payload +
actor + org_id (the audit contract), not the file-write business logic (which
is covered by ``test_client.py``). They exercise the router helpers directly
with a constructed ``Request`` + bound TenantContext + a registered sink, which
is the same surface ``emit_class_b_audit`` touches in production.

Fixture conventions mirror ``test_audit_class_b.py``: isolated SQLite via
``init_engine`` + ``reset_audit_sink_for_testing``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.contracts.context import TenantContext, bind_tenant_context
from deerflow.contracts.events import AuditEvent
from deerflow.contracts.identity import PrincipalRef
from deerflow.persistence.audit.model import AuditOutboxRow


async def _outbox_events(sf, *, action: str) -> list[AuditEvent]:
    async with sf() as session:
        rows = (await session.execute(select(AuditOutboxRow))).scalars().all()
    parsed = [AuditEvent.model_validate_json(r.payload_json) for r in rows]
    return [e for e in parsed if e.action == action]


@contextmanager
def _bound_tenant(org_id: str):
    """Bind a TenantContext for the duration of the block (test-scoped)."""
    from deerflow.contracts.context import reset_tenant_context

    token = bind_tenant_context(
        TenantContext(
            org_id=org_id,
            principal=PrincipalRef(type="user", id="test-user", user_id="test-user"),
            auth_method="session",
            request_id="req-pr043",
            issued_at=datetime.now(UTC),
        )
    )
    try:
        yield
    finally:
        reset_tenant_context(token)


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    from app.gateway.audit_sink import reset_audit_sink_for_testing

    reset_audit_sink_for_testing()
    try:
        yield get_session_factory()
    finally:
        reset_audit_sink_for_testing()
        await close_engine()


def _make_request(user_id: str | None) -> SimpleNamespace:
    """Build a minimal request stand-in with ``state.user`` populated.

    The skill/mcp audit helpers read only ``request.state.user`` (the
    AuthMiddleware stamp); a ``SimpleNamespace`` with ``state`` mirrors that.
    """
    state = SimpleNamespace()
    if user_id is not None:
        state.user = SimpleNamespace(id=uuid.UUID(user_id) if "-" in user_id else user_id)
    else:
        state.user = None
    return SimpleNamespace(state=state)


ORG_ID = "org-pr043"
USER_ID = "12345678-1234-5678-1234-567812345678"


# ===========================================================================
# catalog.mcp.changed — PUT /mcp/config
# ===========================================================================


class TestMcpConfigAudit:
    @pytest.mark.anyio
    async def test_mcp_config_change_emits_catalog_mcp_changed(self, sf):
        """A successful MCP config rewrite emits ``catalog.mcp.changed`` with
        the server count + names (never server config bodies — §3.3 / §5.3)."""
        from app.gateway.routers.mcp import _emit_mcp_changed_audit

        with _bound_tenant(ORG_ID):
            await _emit_mcp_changed_audit(
                _make_request(USER_ID),
                server_names=["github", "filesystem"],
            )

        events = await _outbox_events(sf, action="catalog.mcp.changed")
        assert len(events) == 1
        ev = events[0]
        assert ev.org_id == ORG_ID
        assert ev.actor.id == USER_ID
        assert ev.actor.type == "user"
        assert ev.outcome == "success"
        assert ev.resource is not None
        assert ev.resource.type == "mcp_config"
        assert ev.payload["server_count"] == 2
        assert ev.payload["server_names"] == ["github", "filesystem"]

    @pytest.mark.anyio
    @pytest.mark.no_auto_user
    async def test_mcp_config_change_skipped_when_no_tenant(self, sf):
        """No tenant context bound → emit is skipped (graceful, not raised)."""
        from app.gateway.routers.mcp import _emit_mcp_changed_audit

        # No bind_tenant_context — get_tenant_context() returns None.
        await _emit_mcp_changed_audit(_make_request(USER_ID), server_names=["x"])
        events = await _outbox_events(sf, action="catalog.mcp.changed")
        assert events == []

    @pytest.mark.anyio
    async def test_mcp_config_payload_excludes_secret_material(self, sf):
        """The payload records only names, not server config (which may carry
        ``$TOKEN`` secret references). A body/secret key must never appear."""
        from app.gateway.routers.mcp import _emit_mcp_changed_audit

        with _bound_tenant(ORG_ID):
            await _emit_mcp_changed_audit(_make_request(USER_ID), server_names=["s1"])

        ev = (await _outbox_events(sf, action="catalog.mcp.changed"))[0]
        assert set(ev.payload.keys()) == {"server_count", "server_names"}
        for forbidden in ("command", "env", "headers", "oauth", "client_secret", "token"):
            assert forbidden not in ev.payload


# ===========================================================================
# catalog.skill.changed — 5 write endpoints (verb matrix)
# ===========================================================================


@pytest.mark.parametrize(
    "verb",
    ["installed", "edited", "deleted", "rolled_back", "enabled", "disabled"],
)
class TestSkillChangeAudit:
    @pytest.mark.anyio
    async def test_skill_change_emits_correct_verb(self, sf, verb):
        """Each skill write emits ``catalog.skill.changed`` with the right verb
        in the payload (single action, verb distinguishes the 5 endpoints)."""
        from app.gateway.routers.skills import _emit_skill_changed_audit

        with _bound_tenant(ORG_ID):
            await _emit_skill_changed_audit(_make_request(USER_ID), skill_name="my-skill", verb=verb)

        events = await _outbox_events(sf, action="catalog.skill.changed")
        assert len(events) == 1
        ev = events[0]
        assert ev.org_id == ORG_ID
        assert ev.actor.id == USER_ID
        assert ev.outcome == "success"
        assert ev.resource is not None
        assert ev.resource.type == "skill"
        assert ev.resource.id == "my-skill"
        assert ev.payload["skill_name"] == "my-skill"
        assert ev.payload["verb"] == verb

    @pytest.mark.anyio
    async def test_skill_change_payload_excludes_skill_content(self, sf, verb):
        """The payload never carries SKILL.md content (which may hold sensitive
        prompts/instructions) — only skill_name + verb."""
        from app.gateway.routers.skills import _emit_skill_changed_audit

        with _bound_tenant(ORG_ID):
            await _emit_skill_changed_audit(_make_request(USER_ID), skill_name="s", verb=verb)

        ev = (await _outbox_events(sf, action="catalog.skill.changed"))[0]
        assert set(ev.payload.keys()) == {"skill_name", "verb"}


class TestSkillChangeAuditEdgeCases:
    @pytest.mark.anyio
    @pytest.mark.no_auto_user
    async def test_skill_change_skipped_when_no_tenant(self, sf):
        """No tenant context bound → emit is skipped (graceful, not raised)."""
        from app.gateway.routers.skills import _emit_skill_changed_audit

        # No bind_tenant_context.
        await _emit_skill_changed_audit(_make_request(USER_ID), skill_name="x", verb="edited")
        assert await _outbox_events(sf, action="catalog.skill.changed") == []

    @pytest.mark.anyio
    async def test_skill_change_actor_falls_back_to_system_when_no_user(self, sf):
        """An unauthenticated-style request (no ``request.state.user``) records
        a system actor rather than skipping — the audit still lands."""
        from app.gateway.routers.skills import _emit_skill_changed_audit

        with _bound_tenant(ORG_ID):
            await _emit_skill_changed_audit(_make_request(user_id=None), skill_name="x", verb="edited")

        ev = (await _outbox_events(sf, action="catalog.skill.changed"))[0]
        assert ev.actor.type == "system"
        assert ev.actor.id == "system"
        # system principal must NOT carry a user_id (PrincipalRef validator).
        assert ev.actor.user_id is None
