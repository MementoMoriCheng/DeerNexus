"""Class A same-transaction audit atomicity tests (PR-042).

Locks ADR-0005 §7.1's core guarantee: **a Class A control-plane write and its
``audit_outbox`` enqueue commit in the same transaction, so an outbox write
failure rolls back the business write** (no "business success without an audit
row"). Also covers:

* ADR §4 action normalization — ``<domain>.<resource>.<verb>``;
* ``build_audit_event`` resource/actor/outcome projection;
* ``enqueue_audit_outbox_in_session`` adds a row to a caller session WITHOUT
  committing (the caller owns the transaction);
* the IAM repository write helpers' ``session=`` passthrough stages the
  mutation in the caller's transaction (no own-commit when a session is passed).

Fixture conventions mirror ``test_audit_outbox.py``: isolated SQLite via
``init_engine`` (full bootstrap installs migrations 0010/0011 so both
``audit_events`` and ``audit_outbox`` exist).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.contracts.identity import PrincipalRef
from deerflow.contracts.policy import ResourceRef
from deerflow.persistence.audit.model import AuditOutboxRow
from deerflow.persistence.audit.outbox import enqueue_audit_outbox_in_session
from deerflow.persistence.iam.model import ServiceAccountRow
from deerflow.persistence.iam.repository import create_service_account
from deerflow.persistence.orgs.model import OrganizationRow
from deerflow.tenancy.audit_events import (
    TENANT_EVENT_ACTION_REGISTRY,
    build_audit_event,
)

ORG_A = "00000000-0000-4000-8000-0000000000a1"
USER_ID = "00000000-0000-4000-8000-0000000000c3"


def _actor() -> PrincipalRef:
    return PrincipalRef(type="user", id=USER_ID, user_id=USER_ID)


def _resource(*, type_: str, id_: str) -> ResourceRef:
    return ResourceRef(type=type_, id=id_, org_id=ORG_A)


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'classa.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        sf = get_session_factory()
        # Seed the Org so FK-style inserts (service_accounts.org_id) are valid.
        async with sf() as session:
            session.add(OrganizationRow(id=ORG_A, slug="org-a", name="Org A", status="active"))
            await session.commit()
        yield sf
    finally:
        await close_engine()


# ===========================================================================
# enqueue_audit_outbox_in_session — no own-commit, caller owns the transaction
# ===========================================================================


class TestEnqueueInSession:
    @pytest.mark.anyio
    async def test_enqueue_does_not_commit_rollback_discards_row(self, sf):
        """The helper stages a row but the caller's rollback must discard it."""
        event = build_audit_event(
            "iam.service_account.created",
            org_id=ORG_A,
            actor=_actor(),
            resource=_resource(type_="service_account", id_="sa-1"),
        )
        async with sf() as session:
            await enqueue_audit_outbox_in_session(session, event)
            # Row is staged in this session but not committed.
            await session.rollback()
        # No row landed: the caller owned the transaction and rolled it back.
        async with sf() as session:
            count = int((await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_A))).all().__len__())
        assert count == 0

    @pytest.mark.anyio
    async def test_enqueue_then_commit_persists_row(self, sf):
        event = build_audit_event(
            "iam.role_binding.created",
            org_id=ORG_A,
            actor=_actor(),
            resource=_resource(type_="role_binding", id_="rb-1"),
        )
        async with sf() as session:
            await enqueue_audit_outbox_in_session(session, event)
            await session.commit()
        async with sf() as session:
            row = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_A))).scalar_one()
        assert row.status == "pending"


# ===========================================================================
# §7.1 atomicity — outbox failure rolls back the business write
# ===========================================================================


class TestClassAAtomicity:
    @pytest.mark.anyio
    async def test_outbox_failure_rolls_back_business_write(self, sf):
        """ADR §7.1: a failed outbox enqueue inside the business transaction
        rolls back the ServiceAccount insert. We force the failure by
        pre-seeding the ``uq_audit_outbox_event_id`` unique index with the
        same event_id the enqueue will try to use — the IntegrityError
        aborts the shared transaction, so neither the SA row nor the outbox
        row lands. There is NO "business success without an audit row"."""
        from deerflow.persistence.iam.repository import create_service_account as repo_create

        # Pre-occupy the event_id we will reuse to force the collision.
        reused_event = build_audit_event(
            "iam.service_account.created",
            org_id=ORG_A,
            actor=_actor(),
            resource=_resource(type_="service_account", id_="sa-collide"),
        )
        async with sf() as session:
            await enqueue_audit_outbox_in_session(session, reused_event)
            await session.commit()

        # Now drive the business write + an enqueue that reuses the same
        # event_id in a single transaction. The duplicate event_id must raise
        # and roll back the whole transaction (the SA insert included).
        colliding_event = build_audit_event(
            "iam.service_account.created",
            org_id=ORG_A,
            actor=_actor(),
            resource=_resource(type_="service_account", id_="sa-collide"),
        )
        colliding_event = colliding_event.model_copy(update={"event_id": reused_event.event_id})
        with pytest.raises(IntegrityError):
            async with sf() as session:
                await repo_create(
                    sf,
                    org_id=ORG_A,
                    name="doomed-sa",
                    session=session,
                )
                await enqueue_audit_outbox_in_session(session, colliding_event)
                await session.commit()

        # The business write was rolled back: no ServiceAccount named
        # "doomed-sa" exists. The originally-seeded outbox row remains.
        async with sf() as session:
            sa_rows = (await session.execute(select(ServiceAccountRow).where(ServiceAccountRow.name == "doomed-sa"))).scalars().all()
            outbox_rows = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_A))).scalars().all()
        assert sa_rows == []
        # Exactly the one pre-seeded outbox row — the colliding enqueue did not land.
        assert len(outbox_rows) == 1

    @pytest.mark.anyio
    async def test_business_write_then_audit_commit_atomically(self, sf):
        """The happy path: business write + audit enqueue commit together, and
        after commit both the SA row and a single ``pending`` outbox row exist."""
        event = build_audit_event(
            "iam.service_account.created",
            org_id=ORG_A,
            actor=_actor(),
            resource=_resource(type_="service_account", id_="will-be-set"),
        )
        async with sf() as session:
            await create_service_account(sf, org_id=ORG_A, name="happy-sa", session=session)
            await enqueue_audit_outbox_in_session(session, event)
            await session.commit()
        # Both landed.
        async with sf() as session:
            sa_rows = (await session.execute(select(ServiceAccountRow).where(ServiceAccountRow.name == "happy-sa"))).scalars().all()
            outbox = (await session.execute(select(AuditOutboxRow).where(AuditOutboxRow.org_id == ORG_A))).scalars().all()
        assert len(sa_rows) == 1
        assert len(outbox) == 1
        assert outbox[0].status == "pending"


# ===========================================================================
# ADR §4 action normalization + build_audit_event projection
# ===========================================================================


class TestActionNormalization:
    def test_registry_maps_every_legacy_event_type(self):
        # Every legacy shim event_type the router / bootstrap used must have a
        # normalized entry. If a new emit_tenant_event call site is added
        # without a registry entry, it falls through unchanged — this test
        # pins the known set so a rename is a deliberate change.
        assert TENANT_EVENT_ACTION_REGISTRY["service_account_created"] == "iam.service_account.created"
        assert TENANT_EVENT_ACTION_REGISTRY["api_key_created"] == "iam.api_key.created"
        assert TENANT_EVENT_ACTION_REGISTRY["org_membership_suspended"] == "iam.membership.suspended"
        assert TENANT_EVENT_ACTION_REGISTRY["oidc_group_mapping_created"] == "iam.oidc_group_mapping.created"
        # All normalized actions are lowercase dotted ADR §4 form. ADR §5.1
        # allows a few two-segment exceptions in the minimal-9 list (``auth.login``);
        # everything else is the three-segment ``<domain>.<resource>.<verb>``.
        for normalized in TENANT_EVENT_ACTION_REGISTRY.values():
            assert normalized == normalized.lower()
            assert normalized.count(".") >= 1  # at least <domain>.<verb>

    def test_build_audit_event_projects_resource_actor_outcome(self):
        ev = build_audit_event(
            "iam.role_binding.deleted",
            org_id=ORG_A,
            actor=_actor(),
            outcome="success",
            resource=_resource(type_="role_binding", id_="rb-9"),
            payload={"role_id": "r-admin"},
        )
        assert ev.action == "iam.role_binding.deleted"
        assert ev.outcome == "success"
        assert ev.actor.id == USER_ID
        assert ev.actor.user_id == USER_ID
        assert ev.resource is not None
        assert ev.resource.type == "role_binding"
        assert ev.resource.id == "rb-9"
        assert ev.org_id == ORG_A
        assert ev.event_id  # generated
        assert ev.request_id  # non-empty ("system" when no correlation context)

    def test_build_audit_event_request_id_defaults_to_system(self):
        # Outside a CorrelationContext (no active request), the id is the
        # stable "system" sentinel rather than empty (AuditEvent requires it).
        ev = build_audit_event(
            "iam.service_account.created",
            org_id=ORG_A,
            actor=_actor(),
        )
        assert ev.request_id == "system"

    def test_build_audit_event_occurred_at_is_utc(self):
        ev = build_audit_event("iam.api_key.revoked", org_id=ORG_A, actor=_actor())
        assert ev.occurred_at.tzinfo is not None
        # It is recent (within the last minute of "now").
        assert (datetime.now(UTC) - ev.occurred_at).total_seconds() < 60
