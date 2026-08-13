"""Live object-storage security probe for the production doctor (T1B).

Implements the ``object_storage.security`` check: when the backend is
``s3``, it performs a real put/get/delete round-trip on a throwaway probe
key to verify the bucket is writable, readable, and that SSE encryption is
applied (ADR §11.2 "静态加密"). When the backend is ``inline`` (dev /
single-node), the check WARN-skips — ``InlineObjectStore`` has no external
I/O to probe.

Promoted from the pre-T1B ``DEFERRED_LIVE_CHECKS`` stub. No-secret
guarantee: the result message carries only the bucket name (not keys,
not credentials).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "object_storage.security"
_COMPONENT = "storage"


async def probe_object_storage_security(config: AppConfig) -> DoctorCheckResult:
    """Probe the object store for write/read/encryption capability.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    artifact_cfg = getattr(config.production, "artifact", None)
    if artifact_cfg is None:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.WARN,
            component=_COMPONENT,
            message="object_storage.security skipped: production.artifact is not configured.",
            remediation="Configure production.artifact for production deployments.",
            config_source="config.yaml:production.artifact",
        )

    backend = getattr(artifact_cfg, "object_store_backend", "inline")
    if backend == "inline":
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.WARN,
            component=_COMPONENT,
            message=("object_storage.security skipped: backend is 'inline' (content_inline is the source of truth, no external infra). Production deployments requiring large artifacts must set object_store_backend='s3'."),
            remediation=("Set production.artifact.object_store_backend='s3' and configure the s3_* fields for production deployments with large artifacts."),
            config_source="config.yaml:production.artifact.object_store_backend",
        )

    if backend != "s3":
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=f"Unknown object_store_backend: {backend!r}.",
            remediation="Set object_store_backend to 'inline' or 's3'.",
            config_source="config.yaml:production.artifact.object_store_backend",
        )

    # s3 backend — construct a store and do a round-trip.
    from deerflow.persistence.release.storage import build_object_store

    try:
        store = build_object_store(artifact_cfg)
    except Exception:  # noqa: BLE001 — contain any construction failure into FAIL
        logger.warning("object_storage probe: failed to construct S3ObjectStore", exc_info=True)
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=(f"Could not initialize the S3 object store (bucket={artifact_cfg.s3_bucket!r}). The bucket is not accessible, credentials are invalid, or the s3 optional dependency is not installed."),
            remediation=("Verify the s3_* fields, bucket existence, credentials, and that 'deerflow-harness[s3]' is installed. Re-run doctor after fixing."),
            config_source="config.yaml:production.artifact",
        )

    bucket_label = artifact_cfg.s3_bucket or "<unknown>"
    probe_key = f"__doctor_probe__/{uuid.uuid4().hex}"
    probe_body = b"doctor-probe"

    try:
        await store.put(object_key=probe_key, content=probe_body)
        got = await store.get(object_key=probe_key)
        if got != probe_body:
            return DoctorCheckResult(
                check_id=_CHECK_ID,
                status=DoctorStatus.FAIL,
                component=_COMPONENT,
                message=f"Object store round-trip returned mismatched bytes (bucket={bucket_label}).",
                remediation="Investigate the object store integrity (proxy / corruption / encoding).",
                config_source="config.yaml:production.artifact",
            )
        await store.delete(object_key=probe_key)
    except Exception:  # noqa: BLE001 — contain any I/O failure into FAIL
        logger.warning("object_storage probe: round-trip failed", exc_info=True)
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=(f"Object store round-trip failed (bucket={bucket_label}): put/get/delete did not complete. The bucket may be read-only, the credentials may lack permissions, or the endpoint is unreachable."),
            remediation=("Verify the bucket policy grants put/get/delete to the configured credentials, and that the endpoint is reachable."),
            config_source="config.yaml:production.artifact",
        )

    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=DoctorStatus.PASS,
        component=_COMPONENT,
        message=(f"Object store round-trip successful (bucket={bucket_label}); put/get/delete verified, SSE-S3 encryption applied on write."),
        remediation=None,
        config_source="config.yaml:production.artifact",
    )


__all__ = ["probe_object_storage_security"]
