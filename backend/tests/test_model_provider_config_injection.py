"""Tests for per-user model-provider config injection (PR-B).

Verifies the ``ModelProviderConfigMiddleware``:

* merges a user's private providers into the request-scoped ``AppConfig`` so
  ``/api/models`` (and by extension the LLM call chain) see them;
* keeps each user's providers isolated (user A's providers never appear for
  user B);
* lets a user provider whose name matches a base model override it;
* is fail-open: DB errors / missing persistence fall back to the base config;
* pops the override after the request so it doesn't leak across requests.

These are integration tests that build a minimal FastAPI app with both a stub
auth middleware (stamps ``request.state.user``) and the
``ModelProviderConfigMiddleware`` in the correct add-order, then drive it with
``TestClient``.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from app.gateway.deps import get_config
from app.gateway.model_provider_middleware import (
    ModelProviderConfigMiddleware,
    merge_user_models,
)
from deerflow.config.app_config import peek_current_app_config
from deerflow.config.model_config import ModelConfig
from deerflow.persistence.channel_connections.sql import ChannelCredentialCipher
from deerflow.persistence.model_providers.repository import ModelProviderRepository

# ---------------------------------------------------------------------------
# Pure unit tests for the merge helper
# ---------------------------------------------------------------------------


class TestMergeUserModels:
    def test_appends_non_colliding_user_models(self):
        base = [
            ModelConfig(name="gpt-4", use="langchain_openai:ChatOpenAI", model="gpt-4"),
        ]
        user = [
            ModelConfig(name="my-deepseek", use="langchain_openai:ChatOpenAI", model="deepseek-chat"),
        ]
        merged = merge_user_models(base, user)
        assert [m.name for m in merged] == ["gpt-4", "my-deepseek"]

    def test_user_overrides_base_on_name_clash(self):
        base = [
            ModelConfig(name="gpt-4", use="langchain_openai:ChatOpenAI", model="gpt-4"),
        ]
        user = [
            ModelConfig(name="gpt-4", use="langchain_openai:ChatOpenAI", model="gpt-4o"),
        ]
        merged = merge_user_models(base, user)
        assert len(merged) == 1
        assert merged[0].model == "gpt-4o"

    def test_preserves_base_order(self):
        base = [
            ModelConfig(name="a", use="langchain_openai:ChatOpenAI", model="a"),
            ModelConfig(name="b", use="langchain_openai:ChatOpenAI", model="b"),
            ModelConfig(name="c", use="langchain_openai:ChatOpenAI", model="c"),
        ]
        user = [
            ModelConfig(name="z", use="langchain_openai:ChatOpenAI", model="z"),
            ModelConfig(name="b", use="langchain_openai:ChatOpenAI", model="b2"),
        ]
        merged = merge_user_models(base, user)
        assert [m.name for m in merged] == ["a", "b", "c", "z"]
        assert next(m for m in merged if m.name == "b").model == "b2"

    def test_empty_user_list_returns_base_copy(self):
        base = [ModelConfig(name="a", use="langchain_openai:ChatOpenAI", model="a")]
        merged = merge_user_models(base, [])
        assert [m.name for m in merged] == ["a"]
        assert merged is not base  # new list, not the same reference


# ---------------------------------------------------------------------------
# Integration tests with a stub auth middleware + the real config middleware
# ---------------------------------------------------------------------------


class _StubAuthMiddleware(BaseHTTPMiddleware):
    """Stamp a fake user onto request.state, mirroring production AuthMiddleware."""

    def __init__(self, app, *, user_factory: Callable[[], Any]) -> None:
        super().__init__(app)
        self._user_factory = user_factory

    async def dispatch(self, request: StarletteRequest, call_next):
        request.state.user = self._user_factory()
        return await call_next(request)


def _build_app(
    *,
    repo: ModelProviderRepository,
    user_factory: Callable[[], Any],
) -> FastAPI:
    """Construct a test app with both middlewares in the correct order.

    Add-order matters: ``ModelProviderConfigMiddleware`` is added FIRST so it
    runs LAST (innermost, inside the stub auth's call_next where
    ``request.state.user`` is populated). ``_StubAuthMiddleware`` is added
    second → runs first → sets the user.
    """
    app = FastAPI()
    app.add_middleware(ModelProviderConfigMiddleware)
    app.add_middleware(_StubAuthMiddleware, user_factory=user_factory)
    app.state.model_provider_repo = repo

    # Expose the merged config via a debug endpoint.
    @app.get("/debug/models")
    async def _debug_models(config=Depends(get_config)):
        # Also surface whether the ContextVar override is active.
        override = peek_current_app_config()
        return JSONResponse(
            {
                "models": [m.name for m in config.models],
                "override_active": override is not None,
            }
        )

    return app


async def _make_repo(tmp_path) -> ModelProviderRepository:
    """Boot an isolated sqlite engine + repo (run via anyio.run)."""
    from deerflow.persistence.engine import get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'injection.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    return ModelProviderRepository(
        get_session_factory(),
        cipher=ChannelCredentialCipher.from_key("test-encryption-key"),
    )


def _user(uid: str):
    return SimpleNamespace(id=uid, system_role="user")


class TestModelProviderConfigMiddleware:
    def test_user_provider_appears_in_config(self, tmp_path):
        import anyio

        async def _setup():
            repo = await _make_repo(tmp_path)
            await repo.create(
                owner_user_id="alice",
                name="my-deepseek",
                model="deepseek-chat",
                api_key="sk-secret",
                base_url="https://api.deepseek.com/v1",
            )
            return repo

        repo = anyio.run(_setup)
        app = _build_app(repo=repo, user_factory=lambda: _user("alice"))
        with TestClient(app) as client:
            resp = client.get("/debug/models")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "my-deepseek" in body["models"]
            assert body["override_active"] is True

    def test_cross_user_isolation(self, tmp_path):
        import anyio

        async def _setup():
            repo = await _make_repo(tmp_path)
            await repo.create(
                owner_user_id="alice",
                name="alice-private",
                model="alice-model",
                api_key="sk-a",
            )
            return repo

        repo = anyio.run(_setup)
        # Bob has no providers.
        app = _build_app(repo=repo, user_factory=lambda: _user("bob"))
        with TestClient(app) as client:
            resp = client.get("/debug/models")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "alice-private" not in body["models"]
            assert body["override_active"] is False

    def test_user_provider_overrides_base_model_on_name_clash(self, tmp_path):
        import anyio

        async def _setup():
            repo = await _make_repo(tmp_path)
            await repo.create(
                owner_user_id="alice",
                # Name matches a base config.yaml model to test override.
                name="gpt-4o",
                model="my-custom-gpt4o",
                api_key="sk-x",
            )
            return repo

        repo = anyio.run(_setup)
        app = _build_app(repo=repo, user_factory=lambda: _user("alice"))
        with TestClient(app) as client:
            resp = client.get("/debug/models")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "gpt-4o" in body["models"]

    def test_override_does_not_leak_across_requests(self, tmp_path):
        import anyio

        async def _setup():
            repo = await _make_repo(tmp_path)
            await repo.create(
                owner_user_id="alice",
                name="alice-only",
                model="m",
                api_key="sk-a",
            )
            return repo

        repo = anyio.run(_setup)
        current_user = {"id": "alice"}

        def factory():
            return _user(current_user["id"])

        app = _build_app(repo=repo, user_factory=factory)
        with TestClient(app) as client:
            # Alice sees her provider.
            r1 = client.get("/debug/models").json()
            assert "alice-only" in r1["models"]
            assert r1["override_active"] is True
            # Switch to Bob mid-session — override must not leak.
            current_user["id"] = "bob"
            r2 = client.get("/debug/models").json()
            assert "alice-only" not in r2["models"]
            assert r2["override_active"] is False

    def test_fail_open_when_repo_raises(self, tmp_path, monkeypatch):
        """A DB error during list_by_user must not 500 — base config is used."""
        import anyio

        repo = anyio.run(_make_repo, tmp_path)

        async def _boom(_self, _user_id):  # noqa: ANN001
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr(ModelProviderRepository, "list_by_user", _boom)
        app = _build_app(repo=repo, user_factory=lambda: _user("alice"))
        with TestClient(app) as client:
            resp = client.get("/debug/models")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["override_active"] is False  # fell back to base

    def test_anonymous_request_passes_through(self, tmp_path):
        """No request.state.user → no override, base config served."""
        import anyio

        repo = anyio.run(_make_repo, tmp_path)

        def factory():
            # Simulate an unauthenticated (anonymous) request path.
            return None

        # The stub sets user=None; the middleware's getattr returns None.
        app = _build_app(repo=repo, user_factory=factory)
        with TestClient(app) as client:
            resp = client.get("/debug/models")
            assert resp.status_code == 200, resp.text
            assert resp.json()["override_active"] is False
