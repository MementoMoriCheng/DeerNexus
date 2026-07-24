"""HTTP endpoint tests for the PR-045 audit query API (``GET /api/v1/admin/audit/events``).

Mirrors ``test_admin_console_api.py``'s mock-app pattern but drives the real
``audit_events`` table (the repository layer ``list_audit_events`` is the
data source, so a real seeded DB is needed rather than a mocked run store).
The ``@require_rbac(Permission.ADMIN_AUDIT_READ)`` decorator runs in bypass
mode (``make_rbac_test_app(bypass_authorize=True)``) so the handler logic is
exercised without seeding an IAM stack; the 403-not-admin boundary for the
``admin:audit:read`` capability is pinned in ``test_rbac_admin_routers.py``
against the real Authorize Service.

Covers ADR-0005 §12.1: Org isolation, the 7 filter dimensions, cursor
pagination + has_more, the 24h default window, the 90-day cap, malformed
cursor, and the payload-is-scrubbed guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

from app.gateway.routers import admin as admin_router
from deerflow.contracts import PrincipalRef
from deerflow.contracts.events import AuditEvent
from deerflow.contracts.policy import ResourceRef
from deerflow.persistence.audit import insert_audit_event
from deerflow.persistence.engine import get_session_factory

ORG_A = "default"
ORG_B = "00000000-0000-4000-8000-0000000000b2"
USER_ID = "00000000-0000-4000-8000-0000000000c3"
_NOW = datetime.now(UTC)


def _event(
    *,
    event_id: str,
    org_id: str | None = ORG_A,
    action: str = "iam.role_binding.created",
    outcome: str = "success",
    occurred_at: datetime = _NOW,
    actor_id: str = USER_ID,
    resource: ResourceRef | None = None,
    run_id: str | None = None,
    request_id: str = "req-1",
    payload: dict | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        idempotency_key=f"idem-{event_id}",
        org_id=org_id,
        actor=PrincipalRef(type="user", id=actor_id, user_id=actor_id, display_name="alice"),
        action=action,
        resource=resource,
        outcome=outcome,  # type: ignore[arg-type]
        request_id=request_id,
        occurred_at=occurred_at,
        run_id=run_id,
        payload=payload if payload is not None else {"role_id": "r-admin"},
    )


async def _seed(sf, event: AuditEvent) -> None:
    await insert_audit_event(sf, event)


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'auditq.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


def _make_app(sf):
    """Build a test app in bypass-authorize mode bound to the seeded factory."""
    app = make_rbac_test_app(bypass_authorize=True)
    app.include_router(admin_router.router)
    return app


# The autouse ``_auto_user_context`` fixture (conftest.py) binds a TenantContext
# for ``org_id="default"`` (== ORG_A here). The router reads org_id off the
# bound context, so no per-test binding is needed; cross-Org isolation is
# verified by seeding a second Org (ORG_B) and asserting its events never
# surface to the ``default``-bound caller.


class TestOrgIsolation:
    @pytest.mark.anyio
    async def test_org_a_query_does_not_return_org_b(self, sf):
        """§12.1 + §8: an Org-A caller never sees Org-B events."""
        # Use an explicit wide window so the assertion is about Org isolation,
        # not the 24h default-window boundary (tested separately).
        await _seed(sf, _event(event_id="a-1", org_id=ORG_A, occurred_at=_NOW - timedelta(hours=1)))
        await _seed(sf, _event(event_id="b-1", org_id=ORG_B, occurred_at=_NOW - timedelta(hours=1)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/admin/audit/events",
                params={"occurred_after": (_NOW - timedelta(days=2)).isoformat(), "occurred_before": _NOW.isoformat()},
            )
        assert resp.status_code == 200, resp.text
        ids = {e["event_id"] for e in resp.json()["data"]}
        assert ids == {"a-1"}


class TestFilters:
    @pytest.mark.anyio
    async def test_action_filter(self, sf):
        await _seed(sf, _event(event_id="e1", action="iam.role_binding.created", occurred_at=_NOW - timedelta(minutes=10)))
        await _seed(sf, _event(event_id="e2", action="iam.api_key.created", occurred_at=_NOW - timedelta(minutes=9)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events", params={"action": "iam.api_key.created"})
        assert resp.status_code == 200
        ids = {e["event_id"] for e in resp.json()["data"]}
        assert ids == {"e2"}

    @pytest.mark.anyio
    async def test_outcome_filter(self, sf):
        await _seed(sf, _event(event_id="ok", outcome="success", occurred_at=_NOW - timedelta(minutes=10)))
        await _seed(sf, _event(event_id="no", outcome="denied", occurred_at=_NOW - timedelta(minutes=9)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events", params={"outcome": "denied"})
        assert resp.status_code == 200
        assert {e["event_id"] for e in resp.json()["data"]} == {"no"}

    @pytest.mark.anyio
    async def test_actor_filter(self, sf):
        await _seed(sf, _event(event_id="e1", actor_id="u-1", occurred_at=_NOW - timedelta(minutes=10)))
        await _seed(sf, _event(event_id="e2", actor_id="u-2", occurred_at=_NOW - timedelta(minutes=9)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events", params={"actor_id": "u-2"})
        assert resp.status_code == 200
        assert {e["event_id"] for e in resp.json()["data"]} == {"e2"}

    @pytest.mark.anyio
    async def test_resource_type_and_id_filter(self, sf):
        res = ResourceRef(type="service_account", id="sa-1", org_id=ORG_A)
        await _seed(sf, _event(event_id="e1", resource=res, occurred_at=_NOW - timedelta(minutes=10)))
        await _seed(sf, _event(event_id="e2", occurred_at=_NOW - timedelta(minutes=9)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events", params={"resource_type": "service_account", "resource_id": "sa-1"})
        assert resp.status_code == 200
        assert {e["event_id"] for e in resp.json()["data"]} == {"e1"}
        # The resource is re-nested in the response.
        assert resp.json()["data"][0]["resource"]["type"] == "service_account"

    @pytest.mark.anyio
    async def test_run_id_and_request_id_filter(self, sf):
        await _seed(sf, _event(event_id="e1", run_id="run-1", request_id="req-x", occurred_at=_NOW - timedelta(minutes=10)))
        await _seed(sf, _event(event_id="e2", run_id="run-2", request_id="req-y", occurred_at=_NOW - timedelta(minutes=9)))
        app = _make_app(sf)
        with TestClient(app) as client:
            by_run = client.get("/api/v1/admin/audit/events", params={"run_id": "run-1"})
            by_req = client.get("/api/v1/admin/audit/events", params={"request_id": "req-y"})
        assert {e["event_id"] for e in by_run.json()["data"]} == {"e1"}
        assert {e["event_id"] for e in by_req.json()["data"]} == {"e2"}


class TestPagination:
    @pytest.mark.anyio
    async def test_cursor_pagination_walks_all_pages(self, sf):
        # Seed 5 events at distinct timestamps so the (occurred_at, event_id)
        # cursor order is stable.
        base = _NOW - timedelta(hours=2)
        for i in range(5):
            await _seed(sf, _event(event_id=f"p{i}", occurred_at=base + timedelta(minutes=i)))
        app = _make_app(sf)
        seen: list[str] = []
        cursor = None
        with TestClient(app) as client:
            for _ in range(10):  # safety bound
                params: dict = {"limit": 2}
                if cursor:
                    params["cursor"] = cursor
                resp = client.get("/api/v1/admin/audit/events", params=params)
                assert resp.status_code == 200, resp.text
                body = resp.json()
                seen.extend(e["event_id"] for e in body["data"])
                cursor = body.get("next_cursor")
                if not body["has_more"]:
                    break
        assert seen == ["p0", "p1", "p2", "p3", "p4"]  # no duplicates, full coverage

    @pytest.mark.anyio
    async def test_has_more_false_on_last_page(self, sf):
        await _seed(sf, _event(event_id="only", occurred_at=_NOW - timedelta(minutes=5)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events", params={"limit": 100})
        body = resp.json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None


class TestTimeWindow:
    @pytest.mark.anyio
    async def test_default_window_is_24h(self, sf):
        """Without explicit bounds the window defaults to the trailing 24h."""
        await _seed(sf, _event(event_id="recent", occurred_at=_NOW - timedelta(hours=1)))
        await _seed(sf, _event(event_id="old", occurred_at=_NOW - timedelta(hours=48)))
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events")
        assert resp.status_code == 200
        ids = {e["event_id"] for e in resp.json()["data"]}
        assert ids == {"recent"}  # the 48h-old event is outside the 24h default

    @pytest.mark.anyio
    async def test_window_over_90_days_rejected(self, sf):
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get(
                "/api/v1/admin/audit/events",
                params={
                    "occurred_after": (_NOW - timedelta(days=100)).isoformat(),
                    "occurred_before": _NOW.isoformat(),
                },
            )
        assert resp.status_code == 400
        assert "90-day" in resp.text

    @pytest.mark.anyio
    async def test_malformed_cursor_rejected(self, sf):
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events", params={"cursor": "not-a-valid-cursor"})
        assert resp.status_code == 400
        assert "cursor" in resp.text.lower()


class TestResponseShape:
    @pytest.mark.anyio
    async def test_response_projects_actor_and_resource(self, sf):
        res = ResourceRef(type="api_key", id="key-1", org_id=ORG_A)
        await _seed(
            sf,
            _event(
                event_id="proj",
                actor_id="u-9",
                resource=res,
                occurred_at=_NOW - timedelta(minutes=5),
                payload={"key_id": "key-1"},  # already-scrubbed form at write time
            ),
        )
        app = _make_app(sf)
        with TestClient(app) as client:
            resp = client.get("/api/v1/admin/audit/events")
        assert resp.status_code == 200
        ev = resp.json()["data"][0]
        assert ev["actor"]["id"] == "u-9"
        assert ev["actor"]["type"] == "user"
        assert ev["resource"]["type"] == "api_key"
        assert ev["resource"]["id"] == "key-1"
        assert ev["org_id"] == ORG_A
        # The payload is returned as-written (scrubbing happened at insert).
        assert ev["payload"] == {"key_id": "key-1"}
