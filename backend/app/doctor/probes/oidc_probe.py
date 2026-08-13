"""Live OIDC JWKS reachability probe for the production doctor.

Implements the ``oidc.jwks_validation`` check: when ``production.oidc`` is
declared, fetch the configured ``jwks_uri`` and verify the response is a
valid JWKS document (JSON containing a non-empty ``"keys"`` array). The
probe validates **reachability + document shape**, not a full token
verification round-trip — the latter requires a real IdP-issued token and
is exercised by the auth middleware at request time.

Only meaningful when OIDC is configured — without an ``oidc`` block the
check WARN-skips (dev environments use local email/password login; ADR-0003
§10 makes OIDC an optional production declaration). When OIDC is configured
but ``jwks_uri`` is missing, the check FAILs (the static ``oidc.declared``
check already warns about this, but a live FAIL surfaces it at doctor time).

Promoted from the pre-T1A ``DEFERRED_LIVE_CHECKS`` stub. No-secret
guarantee: the result message carries only the JWKS URI's host (or a
label), never the full URL or any IdP credentials.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "oidc.jwks_validation"
_COMPONENT = "identity"


def _host_of(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or url
    except ValueError:
        return url


async def probe_oidc_jwks(config: AppConfig) -> DoctorCheckResult:
    """Probe the configured OIDC JWKS endpoint for reachability + shape.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    oidc = getattr(config.production, "oidc", None)

    if oidc is None:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.WARN,
            component=_COMPONENT,
            message=("oidc.jwks_validation skipped: production.oidc is not configured. OIDC is optional for dev (local email/password login); production deployments using an external IdP must declare it."),
            remediation=("Set production.oidc (issuer, audience, jwks_uri) for production deployments that use an external Identity Provider. Safe to leave unset for dev / single-Org local-auth deployments."),
            config_source="config.yaml:production.oidc",
        )

    if not oidc.jwks_uri:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=("production.oidc is configured but jwks_uri is missing. Bearer JWT verification cannot proceed without a JWKS endpoint."),
            remediation="Set production.oidc.jwks_uri to the IdP's JWKS endpoint (HTTPS).",
            config_source="config.yaml:production.oidc.jwks_uri",
        )

    host_label = _host_of(oidc.jwks_uri)

    # Lazily import httpx so config-only tests do not pay the import cost.
    try:
        import httpx
    except ImportError:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message="httpx is not installed; cannot probe JWKS reachability.",
            remediation="Install httpx (it is a declared production dependency).",
            config_source="config.yaml:production.oidc.jwks_uri",
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(oidc.jwks_uri)
            resp.raise_for_status()
            jwks = resp.json()
    except Exception:  # noqa: BLE001 — contain any network failure into FAIL
        logger.warning("OIDC JWKS probe could not reach %s", host_label, exc_info=True)
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=(f"Could not fetch the JWKS document from the configured IdP (host={host_label}); the endpoint is unreachable, returns a non-200 status, or the response is not valid JSON."),
            remediation=("Verify production.oidc.jwks_uri is correct, the IdP is reachable, and the endpoint serves a valid JWKS document. Re-run doctor after fixing."),
            config_source="config.yaml:production.oidc.jwks_uri",
        )

    keys = jwks.get("keys")
    if not isinstance(keys, list) or len(keys) == 0:
        return DoctorCheckResult(
            check_id=_CHECK_ID,
            status=DoctorStatus.FAIL,
            component=_COMPONENT,
            message=(f"The JWKS document at {host_label} does not contain a non-empty 'keys' array; the IdP may be misconfigured or the endpoint may serve a different document type."),
            remediation="Verify the jwks_uri points to a standards-compliant JWKS endpoint.",
            config_source="config.yaml:production.oidc.jwks_uri",
        )

    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=DoctorStatus.PASS,
        component=_COMPONENT,
        message=(f"JWKS endpoint reachable at {host_label}; {len(keys)} key(s) available for token signature verification."),
        remediation=None,
        config_source="config.yaml:production.oidc.jwks_uri",
    )


__all__ = ["probe_oidc_jwks"]
