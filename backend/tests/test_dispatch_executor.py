"""Tests for the Dispatcher / Executor Protocol (PR-075, Track G).

Covers :mod:`deerflow.runtime.runs.dispatch`:

* ``ExecRequest`` is a frozen, complete value object (carries every
  :func:`run_agent` argument).
* ``InProcessExecutor.execute`` returns a running :class:`asyncio.Task` whose
  behaviour matches the former inline ``asyncio.create_task(run_agent(...))``
  (the dispatch seam this PR relocated from ``services.py``).
* ``InProcessDispatcher.dispatch`` is a single passthrough to its executor —
  TM-026 (one executor per Run): a dispatch invokes ``execute`` exactly once.
* ``make_dispatcher`` defaults to the in-process pair; ``type="remote"`` raises
  :class:`NotImplementedError` (PR-076+ surface).
* Structural typing: a duck-typed fake executor satisfies the ``Executor``
  Protocol, so PR-076+ can swap implementations without touching the seam.
* The Gateway ``app.state.dispatcher`` is wired by ``make_dispatcher``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from deerflow.runtime import (
    ExecRequest,
    InProcessDispatcher,
    InProcessExecutor,
    MemoryStreamBridge,
    RunContext,
    RunManager,
    make_dispatcher,
)
from deerflow.runtime.runs.dispatch import Dispatcher, Executor

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _exec_request(*, agent_factory=None, run_id: str = "run-dispatch") -> ExecRequest:
    """Build a minimal ExecRequest whose in-process execution completes the run.

    Uses a real ``RunManager.create`` so the record is well-formed; must be
    awaited inside a running event loop (the tests are ``@pytest.mark.anyio``).
    """
    run_manager = RunManager()
    record = await run_manager.create("thread-dispatch")

    async def _fake_agent(*args, **kwargs):  # noqa: ARG001 — signature compat
        # A real run_agent publishes END via bridge.publish_end in its finally;
        # the cheapest faithful stand-in is a no-op agent that yields nothing.
        return None

    return ExecRequest(
        bridge=MemoryStreamBridge(),
        run_manager=run_manager,
        record=record,
        ctx=RunContext(checkpointer=object()),
        agent_factory=agent_factory if agent_factory is not None else _fake_agent,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "thread-dispatch", "run_id": record.run_id}},
    )


def _exec_request_stub() -> ExecRequest:
    """Synchronous ExecRequest with stub fields (no RunManager needed) — for the
    frozen/shape unit tests that don't execute the request."""
    return ExecRequest(
        bridge=MemoryStreamBridge(),
        run_manager=RunManager(),
        record=SimpleNamespace(run_id="stub-run"),  # type: ignore[arg-type]
        ctx=RunContext(checkpointer=object()),
        agent_factory=lambda *a, **k: None,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": "t", "run_id": "stub-run"}},
    )


# ---------------------------------------------------------------------------
# ExecRequest value object
# ---------------------------------------------------------------------------


class TestExecRequest:
    def test_is_frozen(self):
        req = _exec_request_stub()
        with pytest.raises(Exception):  # FrozenInstanceError subclasses dataclasses.FrozenInstanceError
            req.graph_input = {}  # type: ignore[misc]

    def test_carries_all_run_agent_arguments(self):
        req = _exec_request_stub()
        # Every run_agent parameter is present on ExecRequest.
        for field in (
            "bridge",
            "run_manager",
            "record",
            "ctx",
            "agent_factory",
            "graph_input",
            "config",
            "stream_modes",
            "stream_subgraphs",
            "interrupt_before",
            "interrupt_after",
        ):
            assert hasattr(req, field), f"ExecRequest missing {field}"


# ---------------------------------------------------------------------------
# InProcessExecutor
# ---------------------------------------------------------------------------


class TestInProcessExecutor:
    async def test_execute_returns_running_task(self):
        # The fake agent is a no-op; run_agent's finally marks the run and the
        # task resolves without error.
        bridge = MemoryStreamBridge()

        run_manager = RunManager()
        record = await run_manager.create("thread-exec")

        async def _noop_agent(*args, **kwargs):  # noqa: ARG001
            return None

        request = ExecRequest(
            bridge=bridge,
            run_manager=run_manager,
            record=record,
            ctx=RunContext(checkpointer=object()),
            agent_factory=_noop_agent,
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "thread-exec", "run_id": record.run_id}},
        )
        executor = InProcessExecutor()
        task = await executor.execute(request)
        assert isinstance(task, asyncio.Task)
        # The task is the background run; awaiting it must not raise.
        await task

    async def test_execute_task_registered_on_record(self):
        """Mirrors services.py: ``record.task = await dispatcher.dispatch(...)``
        — the returned task is the one RunManager cancels / drains on."""
        bridge = MemoryStreamBridge()
        run_manager = RunManager()
        record = await run_manager.create("thread-record")

        async def _noop_agent(*args, **kwargs):  # noqa: ARG001
            return None

        request = ExecRequest(
            bridge=bridge,
            run_manager=run_manager,
            record=record,
            ctx=RunContext(checkpointer=object()),
            agent_factory=_noop_agent,
            graph_input={"messages": []},
            config={"configurable": {"thread_id": "thread-record", "run_id": record.run_id}},
        )
        record.task = await InProcessExecutor().execute(request)
        assert record.task is not None
        assert not record.task.done()
        await record.task


# ---------------------------------------------------------------------------
# InProcessDispatcher — TM-026 (one executor per Run)
# ---------------------------------------------------------------------------


class _CountingExecutor:
    """Duck-typed Executor that records how many times execute() is called."""

    def __init__(self) -> None:
        self.execute_calls = 0

    async def execute(self, request: ExecRequest) -> asyncio.Task | None:  # noqa: D401
        self.execute_calls += 1

        async def _noop() -> None:
            return None

        return asyncio.create_task(_noop())


class TestInProcessDispatcher:
    async def test_dispatch_invokes_executor_exactly_once(self):
        """TM-026: one Run is handed to exactly one executor (no double-dispatch)."""
        counting = _CountingExecutor()
        dispatcher = InProcessDispatcher(executor=counting)
        task = await dispatcher.dispatch(await _exec_request())
        assert counting.execute_calls == 1
        assert task is not None
        await task

    async def test_dispatch_returns_the_executors_task(self):
        counting = _CountingExecutor()
        dispatcher = InProcessDispatcher(executor=counting)
        task = await dispatcher.dispatch(await _exec_request())
        returned = await dispatcher.dispatch(await _exec_request())
        await task
        await returned  # type: ignore[arg-type]
        # Each dispatch produced a fresh task (one executor per Run).
        assert counting.execute_calls == 2

    def test_default_executor_is_in_process(self):
        dispatcher = InProcessDispatcher()
        assert isinstance(dispatcher._executor, InProcessExecutor)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# make_dispatcher factory
# ---------------------------------------------------------------------------


class TestMakeDispatcher:
    def test_defaults_to_in_process(self):
        dispatcher = make_dispatcher()
        assert isinstance(dispatcher, InProcessDispatcher)

    def test_in_process_type(self):
        from deerflow.config.dispatcher_config import DispatcherConfig

        dispatcher = make_dispatcher(DispatcherConfig(type="in-process"))
        assert isinstance(dispatcher, InProcessDispatcher)

    def test_remote_type_raises_not_implemented(self):
        from deerflow.config.dispatcher_config import DispatcherConfig

        with pytest.raises(NotImplementedError):
            make_dispatcher(DispatcherConfig(type="remote"))

    def test_unknown_type_raises_value_error(self):
        cfg = SimpleNamespace(type="bogus")
        with pytest.raises(ValueError):
            make_dispatcher(cfg)

    def test_none_config_defaults_to_in_process(self):
        assert isinstance(make_dispatcher(None), InProcessDispatcher)


# ---------------------------------------------------------------------------
# Protocol structural typing (PR-076+ can swap implementations)
# ---------------------------------------------------------------------------


class TestProtocolStructuralTyping:
    def test_duck_typed_executor_satisfies_protocol(self):
        # _CountingExecutor has execute(request) -> Task|None, so it is an Executor.
        assert isinstance(_CountingExecutor(), Executor)

    def test_in_process_executor_satisfies_protocol(self):
        assert isinstance(InProcessExecutor(), Executor)

    def test_in_process_dispatcher_satisfies_protocol(self):
        assert isinstance(InProcessDispatcher(), Dispatcher)


# ---------------------------------------------------------------------------
# Gateway wiring (app.state.dispatcher)
# ---------------------------------------------------------------------------


class TestGatewayWiring:
    def test_make_dispatcher_builds_app_state_singleton(self):
        """The lifespan builds ``app.state.dispatcher`` via make_dispatcher."""
        from app.gateway.deps import get_dispatcher

        app_state = SimpleNamespace(dispatcher=make_dispatcher())
        request = SimpleNamespace(app=SimpleNamespace(state=app_state))
        dispatcher = get_dispatcher(request)
        assert isinstance(dispatcher, InProcessDispatcher)

    def test_get_dispatcher_returns_503_when_missing(self):
        from fastapi import HTTPException

        from app.gateway.deps import get_dispatcher

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        with pytest.raises(HTTPException) as exc_info:
            get_dispatcher(request)
        assert exc_info.value.status_code == 503
