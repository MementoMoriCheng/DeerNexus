"""Configuration for the Dispatcher / Executor seam (PR-075).

The dispatcher hands a Run to an executor. ``in-process`` (default) is the
embedded passthrough that schedules ``run_agent`` in this process — today's
behaviour, zero change. ``remote`` (dispatch outbox / physical worker) is the
PR-076+ surface, gated on ADR-0006 §2.2 physical-split triggers; it is not
implemented here and ``make_dispatcher`` raises ``NotImplementedError`` for it.
"""

from typing import Literal

from pydantic import BaseModel, Field

DispatcherType = Literal["in-process", "remote"]


class DispatcherConfig(BaseModel):
    """Configuration for the dispatcher that hands Runs to an executor."""

    type: DispatcherType = Field(
        default="in-process",
        description=("Dispatcher backend type. 'in-process' schedules the agent in this process (default, single-replica). 'remote' (dispatch outbox / physical worker) is PR-076+, gated on ADR-0006 §2.2 triggers; not yet implemented."),
    )
