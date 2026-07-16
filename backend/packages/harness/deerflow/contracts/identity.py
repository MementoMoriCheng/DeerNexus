"""PrincipalRef contract.

A frozen reference to the authenticated actor behind a request or trusted
task. It is the audit/attribution subject used across tenant context, audit
events and run envelopes.

Design rules (``docs/architecture/runtime-contracts.md`` §4):

* ``id`` is the durable audit subject id and must never be empty;
* ``display_name`` is for human display only and must never participate in
  authorization;
* a ``service_account`` principal has no backing user;
* a ``system`` principal may only execute explicitly allow-listed platform
  tasks and must never impersonate a user.

This DTO carries no roles or permissions: long-running code must not hold a
stale permission set. Authorization is evaluated per-action against the live
principal identity (see the Policy contract, shipped in a later PR).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PrincipalType = Literal["user", "service_account", "system"]
"""Closed set of principal categories. Unknown values are rejected at the
deserialization boundary so consumers fail closed rather than silently mapping
an unexpected identity type."""


class PrincipalRef(BaseModel):
    """Stable audit subject for tenant-scoped requests and tasks.

    Only ``user`` principals may carry a ``user_id``. ``service_account`` and
    ``system`` principals must leave it empty so a non-human identity can never
    masquerade as a user in audit attribution.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: PrincipalType = Field(
        description="Principal category; unknown values are rejected (fail-closed).",
    )
    id: str = Field(
        min_length=1,
        description="Stable audit subject id; never empty.",
    )
    user_id: str | None = Field(
        default=None,
        description="Backing user id; only meaningful for 'user' principals.",
    )
    display_name: str | None = Field(
        default=None,
        description="Human-readable label for display only; never used for authorization.",
    )

    @model_validator(mode="after")
    def _user_id_only_for_users(self) -> Self:
        if self.type != "user" and self.user_id is not None:
            raise ValueError(f"principal type '{self.type}' must not carry a user_id (only 'user' principals may have a backing user_id)")
        return self
