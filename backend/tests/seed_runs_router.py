"""Test-only run/message seeder for the multi-run render-order e2e (issue #3352).

Mounted **only** by ``scripts/run_replay_gateway.py`` (the replay e2e gateway)
and never by the production app, so it cannot ship. It lets a Playwright spec
stand up a thread with >=2 runs whose per-run messages exercise the frontend's
reload / history-rebuild ordering path — with no real model, no recording, and
no API key.

Why a seeder instead of recording a conversation: issue #3352 only reproduces
when the checkpoint no longer holds the older messages (post-compression), so
the frontend rebuilds them from the per-run history endpoints. A seeder lets us
create exactly that precondition deterministically — runs in the run store +
per-run ``category="message"`` events, and **no checkpoint** — so on reload the
buggy ``findLatestUnloadedRunIndex`` + prepend in ``core/threads/hooks.ts`` is
the sole source of truth and its reversed order becomes observable.

It writes through the gateway's OWN ``app.state.run_store`` +
``app.state.run_event_store`` using the request's auth context, so the seeded
``user_id`` matches the browser session that reads it back. The event shape
mirrors exactly what ``runtime/journal.py`` writes for real runs
(``event_type`` ``llm.human.input`` / ``llm.ai.response``, ``category``
``"message"``, ``content`` = ``message.model_dump()``, ``metadata.caller`` =
``"lead_agent"``).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/test-only", tags=["test-only"])

# Mirror runtime/journal.py: human prompts are recorded as ``llm.human.input``
# and assistant turns as ``llm.ai.response``; both land in ``category="message"``.
_EVENT_TYPE = {"human": "llm.human.input", "ai": "llm.ai.response"}


class SeedMessage(BaseModel):
    role: Literal["human", "ai"]
    content: str
    id: str


class SeedRun(BaseModel):
    run_id: str
    # ISO timestamp; RunManager.list_by_thread sorts newest-first by created_at,
    # so a later created_at must mean a later run for the ordering to be faithful.
    created_at: str
    messages: list[SeedMessage]


class SeedRunsBody(BaseModel):
    thread_id: str
    runs: list[SeedRun]


@router.post("/seed-runs")
async def seed_runs(body: SeedRunsBody, request: Request) -> dict:
    """Seed runs + per-run message events for the authenticated user.

    No checkpoint is written: that is the whole point — it forces the frontend's
    reload path to rebuild history from the per-run endpoints (the #3352 bug
    site) instead of the (correctly ordered) checkpoint snapshot.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    run_store = request.app.state.run_store
    event_store = request.app.state.run_event_store

    for run in body.runs:
        # user_id defaults (AUTO) to the request's auth context, matching the
        # browser session that will read these runs back via GET /runs.
        await run_store.put(
            run.run_id,
            thread_id=body.thread_id,
            assistant_id="lead_agent",
            status="success",
            created_at=run.created_at,
        )
        events = []
        for m in run.messages:
            msg = (HumanMessage if m.role == "human" else AIMessage)(content=m.content, id=m.id)
            events.append(
                {
                    "thread_id": body.thread_id,
                    "run_id": run.run_id,
                    "event_type": _EVENT_TYPE[m.role],
                    "category": "message",
                    "content": msg.model_dump(),
                    "metadata": {"caller": "lead_agent"},
                    "created_at": run.created_at,
                }
            )
        # One batch per run so seq is monotonic and run1's messages precede
        # run2's; the gateway reads them back per-run anyway.
        await event_store.put_batch(events)

    return {"ok": True, "thread_id": body.thread_id, "runs": len(body.runs)}


@router.post("/seed-admin-iam")
async def seed_admin_iam(request: Request) -> dict:
    """Seed admin OrgMembership + RoleBinding for the authenticated user (PR-032).

    The replay e2e gateway registers a throwaway user via ``/api/v1/auth/register``
    (which creates the User row but no IAM relationships). Since PR-032 the
    runtime routers consult ``AuthorizeService.authorize()``, which requires an
    active OrgMembership + a role binding carrying the runtime permissions —
    without this seed every ``/api/threads/*/runs/stream`` call 403s and the
    Playwright render assertion times out.

    Reads the user_id off the request's auth context (the same identity the
    browser session uses), then writes the admin membership + system admin role
    binding via the tenancy helpers (idempotent). Mounted only by the replay
    gateway, never in production.
    """
    from app.gateway.config import get_gateway_config
    from app.gateway.deps import get_current_user_from_request
    from deerflow.persistence.engine import get_session_factory
    from deerflow.tenancy import (
        ensure_admin_membership,
        ensure_admin_role_binding,
        ensure_system_admin_role,
    )

    user = await get_current_user_from_request(request)
    user_id = str(user.id)

    sf = get_session_factory()
    if sf is None:
        return {"ok": False, "error": "persistence not initialised"}

    org_id = get_gateway_config().default_org_id
    await ensure_admin_membership(sf, org_id=org_id, user_id=user_id)
    role = await ensure_system_admin_role(sf)
    await ensure_admin_role_binding(sf, org_id=org_id, user_id=user_id, role_id=role.id)

    return {"ok": True, "user_id": user_id, "org_id": org_id}
