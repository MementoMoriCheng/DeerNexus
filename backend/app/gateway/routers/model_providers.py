"""User-owned custom model-provider CRUD API.

Each authenticated user can register private, OpenAI-compatible model
providers (custom endpoint + API key). Providers are scoped to the owning
user — every repository query filters by ``owner_user_id`` — so the API
needs no org-level RBAC: an authenticated user managing their own rows is
the entire authorization surface (mirrors ``connect_channel_provider``).

The API key is never returned in cleartext; the response exposes only
``has_api_key``. The cleartext key reaches the LLM call chain exclusively
through the per-request config-merge middleware (PR-B), which reads the
decrypted ``ModelProviderRecord.api_key`` server-side.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from deerflow.persistence.engine import get_session_factory
from deerflow.persistence.model_providers import build_repository
from deerflow.persistence.model_providers.repository import ModelProviderRepository

router = APIRouter(prefix="/api/model-providers", tags=["model-providers"])
logger = logging.getLogger(__name__)

_DEFAULT_USE = "langchain_openai:ChatOpenAI"


def _get_user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(user.id)


def _get_repository(request: Request) -> ModelProviderRepository:
    repo = getattr(request.app.state, "model_provider_repo", None)
    if isinstance(repo, ModelProviderRepository):
        return repo

    sf = get_session_factory()
    if sf is None:
        raise HTTPException(status_code=503, detail="Model-provider persistence is not available")

    repo = build_repository(sf)
    request.app.state.model_provider_repo = repo
    return repo


class ModelProviderResponse(BaseModel):
    """Public view of a model provider — never exposes the API key."""

    id: str
    name: str
    display_name: str | None = None
    description: str | None = None
    model: str
    use: str
    base_url: str | None = None
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    has_api_key: bool = True


class ModelProviderListResponse(BaseModel):
    providers: list[ModelProviderResponse] = Field(default_factory=list)


class CreateModelProviderRequest(BaseModel):
    name: str = Field(..., description="Unique identifier for the provider (e.g. 'my-deepseek')")
    model: str = Field(..., description="Provider model id (e.g. 'deepseek-chat')")
    api_key: str = Field(..., description="Secret API key (encrypted at rest)")
    display_name: str | None = None
    description: str | None = None
    use: str = Field(default=_DEFAULT_USE, description="LangChain provider class path")
    base_url: str | None = None
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False


class UpdateModelProviderRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    model: str | None = None
    use: str | None = None
    base_url: str | None = None
    supports_thinking: bool | None = None
    supports_reasoning_effort: bool | None = None
    api_key: str | None = Field(
        default=None,
        description="New API key; omit/leave null to keep the existing key unchanged",
    )


def _to_response(record) -> ModelProviderResponse:
    return ModelProviderResponse(
        id=record.id,
        name=record.name,
        display_name=record.display_name,
        description=record.description,
        model=record.model,
        use=record.use,
        base_url=record.base_url,
        supports_thinking=record.supports_thinking,
        supports_reasoning_effort=record.supports_reasoning_effort,
        has_api_key=bool(record.api_key),
    )


@router.get("", response_model=ModelProviderListResponse, summary="List My Model Providers")
async def list_model_providers(request: Request) -> ModelProviderListResponse:
    """List all custom model providers owned by the current user."""
    repo = _get_repository(request)
    records = await repo.list_by_user(_get_user_id(request))
    return ModelProviderListResponse(providers=[_to_response(r) for r in records])


@router.post("", response_model=ModelProviderResponse, status_code=201, summary="Create Model Provider")
async def create_model_provider(
    payload: CreateModelProviderRequest, request: Request
) -> ModelProviderResponse:
    """Register a new private model provider for the current user."""
    repo = _get_repository(request)
    try:
        record = await repo.create(
            owner_user_id=_get_user_id(request),
            name=payload.name,
            model=payload.model,
            api_key=payload.api_key,
            display_name=payload.display_name,
            description=payload.description,
            use=payload.use,
            base_url=payload.base_url,
            supports_thinking=payload.supports_thinking,
            supports_reasoning_effort=payload.supports_reasoning_effort,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail=f"A model provider named '{payload.name}' already exists"
        )
    return _to_response(record)


@router.put("/{provider_id}", response_model=ModelProviderResponse, summary="Update Model Provider")
async def update_model_provider(
    provider_id: str, payload: UpdateModelProviderRequest, request: Request
) -> ModelProviderResponse:
    """Update an editable field set of a provider owned by the current user."""
    repo = _get_repository(request)
    fields = payload.model_dump(exclude={"api_key"}, exclude_unset=True)
    try:
        record = await repo.update(
            owner_user_id=_get_user_id(request),
            provider_id=provider_id,
            api_key=payload.api_key,
            **fields,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail="A model provider with that name already exists")
    if record is None:
        raise HTTPException(status_code=404, detail="Model provider not found")
    return _to_response(record)


@router.delete("/{provider_id}", status_code=204, summary="Delete Model Provider")
async def delete_model_provider(provider_id: str, request: Request) -> None:
    """Delete a provider owned by the current user."""
    repo = _get_repository(request)
    deleted = await repo.delete(_get_user_id(request), provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Model provider not found")
    return None
