"""Dispatcher / Executor Protocol — the run handoff seam (PR-075, Track G).

This module instantiates ADR-0006 §8 **Phase 0** ("同进程接口化"): extract the
``Dispatcher`` / ``Executor`` Protocol so embedded execution and a future
remote worker use the *same contract*, without changing today's deployment
topology.

Layer map (Track G, each layer orthogonal):
    state authority   (PR-070)  transitions.py + CAS — "single terminal winner"
    coordination      (PR-071)  ownership.py      — "who may drive this Run"
    convergence       (PR-072)  reconciler        — orphan → safe terminal
    read fan-out      (PR-073)  stream_bridge     — cross-replica SSE
    dispatch seam ← THIS PR    dispatch.py        — "hand Run to the executor"

The seam today is a single hardcoded ``asyncio.create_task(run_agent(...))`` in
``app/gateway/services.py``. PR-075 replaces it with an injected
``Dispatcher`` whose in-process implementation is a byte-for-byte passthrough
of that call. A future PR-076+ ``RemoteDispatcher`` (dispatch outbox / queue,
gated on ADR §2.2 physical-split triggers) will publish a dispatch signal to
Redis instead of calling the executor directly — but satisfies the same Protocol.

Contracts (ADR §5.1 lifecycle, TM items):
    - **persist Run before dispatch** — the caller (``start_run``) guarantees
      ``create_or_reject`` has persisted the Run before calling ``dispatch``.
    - **one Executor per Run** (TM-026) — the dispatcher chooses exactly one
      executor per Run; in-process this is structurally guaranteed (single
      process, single ``create_task``). The Protocol encodes the invariant so a
      future multi-executor dispatcher cannot double-dispatch.
    - **executor re-checks Run is still executable** (TM-025) — in-process this
      is the lease ``claim`` inside ``run_agent`` (PR-071): if ownership moved,
      the claim fails and the run is not executed.
    - **integrity is null in-process** (TM-024) — same-DB reads (Gateway ↔
      embedded executor) may omit ``EnvelopeIntegrity``; a cross-trust-boundary
      (PR-076+ queue) executor must verify it.
    - **at-least-once, never blind replay** (TM-028) — the dispatcher's retry
      contract applies to the *publish*, not the execution; in-process publish
      is a direct call (cannot fail independently of Run creation), so it is
      trivially at-least-once. The reconciler (PR-072) is the safety net for
      orphans — it drives to a safe terminal, never replays.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .manager import RunManager, RunRecord
from .worker import RunContext, run_agent

# Re-export so callers (and tests) can import run_agent via the dispatch module
# if they prefer the seam-local spelling. Kept in __all__ below.
__all__ = [
    "Dispatcher",
    "ExecRequest",
    "Executor",
    "InProcessDispatcher",
    "InProcessExecutor",
    "make_dispatcher",
    "run_agent",
]


@dataclass(frozen=True)
class ExecRequest:
    """Immutable request value object for executing a Run.

    Packages the arguments ``run_agent`` needs (the executor's input today) into
    one frozen value object so the ``Executor`` Protocol carries a stable
    contract. Semantically this mirrors ADR §6 ``RunEnvelope`` — the trusted
    task envelope consumed by the executor — but in-process (PR-075) we keep a
    *lightweight* wrapper rather than assembling a full ``RunEnvelope``:
    ``release_ref`` / ``policy_snapshot`` / ``integrity`` are optional or
    default-off in ``start_run`` today, so full envelope assembly (and the
    cross-trust-boundary integrity verification) is deferred to PR-076+, where
    the envelope actually crosses a process boundary. The ``record`` field
    carries the run/thread/org identity that an envelope would also carry.

    Attributes mirror :func:`run_agent`'s keyword parameters verbatim.
    """

    bridge: Any
    run_manager: RunManager
    record: RunRecord
    ctx: RunContext
    agent_factory: Any
    graph_input: dict
    config: dict
    stream_modes: list[str] | None = None
    stream_subgraphs: bool = False
    interrupt_before: list[str] | Literal["*"] | None = None
    interrupt_after: list[str] | Literal["*"] | None = None


@runtime_checkable
class Executor(Protocol):
    """Consumes an :class:`ExecRequest` and runs the agent graph.

    The executor is the seam a future remote worker (PR-076+) will implement:
    today the only implementation is :class:`InProcessExecutor`, which schedules
    :func:`run_agent` in this process; a remote executor would translate the
    request into a dispatch signal published to a queue (ADR §4.4).

    Contract:
    - Returns *without blocking* on the agent's completion (the run executes in
      the background); the returned :class:`asyncio.Task` is registered on
      ``record.task`` by the dispatcher's caller so RunManager can cancel/await/
      drain it.
    - Re-checks the Run is still executable before doing real work (TM-025);
      in-process this is the lease ``claim`` inside :func:`run_agent`.
    - May return ``None`` when no background task is created (e.g. the run was
      already claimed elsewhere); callers must handle both.
    """

    async def execute(self, request: ExecRequest) -> asyncio.Task[None] | None:
        """Schedule the run for background execution, returning its task."""
        ...


@runtime_checkable
class Dispatcher(Protocol):
    """The Gateway-side handoff: choose *one* executor for a Run.

    Today (in-process) the dispatcher is a passthrough to the embedded executor.
    A future remote dispatcher (PR-076+) will, per-run, route to a local or
    remote executor (ADR §8 Phase 2: "同一 Run 只能选择一个 Executor") and own
    the dispatch-outbox publish + retry (ADR §5.1: "由 dispatcher 重试").

    Contract (ADR §5.1):
    - The caller has already persisted the Run (persist-before-dispatch).
    - Exactly one executor is handed the Run (TM-026 anti-double-dispatch).
    - The returned task (if any) is registered on ``record.task`` by the caller.
    """

    async def dispatch(self, request: ExecRequest) -> asyncio.Task[None] | None:
        """Hand the run to exactly one executor and return its background task."""
        ...


class InProcessExecutor:
    """Embedded executor: schedule :func:`run_agent` in this process.

    This is a byte-for-byte relocation of the original
    ``asyncio.create_task(run_agent(...))`` site from
    ``app/gateway/services.py``. The lease claim/heartbeat/release lifecycle
    (PR-071) remains inside :func:`run_agent` and is transparently inherited.
    """

    async def execute(self, request: ExecRequest) -> asyncio.Task[None] | None:
        return asyncio.create_task(
            run_agent(
                request.bridge,
                request.run_manager,
                request.record,
                ctx=request.ctx,
                agent_factory=request.agent_factory,
                graph_input=request.graph_input,
                config=request.config,
                stream_modes=request.stream_modes,
                stream_subgraphs=request.stream_subgraphs,
                interrupt_before=request.interrupt_before,
                interrupt_after=request.interrupt_after,
            )
        )


class InProcessDispatcher:
    """In-process dispatcher: a passthrough to a single embedded executor.

    Holds one :class:`Executor` and hands every Run to it. This is the explicit
    "dispatch decision" site that is currently implicit (always "run locally").
    A PR-076+ remote dispatcher will subclass / replace this to add per-Run
    routing (feature flag → remote executor) and the outbox publish, while still
    enforcing "one executor per Run" (TM-026).
    """

    def __init__(self, executor: Executor | None = None) -> None:
        self._executor: Executor = executor if executor is not None else InProcessExecutor()

    async def dispatch(self, request: ExecRequest) -> asyncio.Task[None] | None:
        # In-process: no independent publish to retry (TM-028 trivially
        # satisfied); the executor is the only one, so TM-026 (one executor per
        # Run) is structurally guaranteed.
        return await self._executor.execute(request)


def make_dispatcher(
    config: Any = None,
    *,
    executor: Executor | None = None,
) -> Dispatcher:
    """Build a :class:`Dispatcher` keyed on the dispatcher config ``type``.

    Mirrors :func:`make_lease_store` (ownership.py): the default is the
    in-process variant so single-replica / dev deployments are unchanged.
    ``type="remote"`` (dispatch outbox / remote worker) is the PR-076+ surface
    and raises :class:`NotImplementedError` here — it is gated on the ADR §2.2
    physical-split triggers and a separate plan.
    """
    dispatch_type = "in-process"
    if config is not None:
        dispatch_type = getattr(config, "type", None) or "in-process"

    if dispatch_type == "in-process":
        return InProcessDispatcher(executor=executor)
    if dispatch_type == "remote":
        raise NotImplementedError("Remote dispatcher (dispatch outbox / remote worker) is PR-076+, gated on ADR-0006 §2.2 physical-split triggers.")
    raise ValueError(f"Unknown dispatcher type: {dispatch_type!r}")
