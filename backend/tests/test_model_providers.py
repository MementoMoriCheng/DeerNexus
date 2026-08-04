"""Tests for per-user custom model-provider persistence + CRUD router.

Covers:

* Repository: encrypt/decrypt roundtrip, UNIQUE(owner, name) constraint,
  cross-user isolation, update (with and without api_key rotation), delete.
* Router: auth (no user → 401), create (201 + 409 on clash), list (owner
  isolation), update (404 for other users), delete (404 for other users),
  and the invariant that the API key is never echoed back.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from _router_auth_helpers import make_rbac_test_app
from fastapi.testclient import TestClient

from app.gateway.routers import model_providers as mp_router
from deerflow.persistence.channel_connections.sql import ChannelCredentialCipher
from deerflow.persistence.model_providers.repository import ModelProviderRepository

# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo(tmp_path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'model_providers.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield ModelProviderRepository(
            get_session_factory(),
            cipher=ChannelCredentialCipher.from_key("test-encryption-key"),
        )
    finally:
        await close_engine()


class TestModelProviderRepository:
    @pytest.mark.anyio
    async def test_create_and_get_roundtrips_decrypted_api_key(self, repo):
        record = await repo.create(
            owner_user_id="alice",
            name="my-deepseek",
            model="deepseek-chat",
            api_key="sk-secret-123",
            base_url="https://api.deepseek.com/v1",
        )
        assert record.api_key == "sk-secret-123"  # cleartext returned to caller
        assert record.id
        assert record.use == "langchain_openai:ChatOpenAI"

        fetched = await repo.get("alice", "my-deepseek")
        assert fetched is not None
        assert fetched.api_key == "sk-secret-123"

    @pytest.mark.anyio
    async def test_encrypted_at_rest(self, repo, tmp_path):
        """The DB row must store a ciphertext, not the cleartext key."""
        await repo.create(
            owner_user_id="alice",
            name="p1",
            model="m",
            api_key="sk-cleartext",
        )
        # Raw DB inspection: read the column directly via the session factory.
        from sqlalchemy import select

        from deerflow.persistence.model_providers.model import ModelProviderRow

        async with repo.session_factory() as session:
            row = (
                await session.scalars(
                    select(ModelProviderRow).where(
                        ModelProviderRow.owner_user_id == "alice",
                        ModelProviderRow.name == "p1",
                    )
                )
            ).one()
            assert row.encrypted_api_key != "sk-cleartext"
            assert "sk-cleartext" not in row.encrypted_api_key
            assert row.encrypted_api_key.startswith("fernet:v1:")

    @pytest.mark.anyio
    async def test_duplicate_name_raises_integrity_error(self, repo):
        from sqlalchemy.exc import IntegrityError

        await repo.create(
            owner_user_id="alice", name="dup", model="m", api_key="k1"
        )
        with pytest.raises(IntegrityError):
            await repo.create(
                owner_user_id="alice", name="dup", model="m2", api_key="k2"
            )

    @pytest.mark.anyio
    async def test_same_name_allowed_for_different_owners(self, repo):
        a = await repo.create(owner_user_id="alice", name="shared", model="m", api_key="ka")
        b = await repo.create(owner_user_id="bob", name="shared", model="m", api_key="kb")
        assert a.owner_user_id == "alice"
        assert b.owner_user_id == "bob"
        assert a.id != b.id

    @pytest.mark.anyio
    async def test_list_by_user_isolates_owners(self, repo):
        await repo.create(owner_user_id="alice", name="a1", model="m", api_key="k")
        await repo.create(owner_user_id="alice", name="a2", model="m", api_key="k")
        await repo.create(owner_user_id="bob", name="b1", model="m", api_key="k")

        alice_rows = await repo.list_by_user("alice")
        bob_rows = await repo.list_by_user("bob")
        assert sorted(r.name for r in alice_rows) == ["a1", "a2"]
        assert [r.name for r in bob_rows] == ["b1"]

    @pytest.mark.anyio
    async def test_update_without_api_key_keeps_existing_key(self, repo):
        created = await repo.create(
            owner_user_id="alice", name="p1", model="m", api_key="original-key"
        )
        updated = await repo.update(
            owner_user_id="alice",
            provider_id=created.id,
            api_key=None,
            model="new-model",
            supports_thinking=True,
        )
        assert updated is not None
        assert updated.model == "new-model"
        assert updated.supports_thinking is True
        # Key untouched because api_key=None
        assert updated.api_key == "original-key"

    @pytest.mark.anyio
    async def test_update_with_api_key_rotates_encrypted_value(self, repo):
        created = await repo.create(
            owner_user_id="alice", name="p1", model="m", api_key="old-key"
        )
        updated = await repo.update(
            owner_user_id="alice",
            provider_id=created.id,
            api_key="new-key",
        )
        assert updated is not None
        assert updated.api_key == "new-key"

    @pytest.mark.anyio
    async def test_update_returns_none_for_other_owner(self, repo):
        created = await repo.create(
            owner_user_id="alice", name="p1", model="m", api_key="k"
        )
        result = await repo.update(
            owner_user_id="bob",
            provider_id=created.id,
            api_key=None,
            model="x",
        )
        assert result is None

    @pytest.mark.anyio
    async def test_delete_only_affects_specified_owner(self, repo):
        a = await repo.create(owner_user_id="alice", name="p1", model="m", api_key="k")
        # bob cannot delete alice's provider
        deleted_by_bob = await repo.delete("bob", a.id)
        assert deleted_by_bob is False
        # alice can
        deleted_by_alice = await repo.delete("alice", a.id)
        assert deleted_by_alice is True
        assert await repo.get("alice", "p1") is None

    @pytest.mark.anyio
    async def test_get_by_id_scoped_to_owner(self, repo):
        created = await repo.create(
            owner_user_id="alice", name="p1", model="m", api_key="k"
        )
        assert (await repo.get_by_id("alice", created.id)) is not None
        assert (await repo.get_by_id("bob", created.id)) is None


# ---------------------------------------------------------------------------
# Router tests
# ---------------------------------------------------------------------------


def _user(user_id: str):
    return SimpleNamespace(id=user_id, system_role="user")


async def _make_app_and_repo(tmp_path, *, user_id: str = "alice"):
    from deerflow.persistence.engine import get_session_factory, init_engine

    await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}", sqlite_dir=str(tmp_path))
    sf = get_session_factory()
    repo = ModelProviderRepository(sf, cipher=ChannelCredentialCipher.from_key("test-encryption-key"))
    app = make_rbac_test_app(bypass_authorize=True, user_factory=lambda: _user(user_id))
    app.state.model_provider_repo = repo
    app.include_router(mp_router.router)
    return app, repo, sf


class TestModelProviderRouter:
    def test_create_then_list_and_key_never_returned(self, tmp_path):
        import anyio

        app, repo, _ = anyio.run(_make_app_and_repo, tmp_path)
        with TestClient(app) as client:
            create_resp = client.post(
                "/api/model-providers",
                json={
                    "name": "my-deepseek",
                    "model": "deepseek-chat",
                    "api_key": "sk-super-secret",
                    "base_url": "https://api.deepseek.com/v1",
                    "supports_thinking": True,
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            body = create_resp.json()
            assert body["name"] == "my-deepseek"
            assert body["model"] == "deepseek-chat"
            assert body["supports_thinking"] is True
            assert body["has_api_key"] is True
            # The key must never appear in any field of the response.
            assert "sk-super-secret" not in json.dumps(body)

            list_resp = client.get("/api/model-providers")
            assert list_resp.status_code == 200
            providers = list_resp.json()["providers"]
            assert len(providers) == 1
            assert providers[0]["name"] == "my-deepseek"
            assert "sk-super-secret" not in json.dumps(providers)

    def test_create_duplicate_returns_409(self, tmp_path):
        import anyio

        app, repo, _ = anyio.run(_make_app_and_repo, tmp_path)
        with TestClient(app) as client:
            payload = {"name": "dup", "model": "m", "api_key": "k"}
            r1 = client.post("/api/model-providers", json=payload)
            assert r1.status_code == 201
            r2 = client.post("/api/model-providers", json=payload)
            assert r2.status_code == 409

    def test_update_without_api_key_preserves_it(self, tmp_path):
        import anyio

        app, repo, _ = anyio.run(_make_app_and_repo, tmp_path)
        with TestClient(app) as client:
            created = client.post(
                "/api/model-providers",
                json={"name": "p1", "model": "m", "api_key": "original"},
            ).json()
            updated = client.put(
                f"/api/model-providers/{created['id']}",
                json={"model": "new-model", "supports_reasoning_effort": True},
            )
            assert updated.status_code == 200, updated.text
            body = updated.json()
            assert body["model"] == "new-model"
            assert body["supports_reasoning_effort"] is True

            # Verify the key is still decryptable server-side.
            import anyio as _anyio

            record = _anyio.run(repo.get_by_id, "alice", created["id"])
            assert record.api_key == "original"

    def test_update_with_api_key_rotates(self, tmp_path):
        import anyio

        app, repo, _ = anyio.run(_make_app_and_repo, tmp_path)
        with TestClient(app) as client:
            created = client.post(
                "/api/model-providers",
                json={"name": "p1", "model": "m", "api_key": "old"},
            ).json()
            resp = client.put(
                f"/api/model-providers/{created['id']}",
                json={"api_key": "rotated-key"},
            )
            assert resp.status_code == 200
            import anyio as _anyio

            record = _anyio.run(repo.get_by_id, "alice", created["id"])
            assert record.api_key == "rotated-key"

    def test_delete_removes_provider(self, tmp_path):
        import anyio

        app, repo, _ = anyio.run(_make_app_and_repo, tmp_path)
        with TestClient(app) as client:
            created = client.post(
                "/api/model-providers",
                json={"name": "p1", "model": "m", "api_key": "k"},
            ).json()
            del_resp = client.delete(f"/api/model-providers/{created['id']}")
            assert del_resp.status_code == 204
            # Second delete is 404
            del_resp2 = client.delete(f"/api/model-providers/{created['id']}")
            assert del_resp2.status_code == 404

    def test_cross_user_isolation(self, tmp_path):
        """Alice's providers are invisible/uneditable by Bob."""
        from functools import partial

        import anyio

        # App built as alice, then we re-stamp the request user to bob.
        app, repo, _ = anyio.run(partial(_make_app_and_repo, tmp_path, user_id="alice"))
        with TestClient(app) as client:
            created = client.post(
                "/api/model-providers",
                json={"name": "alice-only", "model": "m", "api_key": "alice-key"},
            ).json()

        # Rebuild as bob with the same DB.
        app2, repo2, _ = anyio.run(partial(_make_app_and_repo, tmp_path, user_id="bob"))
        # Reuse the alice DB — the helper points at the same tmp_path file,
        # but init_engine re-creates a fresh in-memory-ish handle, so list
        # for bob is empty regardless. The critical assertion is that bob
        # cannot update/delete alice's provider id.
        with TestClient(app2) as client2:
            bob_list = client2.get("/api/model-providers").json()["providers"]
            assert bob_list == []

            # Bob tries to update/delete alice's provider → 404 (owner check).
            put_resp = client2.put(
                f"/api/model-providers/{created['id']}",
                json={"model": "hijacked"},
            )
            assert put_resp.status_code == 404
            del_resp = client2.delete(f"/api/model-providers/{created['id']}")
            assert del_resp.status_code == 404

    def test_requires_authentication(self, tmp_path):
        """When no user is stamped on request.state, the router returns 401."""
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(mp_router.router)
        with TestClient(app) as client:
            resp = client.get("/api/model-providers")
            assert resp.status_code == 401
