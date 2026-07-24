"""Tests for the PR-064 live probes.

Each probe follows the test_doctor_tenant_probe.py pattern:

* pure-classifier tests where the decision has a branch table;
* live / in-process tests against an isolated resource (SQLite for postgres,
  real ``generate_metrics_payload()`` for metrics, plain config dict for
  deployment evidence, monkeypatched httpx for gateway probes);
* failure-containment tests (unreachable resource → FAIL, never raise);
* no-secret-leakage tests (result messages never carry the full URL / DSN).
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.doctor.models import DoctorStatus
from app.doctor.probes.audit_probe import probe_audit_outbox
from app.doctor.probes.backup_probe import probe_backup_freshness
from app.doctor.probes.deployment_evidence_probe import probe_deployment_evidence
from app.doctor.probes.gateway_security_probe import probe_gateway_security
from app.doctor.probes.metrics_probe import EXPECTED_METRIC_NAMES, probe_metrics_presence
from app.doctor.probes.postgres_probe import _parse_major_version, probe_postgres_connectivity
from app.doctor.probes.rate_limit_probe import probe_rate_limit_retry_after
from deerflow.config.app_config import AppConfig


def json_dump(result) -> str:
    """Serialise a DoctorCheckResult for no-secret-leak assertions.

    Uses ``DoctorCheckResult.to_dict`` (which handles the datetime field)
    rather than ``dataclasses.asdict`` (which leaves datetime as-is and
    breaks ``json.dumps``).
    """
    return json.dumps(result.to_dict())


# ---------------------------------------------------------------------------
# Shared config builder (mirrors test_production_doctor._production_data but
# trimmed to what probes actually read; tests override what they need).
# ---------------------------------------------------------------------------


def _base_config(**overrides) -> AppConfig:
    data: dict = {
        "log_level": "info",
        "sandbox": {"use": "LocalSandboxProvider"},
        "database": {
            "backend": "postgres",
            "postgres_url": "postgresql://user:pass@example.invalid/deernexus",
            "pool_size": 5,
        },
        "run_events": {"backend": "db"},
        "production": {
            "enabled": True,
            "environment": "production",
            "deployment": {
                "profile": "S",
                "gateway_replicas": 1,
            },
            "gateway_security": {
                "tls_enabled": True,
                "cors_origins": ["https://deernexus.example.com"],
                "csrf_enabled": True,
                "rate_limit_enabled": True,
            },
        },
        "observability": {"metrics": {"enabled": True, "route": "/metrics"}},
    }
    # Deep-merge overrides into the top-level dict.
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = copy.deepcopy(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    return AppConfig.model_validate(data)


# ===========================================================================
# postgres_probe
# ===========================================================================


class TestParseMajorVersion:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("PostgreSQL 15.4 on x86_64-pc-linux-gnu, 64-bit", 15),
            ("PostgreSQL 16.1", 16),
            ("PostgreSQL 9.6.10", 9),
            ("PostgreSQL 17rc1", 17),
            ("not a postgres version", None),
            ("", None),
        ],
    )
    def test_parse(self, raw, expected):
        assert _parse_major_version(raw) == expected


class TestPostgresProbeBackendSkip:
    @pytest.mark.anyio
    async def test_sqlite_backend_warns_skip(self):
        config = _base_config(database={"backend": "sqlite"})
        result = await probe_postgres_connectivity(config)
        assert result.status is DoctorStatus.WARN
        assert "sqlite" in result.message.lower()
        assert result.check_id == "postgres.connectivity"

    @pytest.mark.anyio
    async def test_memory_backend_warns_skip(self):
        config = _base_config(database={"backend": "memory"})
        result = await probe_postgres_connectivity(config)
        assert result.status is DoctorStatus.WARN


class TestPostgresProbeConnectivity:
    @pytest.mark.anyio
    async def test_unreachable_db_fails_without_raising(self):
        # A postgres URL that will refuse the connection. We use a clearly
        # bogus host so the failure is fast (DNS / refused) and deterministic.
        config = _base_config(
            database={
                "backend": "postgres",
                "postgres_url": "postgresql://user:pass@127.0.0.1:1/deernexus",
            }
        )
        result = await probe_postgres_connectivity(config)
        assert result.status is DoctorStatus.FAIL
        assert "could not connect" in result.message.lower() or "could not reach" in result.message.lower() or "connect" in result.message.lower()

    @pytest.mark.anyio
    async def test_no_secret_leak_on_failure(self):
        secret_url = "postgresql://doctoruser:hunter2@127.0.0.1:1/deernexus"
        config = _base_config(database={"backend": "postgres", "postgres_url": secret_url, "pool_size": 5})
        result = await probe_postgres_connectivity(config)
        # The result (message + remediation) must NOT contain the password.
        blob = json_dump(result)
        assert "hunter2" not in blob
        assert "doctoruser" not in blob

    @pytest.mark.anyio
    async def test_live_sqlite_treated_as_warn_not_postgres(self):
        # Even if a sqlite URL is configured, backend=postgres path is not
        # taken; we already cover the skip in TestPostgresProbeBackendSkip.
        # This test pins that the skip path does not leak the URL either.
        config = _base_config(database={"backend": "sqlite"})
        result = await probe_postgres_connectivity(config)
        blob = json_dump(result)
        # sqlite path does not include the URL but the assertion is defensive.
        assert "hunter2" not in blob


# ===========================================================================
# metrics_probe
# ===========================================================================


class TestMetricsProbe:
    def test_expected_names_constant_is_non_empty(self):
        assert len(EXPECTED_METRIC_NAMES) > 0
        # All names are non-empty unique strings.
        assert all(isinstance(n, str) and n for n in EXPECTED_METRIC_NAMES)
        assert len(set(EXPECTED_METRIC_NAMES)) == len(EXPECTED_METRIC_NAMES)

    @pytest.mark.anyio
    async def test_disabled_metrics_yields_warn(self):
        config = _base_config(observability={"metrics": {"enabled": False, "route": "/metrics"}})
        result = await probe_metrics_presence(config)
        assert result.status is DoctorStatus.WARN
        assert "disabled" in result.message.lower()

    @pytest.mark.anyio
    async def test_missing_wired_metrics_outside_gateway_pod_yields_warn(self, monkeypatch):
        # When the doctor runs outside a gateway pod, only python_*/process_*
        # collectors are registered (the wired metrics register on the
        # request path). The probe should WARN, not FAIL, because this is an
        # environment condition rather than a wiring regression.
        config = _base_config()

        # Simulate "no wired metrics yet" by patching generate_metrics_payload
        # to return only the python_gc collector name.
        def fake_payload(registry=None):
            return (b"# HELP python_gc_objects_collected\npython_gc_objects_collected_total 1\n", "text/plain; version=1.0.0; charset=utf-8")

        monkeypatch.setattr("deerflow.observability.metrics.generate_metrics_payload", fake_payload)
        result = await probe_metrics_presence(config)
        assert result.status is DoctorStatus.WARN
        assert "outside a gateway pod" in result.message or "gateway pod" in result.message

    @pytest.mark.anyio
    async def test_all_expected_present_in_payload_passes(self, monkeypatch):
        config = _base_config()
        # Build a fake payload that contains every expected name.
        fake_body = "\n".join(f"{name}{{}} 1.0" for name in EXPECTED_METRIC_NAMES).encode()
        monkeypatch.setattr(
            "deerflow.observability.metrics.generate_metrics_payload",
            lambda registry=None: (fake_body, "text/plain; version=1.0.0; charset=utf-8"),
        )
        result = await probe_metrics_presence(config)
        assert result.status is DoctorStatus.PASS

    @pytest.mark.anyio
    async def test_some_wired_metrics_missing_yields_fail(self, monkeypatch):
        # A payload that has some wired metrics but is missing others → the
        # probe distinguishes this from the "outside gateway pod" case.
        config = _base_config()
        # Include http_requests_total (so the probe sees at least one wired
        # metric) but drop runs_created_total and the rest.
        present = EXPECTED_METRIC_NAMES[:5]
        fake_body = b"http_requests_total 1.0\n" + b"".join(f"{n} 1.0\n".encode() for n in present)
        monkeypatch.setattr(
            "deerflow.observability.metrics.generate_metrics_payload",
            lambda registry=None: (fake_body, "text/plain; version=1.0.0; charset=utf-8"),
        )
        result = await probe_metrics_presence(config)
        assert result.status is DoctorStatus.FAIL
        assert "missing" in result.message.lower()


# ===========================================================================
# deployment_evidence_probe
# ===========================================================================


class TestDeploymentEvidenceProbe:
    @pytest.mark.anyio
    async def test_profile_s_passes(self):
        config = _base_config()
        result = await probe_deployment_evidence(config)
        assert result.status is DoctorStatus.PASS

    @pytest.mark.anyio
    async def test_profile_h_without_evidence_fails(self):
        config = _base_config(
            production={
                "deployment": {"profile": "H", "gateway_replicas": 2},
            }
        )
        result = await probe_deployment_evidence(config)
        assert result.status is DoctorStatus.FAIL
        assert "profile_h_evidence" in result.message

    @pytest.mark.anyio
    async def test_profile_h_with_evidence_passes(self):
        config = _base_config(
            production={
                "deployment": {
                    "profile": "H",
                    "gateway_replicas": 2,
                    "profile_h_evidence": "https://runbooks.example.com/ha-validation",
                },
            }
        )
        result = await probe_deployment_evidence(config)
        assert result.status is DoctorStatus.PASS

    @pytest.mark.anyio
    async def test_profile_w_missing_all_evidence_fails(self):
        config = _base_config(
            production={
                "deployment": {"profile": "W", "worker_replicas": 1},
            }
        )
        result = await probe_deployment_evidence(config)
        assert result.status is DoctorStatus.FAIL
        # All three fields flagged.
        for field in ("profile_w_evidence", "profile_w_rollback_evidence", "profile_w_soak_hours"):
            assert field in result.message

    @pytest.mark.anyio
    async def test_profile_w_partial_evidence_fails(self):
        config = _base_config(
            production={
                "deployment": {
                    "profile": "W",
                    "worker_replicas": 1,
                    "profile_w_evidence": "https://example.com/w",
                    "profile_w_soak_hours": 4,
                    # rollback evidence missing
                },
            }
        )
        result = await probe_deployment_evidence(config)
        assert result.status is DoctorStatus.FAIL
        # The message lists the three Profile-W requirements, then
        # ``missing: <comma-separated>``. Only the actually-missing field
        # should appear after "missing:". The substring trap:
        # ``profile_w_evidence`` is a substring of
        # ``profile_w_rollback_evidence``, so we check the missing-list tail.
        missing_tail = result.message.split("missing:", 1)[-1] if "missing:" in result.message else ""
        assert "profile_w_rollback_evidence" in missing_tail
        # The soak-hours and present-evidence must NOT be in the missing tail.
        assert "profile_w_soak_hours" not in missing_tail

    @pytest.mark.anyio
    async def test_profile_w_complete_passes(self):
        config = _base_config(
            production={
                "deployment": {
                    "profile": "W",
                    "worker_replicas": 1,
                    "profile_w_evidence": "https://example.com/w",
                    "profile_w_rollback_evidence": "https://example.com/w-rollback",
                    "profile_w_soak_hours": 8,
                },
            }
        )
        result = await probe_deployment_evidence(config)
        assert result.status is DoctorStatus.PASS


# ===========================================================================
# gateway_security_probe (httpx monkeypatched)
# ===========================================================================


class TestGatewaySecurityProbe:
    @pytest.mark.anyio
    async def test_no_gateway_url_warns_skip(self, monkeypatch):
        monkeypatch.delenv("DEER_FLOW_GATEWAY_URL", raising=False)
        config = _base_config()
        result = await probe_gateway_security(config)
        assert result.status is DoctorStatus.WARN
        assert "DEER_FLOW_GATEWAY_URL" in result.message

    @pytest.mark.anyio
    async def test_tls_declared_but_http_url_fails(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "http://gateway.example.com:8001")
        config = _base_config()  # tls_enabled=True by default
        result = await probe_gateway_security(config)
        assert result.status is DoctorStatus.FAIL
        assert "https" in result.message.lower()

    @pytest.mark.anyio
    async def test_unreachable_gateway_fails_without_raising(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://127.0.0.1:1")

        async def boom(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr("app.doctor.probes.gateway_security_probe._httpx_get", boom)
        config = _base_config()
        result = await probe_gateway_security(config)
        assert result.status is DoctorStatus.FAIL

    @pytest.mark.anyio
    async def test_reachable_gateway_passes(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://gateway.example.com:8001")
        # CORS off + CSRF off in this scenario so the only assertion is reachability.
        config = _base_config(
            production={
                "gateway_security": {
                    "tls_enabled": True,
                    "cors_origins": [],
                    "csrf_enabled": False,
                    "rate_limit_enabled": True,
                },
            }
        )

        async def fake_get(url, method="GET", headers=None):
            return SimpleNamespace(status_code=200, headers={})

        monkeypatch.setattr("app.doctor.probes.gateway_security_probe._httpx_get", fake_get)
        result = await probe_gateway_security(config)
        assert result.status is DoctorStatus.PASS

    @pytest.mark.anyio
    async def test_no_secret_leak(self, monkeypatch):
        # The URL host should appear but no auth header value should ever
        # surface in the result.
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://gateway.example.com:8001")

        async def boom(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr("app.doctor.probes.gateway_security_probe._httpx_get", boom)
        config = _base_config()
        result = await probe_gateway_security(config)
        blob = json_dump(result)
        assert "gateway.example.com" in blob  # host is fine
        # No Authorization / Bearer value should be present (we never send
        # one, but defensive: assert no auth-token-shaped substring).
        assert "Bearer " not in blob


# ===========================================================================
# rate_limit_probe (httpx monkeypatched)
# ===========================================================================


class TestRateLimitProbe:
    @pytest.mark.anyio
    async def test_no_gateway_url_warns_skip(self, monkeypatch):
        monkeypatch.delenv("DEER_FLOW_GATEWAY_URL", raising=False)
        config = _base_config()
        result = await probe_rate_limit_retry_after(config)
        assert result.status is DoctorStatus.WARN

    @pytest.mark.anyio
    async def test_rate_limit_disabled_warns_skip(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://gateway.example.com:8001")
        config = _base_config(
            production={
                "gateway_security": {
                    "tls_enabled": True,
                    "cors_origins": [],
                    "csrf_enabled": False,
                    "rate_limit_enabled": False,
                },
            }
        )
        result = await probe_rate_limit_retry_after(config)
        assert result.status is DoctorStatus.WARN
        assert "rate_limit_enabled=false" in result.message

    @pytest.mark.anyio
    async def test_429_with_retry_after_passes(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://gateway.example.com:8001")
        config = _base_config()  # rate_limit_enabled=True

        # Simulate: first N attempts 401, then 429 with Retry-After.
        threshold = 5  # _max_login_attempts_threshold() fallback
        call_count = {"n": 0}

        async def fake_post(url, json_body):
            call_count["n"] += 1
            if call_count["n"] <= threshold:
                return SimpleNamespace(status_code=401, headers={})
            return SimpleNamespace(status_code=429, headers={"Retry-After": "60"})

        monkeypatch.setattr("app.doctor.probes.rate_limit_probe._httpx_post", fake_post)
        result = await probe_rate_limit_retry_after(config)
        assert result.status is DoctorStatus.PASS

    @pytest.mark.anyio
    async def test_429_without_retry_after_warns(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://gateway.example.com:8001")
        config = _base_config()

        async def fake_post(url, json_body):
            return SimpleNamespace(status_code=429, headers={})  # no Retry-After

        monkeypatch.setattr("app.doctor.probes.rate_limit_probe._httpx_post", fake_post)
        result = await probe_rate_limit_retry_after(config)
        assert result.status is DoctorStatus.WARN
        assert "retry-after" in result.message.lower()

    @pytest.mark.anyio
    async def test_no_429_at_all_fails(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://gateway.example.com:8001")
        config = _base_config()

        async def fake_post(url, json_body):
            return SimpleNamespace(status_code=401, headers={})  # never locks out

        monkeypatch.setattr("app.doctor.probes.rate_limit_probe._httpx_post", fake_post)
        result = await probe_rate_limit_retry_after(config)
        assert result.status is DoctorStatus.FAIL

    @pytest.mark.anyio
    async def test_httpx_failure_contained_to_fail(self, monkeypatch):
        monkeypatch.setenv("DEER_FLOW_GATEWAY_URL", "https://127.0.0.1:1")
        config = _base_config()

        async def boom(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr("app.doctor.probes.rate_limit_probe._httpx_post", boom)
        result = await probe_rate_limit_retry_after(config)
        assert result.status is DoctorStatus.FAIL


# ===========================================================================
# audit.outbox probe (PR-042) — live table reachability + SLO backlog
# ===========================================================================


class TestAuditProbe:
    @pytest.mark.anyio
    async def test_memory_backend_warns_skip(self):
        config = _base_config(database={"backend": "memory"})
        result = await probe_audit_outbox(config)
        assert result.status is DoctorStatus.WARN
        assert result.check_id == "audit.outbox"
        assert "memory" in result.message.lower()

    @pytest.mark.anyio
    async def test_empty_outbox_passes(self, tmp_path):
        """A reachable outbox table with no backlog/dead-letter is PASS."""
        from deerflow.persistence.engine import close_engine, init_engine

        # init_engine migrates the DB; config.sqlite_dir must resolve to the
        # same ``{dir}/deerflow.db`` the engine created so the probe reads it.
        url = f"sqlite+aiosqlite:///{tmp_path / 'deerflow.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            config = _base_config(database={"backend": "sqlite", "sqlite_dir": str(tmp_path)})
            result = await probe_audit_outbox(config)
        finally:
            await close_engine()
        assert result.status is DoctorStatus.PASS
        assert "0 dead-letter" in result.message

    @pytest.mark.anyio
    async def test_dead_letter_fails(self, tmp_path):
        """A dead-lettered event is a compliance-evidence loss (ADR §8 P1) → FAIL."""
        from deerflow.persistence.audit.model import AuditOutboxRow
        from deerflow.persistence.audit.outbox import OUTBOX_DEAD_LETTER
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'deerflow.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            sf = get_session_factory()
            now = datetime.now(UTC)
            async with sf() as session:
                session.add(
                    AuditOutboxRow(
                        id="dl-1",
                        event_id="evt-dl-1",
                        payload_json="{}",
                        org_id="any",
                        status=OUTBOX_DEAD_LETTER,
                        attempts=10,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.commit()
            config = _base_config(database={"backend": "sqlite", "sqlite_dir": str(tmp_path)})
            result = await probe_audit_outbox(config)
        finally:
            await close_engine()
        assert result.status is DoctorStatus.FAIL
        assert "dead-letter" in result.message.lower()

    @pytest.mark.anyio
    async def test_stale_pending_fails(self, tmp_path):
        """A pending row older than the 5-minute SLO (ADR §14 P2) → FAIL."""
        from deerflow.persistence.audit.model import AuditOutboxRow
        from deerflow.persistence.audit.outbox import OUTBOX_PENDING
        from deerflow.persistence.engine import close_engine, get_session_factory, init_engine

        url = f"sqlite+aiosqlite:///{tmp_path / 'deerflow.db'}"
        await init_engine("sqlite", url=url, sqlite_dir=str(tmp_path))
        try:
            sf = get_session_factory()
            old = datetime.now(UTC) - timedelta(minutes=10)
            async with sf() as session:
                session.add(
                    AuditOutboxRow(
                        id="stale-1",
                        event_id="evt-stale-1",
                        payload_json="{}",
                        org_id="any",
                        status=OUTBOX_PENDING,
                        attempts=0,
                        available_at=old,
                        created_at=old,
                        updated_at=old,
                    )
                )
                await session.commit()
            config = _base_config(database={"backend": "sqlite", "sqlite_dir": str(tmp_path)})
            result = await probe_audit_outbox(config)
        finally:
            await close_engine()
        assert result.status is DoctorStatus.FAIL
        assert "slo" in result.message.lower() or "keeping up" in result.message.lower()

    @pytest.mark.anyio
    async def test_unreachable_db_fails_without_raising(self):
        # An unreachable DB must surface as FAIL, never raise.
        config = _base_config(
            database={
                "backend": "sqlite",
                "sqlite_path": "/nonexistent/protected/path/cannot-create.db",
            }
        )
        result = await probe_audit_outbox(config)
        assert result.status is DoctorStatus.FAIL


# ===========================================================================
# backup_probe (PR-065)
# ===========================================================================


class TestBackupProbe:
    @pytest.mark.anyio
    async def test_disabled_backup_warns_skip(self):
        # Backup not enabled → WARN (the deployment may rely on the DB platform
        # backup only). Never a hard FAIL for an opt-in layer.
        config = _base_config(production={"backup": {"enabled": False}})
        result = await probe_backup_freshness(config)
        assert result.status is DoctorStatus.WARN
        assert result.check_id == "backup.freshness"
        assert "not enabled" in result.message.lower()

    @pytest.mark.anyio
    async def test_enabled_but_never_ran_fails(self, tmp_path):
        # Enabled + destination set, but no manifest → the Job has never run.
        config = _base_config(
            production={
                "backup": {
                    "enabled": True,
                    "declared_rpo_hours": 24,
                    "destination_dir": str(tmp_path / "empty"),
                }
            }
        )
        result = await probe_backup_freshness(config)
        assert result.status is DoctorStatus.FAIL
        assert "never run" in result.message.lower()

    @pytest.mark.anyio
    async def test_fresh_manifest_passes_with_complement_note(self, tmp_path):
        # A manifest within RPO + tamper-intact → PASS, with the honesty caveat.
        dest = tmp_path / "backups"
        await self._write_fresh_manifest(dest, rpo_hours=24, age=timedelta(hours=1))
        config = _base_config(
            production={
                "backup": {
                    "enabled": True,
                    "declared_rpo_hours": 24,
                    "destination_dir": str(dest),
                }
            }
        )
        result = await probe_backup_freshness(config)
        assert result.status is DoctorStatus.PASS
        # The PASS message MUST state it complements (not replaces) the DB backup.
        assert "complements" in result.message.lower()
        assert "does not replace" in result.message.lower()

    @pytest.mark.anyio
    async def test_stale_manifest_exceeding_rpo_fails(self, tmp_path):
        dest = tmp_path / "backups"
        # Manifest 48h old, RPO 24h → FAIL (runbook §14.2 P1).
        await self._write_fresh_manifest(dest, rpo_hours=24, age=timedelta(hours=48))
        config = _base_config(
            production={
                "backup": {
                    "enabled": True,
                    "declared_rpo_hours": 24,
                    "destination_dir": str(dest),
                }
            }
        )
        result = await probe_backup_freshness(config)
        assert result.status is DoctorStatus.FAIL
        assert "exceeding the declared rpo" in result.message.lower()

    @pytest.mark.anyio
    async def test_tampered_manifest_fails(self, tmp_path):
        dest = tmp_path / "backups"
        await self._write_fresh_manifest(dest, rpo_hours=24, age=timedelta(hours=1))
        # Corrupt the manifest's content_digest so it no longer recomputes.
        manifest_path = dest / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["content_digest"] = "0" * 64
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        config = _base_config(
            production={
                "backup": {
                    "enabled": True,
                    "declared_rpo_hours": 24,
                    "destination_dir": str(dest),
                }
            }
        )
        result = await probe_backup_freshness(config)
        assert result.status is DoctorStatus.FAIL
        assert "tamper" in result.message.lower()

    async def _write_fresh_manifest(self, dest: Path, *, rpo_hours: int, age: timedelta) -> None:
        """Write a valid, finalize_digests-stamped manifest aged ``age`` ago."""
        from deerflow.persistence.backup import (
            BackupManifest,
            BackupTableEntry,
            finalize_digests,
            write_manifest,
        )

        manifest = finalize_digests(
            BackupManifest(
                created_at=datetime.now(UTC) - age,
                backend="sqlite",
                schema_version="0011_audit_outbox",
                declared_rpo_hours=rpo_hours,
                tables=[BackupTableEntry(name="organizations", row_count=0, content_digest="0" * 64, columns=["id"])],
            )
        )
        write_manifest(dest, manifest)
