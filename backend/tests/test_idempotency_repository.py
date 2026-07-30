"""Repository tests for the Idempotency-Key replay store (PR-055).

Covers :mod:`deerflow.persistence.release.idempotency` against an isolated
SQLite: ``compute_request_hash`` determinism + identity semantics, the
happy-path ``get`` / ``insert`` round-trip on the caller's session, the
``UNIQUE(org_id, idempotency_key)`` collision (the concurrency fence),
``resolve_idempotency_outcome`` classification (replay / conflict / miss),
and the ``IdempotencyConflictError`` shape.

Fixture conventions mirror ``test_channel_repository.py``: boot an isolated
SQLite via ``init_engine``, yield ``get_session_factory()``, tear down with
``close_engine``.

Repository IDs: ``ART-1600`` series (repository layer, PR-055).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import deerflow.persistence.models  # noqa: F401  — register ORM with Base.metadata
from deerflow.persistence.release import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IdempotencyConflictError,
    compute_request_hash,
    count_idempotency_records,
    delete_idempotency_records_older_than,
    get_idempotency_record,
    insert_idempotency_record,
    resolve_idempotency_outcome,
)

ORG_ID = "org-test"
OTHER_ORG_ID = "org-other"

pytestmark = pytest.mark.anyio


@pytest.fixture
async def sf(tmp_path: Path):
    from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

    url = f"sqlite+aiosqlite:///{tmp_path / 'idem_repo.db'}"
    await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
    try:
        yield get_session_factory()
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# compute_request_hash (ART-1600)
# ---------------------------------------------------------------------------


class TestRequestHash:
    def test_identical_requests_hash_equal(self):
        kwargs = dict(
            action="promote",
            package_id="pkg-1",
            channel="prod",
            target_version_id="v-1",
            workspace_id=None,
            reason="ship it",
        )
        assert compute_request_hash(**kwargs) == compute_request_hash(**kwargs)

    def test_different_target_version_differs(self):
        common = dict(
            action="promote",
            package_id="pkg-1",
            channel="prod",
            workspace_id=None,
            reason=None,
        )
        h1 = compute_request_hash(target_version_id="v-1", **common)
        h2 = compute_request_hash(target_version_id="v-2", **common)
        assert h1 != h2

    def test_different_action_differs(self):
        common = dict(
            package_id="pkg-1",
            channel="prod",
            target_version_id="v-1",
            workspace_id=None,
            reason=None,
        )
        assert compute_request_hash(action="promote", **common) != compute_request_hash(
            action="rollback",
            **common,
        )

    def test_expected_channel_version_excluded_from_hash(self):
        """A retry after a CAS miss sends a new expected version — must NOT perturb the hash.

        This is the load-bearing identity rule: a client retrying the same
        logical promote (same target / package / channel) must replay the
        original result, not conflict. ``expected_channel_version`` is a
        per-attempt CAS predicate, not request identity.
        """
        common = dict(
            action="promote",
            package_id="pkg-1",
            channel="prod",
            target_version_id="v-1",
            workspace_id=None,
            reason="ship it",
        )
        # The hash function takes no expected_channel_version arg at all —
        # verify by confirming the signature rejects it.
        with pytest.raises(TypeError):
            compute_request_hash(expected_channel_version=2, **common)  # type: ignore[call-arg]

    def test_none_reason_and_empty_reason_differ(self):
        """None vs "" are semantically different (explicit empty vs omitted)."""
        common = dict(
            action="promote",
            package_id="pkg-1",
            channel="prod",
            target_version_id="v-1",
            workspace_id=None,
        )
        assert compute_request_hash(reason=None, **common) != compute_request_hash(reason="", **common)

    def test_reason_text_change_differs(self):
        common = dict(
            action="promote",
            package_id="pkg-1",
            channel="prod",
            target_version_id="v-1",
            workspace_id=None,
        )
        assert compute_request_hash(reason="hotfix", **common) != compute_request_hash(
            reason="hotfix v2",
            **common,
        )

    def test_returns_sha256_hex(self):
        h = compute_request_hash(
            action="promote",
            package_id="p",
            channel="dev",
            target_version_id="v",
            workspace_id=None,
            reason=None,
        )
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# get / insert round-trip (ART-1610)
# ---------------------------------------------------------------------------


class TestGetInsert:
    async def test_get_returns_none_when_absent(self, sf):
        async with sf() as session:
            record = await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="missing")
        assert record is None

    async def test_insert_then_get_returns_record(self, sf):
        payload = {"channel": {"id": "ch-1", "row_version": 2}, "event": {"id": "ev-1"}}
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="key-1",
                request_hash="hash-1",
                response_payload=payload,
                status_code=200,
                record_id="rec-1",
            )
            await session.commit()
        async with sf() as session:
            record = await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="key-1")
        assert record is not None
        assert record.id == "rec-1"
        assert record.org_id == ORG_ID
        assert record.idempotency_key == "key-1"
        assert record.request_hash == "hash-1"
        assert record.response_payload == payload
        assert record.status_code == 200

    async def test_get_is_org_scoped(self, sf):
        """Cross-Org existence-hiding: same key in another Org is invisible."""
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="shared",
                request_hash="h",
                response_payload={},
                status_code=200,
                record_id="rec-a",
            )
            await session.commit()
        async with sf() as session:
            record = await get_idempotency_record(session, org_id=OTHER_ORG_ID, idempotency_key="shared")
        assert record is None

    async def test_insert_collision_raises_integrity_error(self, sf):
        """Same (org, key) insert must raise IntegrityError — the fence the router relies on."""
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="collide",
                request_hash="h-1",
                response_payload={},
                status_code=200,
                record_id="rec-c1",
            )
            await session.commit()
        async with sf() as session:
            with pytest.raises(IntegrityError):
                await insert_idempotency_record(
                    session,
                    org_id=ORG_ID,
                    idempotency_key="collide",  # same org + key
                    request_hash="h-2",
                    response_payload={},
                    status_code=200,
                    record_id="rec-c2",
                )
                await session.flush()

    async def test_get_requires_org_id(self, sf):
        async with sf() as session:
            with pytest.raises(ValueError):
                await get_idempotency_record(session, org_id="", idempotency_key="k")

    async def test_get_requires_idempotency_key(self, sf):
        async with sf() as session:
            with pytest.raises(ValueError):
                await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="")


# ---------------------------------------------------------------------------
# resolve_idempotency_outcome (ART-1620) — the race classifier
# ---------------------------------------------------------------------------


class TestResolveOutcome:
    async def test_replay_when_same_request_hash(self, sf):
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="k-replay",
                request_hash="hash-X",
                response_payload={"channel": {"row_version": 5}},
                status_code=200,
                record_id="rec-r1",
            )
            await session.commit()
        outcome, record = await resolve_idempotency_outcome(
            sf,
            org_id=ORG_ID,
            idempotency_key="k-replay",
            request_hash="hash-X",
        )
        assert outcome == "replay"
        assert record is not None
        assert record.request_hash == "hash-X"

    async def test_conflict_when_different_request_hash(self, sf):
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="k-conflict",
                request_hash="hash-A",
                response_payload={},
                status_code=200,
                record_id="rec-r2",
            )
            await session.commit()
        outcome, record = await resolve_idempotency_outcome(
            sf,
            org_id=ORG_ID,
            idempotency_key="k-conflict",
            request_hash="hash-B",
        )
        assert outcome == "conflict"
        assert record is None

    async def test_miss_when_no_record(self, sf):
        """The winner rolled back (rare) — caller retries the whole request."""
        outcome, record = await resolve_idempotency_outcome(
            sf,
            org_id=ORG_ID,
            idempotency_key="never-inserted",
            request_hash="hash-Z",
        )
        assert outcome == "miss"
        assert record is None


# ---------------------------------------------------------------------------
# GC / TTL prune (ART-1650) — §16.56 follow-up
# ---------------------------------------------------------------------------


async def _insert_record_with_age(
    sf,
    *,
    org_id: str,
    idempotency_key: str,
    age_seconds: float,
    record_id: str,
) -> None:
    """Insert a replay record back-dated by ``age_seconds`` from now (for GC tests)."""
    from deerflow.persistence.release.model import ReleaseIdempotencyRecordRow

    created = datetime.now(UTC) - timedelta(seconds=age_seconds)
    async with sf() as session:
        row = ReleaseIdempotencyRecordRow(
            id=record_id,
            org_id=org_id,
            idempotency_key=idempotency_key,
            request_hash="h",
            response_payload={},
            status_code=200,
            created_at=created,
        )
        session.add(row)
        await session.commit()


class TestGC:
    async def test_count_all_when_empty(self, sf):
        assert await count_idempotency_records(sf) == 0

    async def test_count_all_after_inserts(self, sf):
        async with sf() as session:
            await insert_idempotency_record(
                session,
                org_id=ORG_ID,
                idempotency_key="k-1",
                request_hash="h",
                response_payload={},
                status_code=200,
                record_id="rec-1",
            )
            await session.commit()
        assert await count_idempotency_records(sf) == 1

    async def test_count_older_than_filters_by_created_at(self, sf):
        await _insert_record_with_age(sf, org_id=ORG_ID, idempotency_key="old", age_seconds=3600, record_id="rec-old")
        await _insert_record_with_age(sf, org_id=ORG_ID, idempotency_key="new", age_seconds=10, record_id="rec-new")
        cutoff = datetime.now(UTC) - timedelta(seconds=1800)  # between old (3600) and new (10)
        assert await count_idempotency_records(sf, older_than=cutoff) == 1

    async def test_delete_older_than_removes_only_stale(self, sf):
        await _insert_record_with_age(sf, org_id=ORG_ID, idempotency_key="old", age_seconds=3600, record_id="rec-old")
        await _insert_record_with_age(sf, org_id=ORG_ID, idempotency_key="new", age_seconds=10, record_id="rec-new")
        cutoff = datetime.now(UTC) - timedelta(seconds=1800)
        removed = await delete_idempotency_records_older_than(sf, cutoff=cutoff)
        assert removed == 1
        # The fresh record survives; the stale one is gone.
        assert await count_idempotency_records(sf) == 1
        async with sf() as session:
            assert await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="new") is not None
            assert await get_idempotency_record(session, org_id=ORG_ID, idempotency_key="old") is None

    async def test_delete_older_than_is_org_agnostic(self, sf):
        """GC prunes across all Orgs (replay records are self-contained; retention is global)."""
        await _insert_record_with_age(sf, org_id=ORG_ID, idempotency_key="old-a", age_seconds=3600, record_id="rec-a")
        await _insert_record_with_age(sf, org_id=OTHER_ORG_ID, idempotency_key="old-b", age_seconds=3600, record_id="rec-b")
        cutoff = datetime.now(UTC) - timedelta(seconds=1800)
        removed = await delete_idempotency_records_older_than(sf, cutoff=cutoff)
        assert removed == 2

    async def test_delete_older_than_with_no_stale_returns_zero(self, sf):
        await _insert_record_with_age(sf, org_id=ORG_ID, idempotency_key="fresh", age_seconds=5, record_id="rec-f")
        cutoff = datetime.now(UTC) - timedelta(days=30)
        assert await delete_idempotency_records_older_than(sf, cutoff=cutoff) == 0

    async def test_delete_requires_tz_aware_cutoff(self, sf):
        naive = datetime.now()  # no tzinfo
        with pytest.raises(ValueError):
            await delete_idempotency_records_older_than(sf, cutoff=naive)


# ---------------------------------------------------------------------------
# IdempotencyConflictError shape (ART-1630)
# ---------------------------------------------------------------------------


class TestIdempotencyConflictError:
    def test_carries_org_and_key(self):
        err = IdempotencyConflictError(org_id="org-1", idempotency_key="key-1")
        assert err.org_id == "org-1"
        assert err.idempotency_key == "key-1"
        assert "key-1" in str(err)
        assert "org-1" in str(err)

    def test_is_exception_subclass(self):
        err = IdempotencyConflictError(org_id="o", idempotency_key="k")
        assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# Header constant sanity (ART-1640)
# ---------------------------------------------------------------------------


class TestConstants:
    def test_header_name(self):
        from deerflow.persistence.release import IDEMPOTENCY_KEY_HEADER

        assert IDEMPOTENCY_KEY_HEADER == "Idempotency-Key"

    def test_max_length_matches_column_width(self):
        assert IDEMPOTENCY_KEY_MAX_LENGTH == 128
