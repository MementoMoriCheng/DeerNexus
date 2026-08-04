"""Per-request model-provider config injection (PR-B).

Merges a user's private model providers (stored encrypted in the
``model_providers`` table, PR-A) into a request-scoped ``AppConfig`` override
so that both ``/api/models`` (via ``get_config`` → ``get_app_config``) and the
LLM call chain (via ``ctx.app_config`` → ``create_chat_model``) transparently
see the user's custom providers — without changing any route or agent
signature.

Mechanism
---------
Registered after ``AuthMiddleware`` (so ``request.state.user`` is populated),
the middleware:

1. Reads ``request.state.user.id``. Anonymous / unauthenticated requests and
   requests without model-provider persistence are passed through unchanged.
2. Looks up the user's providers via ``ModelProviderRepository.list_by_user``.
   A short-lived (30s) in-process cache keyed by user id avoids hitting the DB
   on every request; the cache is best-effort and safe to stale-out.
3. Builds a merged ``AppConfig`` — a shallow ``model_copy`` of the base config
   whose ``models`` list is the base models extended with one ``ModelConfig``
   per provider. User providers whose ``name`` collides with a base model
   override it (last-write-wins); non-colliding names are appended.
4. ``push_current_app_config(merged)`` for the duration of the request and
   ``pop_current_app_config()`` in ``finally``.

The decrypted API key reaches ``ModelConfig`` via the ``extra="allow"`` field
set (``api_key`` / ``api_base``), exactly as a YAML-configured model would.
The cleartext never leaves this server-side merge.

Failure mode
------------
The middleware is fail-open for observability/ease: if the lookup or merge
raises, the base config is used (the request still succeeds, just without the
user's private providers). This matches the project's existing pattern where
config enrichment is never a correctness gate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from deerflow.config.app_config import (
    AppConfig,
    get_app_config,
    pop_current_app_config,
    push_current_app_config,
)
from deerflow.config.model_config import ModelConfig
from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.model_providers import build_repository
from deerflow.persistence.model_providers.repository import (
    ModelProviderRecord,
    ModelProviderRepository,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

#: How long (seconds) a per-user provider list is cached in-process before the
#: middleware re-queries the DB. Short enough that CRUD writes become visible
#: quickly, long enough to avoid a DB round-trip on every request.
_CACHE_TTL_SECONDS = 30.0


def _record_to_model_config(record: ModelProviderRecord) -> ModelConfig:
    """Convert a decrypted provider record into a ``ModelConfig``.

    ``api_key`` and ``api_base`` ride on ``ModelConfig``'s ``extra="allow"``
    field set so ``create_chat_model`` dumps them into the langchain
    constructor verbatim — identical to a YAML-configured model.
    """
    kwargs: dict = {
        "api_key": record.api_key,
    }
    if record.base_url:
        kwargs["api_base"] = record.base_url
    return ModelConfig(
        name=record.name,
        display_name=record.display_name,
        description=record.description,
        use=record.use,
        model=record.model,
        supports_thinking=record.supports_thinking,
        supports_reasoning_effort=record.supports_reasoning_effort,
        **kwargs,
    )


def merge_user_models(
    base_models: list[ModelConfig],
    user_models: list[ModelConfig],
) -> list[ModelConfig]:
    """Merge base + user models; user entries override base on name clash.

    Ordering preserves base-model order (so existing model menus don't
    reshuffle), appends non-colliding user models afterwards, and replaces
    colliding base models in-place.
    """
    user_by_name = {m.name: m for m in user_models}
    merged: list[ModelConfig] = []
    seen_user_names: set[str] = set()
    for base in base_models:
        if base.name in user_by_name:
            merged.append(user_by_name[base.name])
            seen_user_names.add(base.name)
        else:
            merged.append(base)
    for user_model in user_models:
        if user_model.name not in seen_user_names:
            merged.append(user_model)
    return merged


class ModelProviderConfigMiddleware(BaseHTTPMiddleware):
    """Inject per-user model providers into the request-scoped ``AppConfig``.

    Fail-open: any error during lookup or merge drops back to the base config
    (the request still succeeds). Caches each user's provider list for
    ``_CACHE_TTL_SECONDS`` to avoid a DB round-trip on every request.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        # user_id -> (fetched_at_monotonic, [ModelProviderRecord])
        self._cache: dict[str, tuple[float, list[ModelProviderRecord]]] = {}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        merged_config = await self._maybe_build_override(request)
        if merged_config is None:
            return await call_next(request)

        push_current_app_config(merged_config)
        try:
            return await call_next(request)
        finally:
            pop_current_app_config()

    # -- override construction ------------------------------------------

    async def _maybe_build_override(self, request: Request) -> AppConfig | None:
        """Return a merged ``AppConfig`` for the request's user, or ``None``.

        Returns ``None`` (no override) when there is no authenticated user,
        no persistence layer, no user-owned providers, or any error occurs —
        the base config is used as-is in all those cases.
        """
        user = getattr(request.state, "user", None)
        if user is None:
            return None
        user_id = str(user.id)

        repo = self._get_repository(request)
        if repo is None:
            return None

        try:
            records = await self._list_cached(user_id, repo)
        except Exception:
            logger.exception(
                "Failed to load model providers for user %s; falling back to base config",
                user_id,
            )
            return None

        if not records:
            return None

        user_models = [_record_to_model_config(r) for r in records]
        try:
            base = get_app_config()
            merged_models = merge_user_models(list(base.models), user_models)
            return base.model_copy(update={"models": merged_models})
        except Exception:
            logger.exception(
                "Failed to merge user model providers for user %s; falling back to base config",
                user_id,
            )
            return None

    def _get_repository(self, request: Request) -> ModelProviderRepository | None:
        """Resolve the singleton repository, or ``None`` if persistence is off.

        Mirrors the CRUD router's lazy-init: cache on ``app.state`` so the
        cipher is constructed once per process.
        """
        app_state = request.app.state
        repo = getattr(app_state, "model_provider_repo", None)
        if isinstance(repo, ModelProviderRepository):
            return repo
        sf = get_session_factory()
        if sf is None:
            return None
        repo = build_repository(sf)
        app_state.model_provider_repo = repo
        return repo

    async def _list_cached(
        self,
        user_id: str,
        repo: ModelProviderRepository,
    ) -> list[ModelProviderRecord]:
        """Return the user's providers, caching for ``_CACHE_TTL_SECONDS``."""
        now = time.monotonic()
        cached = self._cache.get(user_id)
        if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        records = await repo.list_by_user(user_id)
        self._cache[user_id] = (now, list(records))
        return records

    def invalidate(self, user_id: str | None = None) -> None:
        """Drop cached providers for ``user_id`` (or the whole cache).

        Intended to be called after a CRUD write so the next request sees the
        change immediately rather than waiting for the TTL.
        """
        if user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(user_id, None)


__all__ = [
    "ModelProviderConfigMiddleware",
    "merge_user_models",
]
