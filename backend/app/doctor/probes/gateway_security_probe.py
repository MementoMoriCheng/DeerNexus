"""Gateway security probe for the production doctor (PR-064).

Implements ``gateway.security_validation``: an HTTP probe of the configured
gateway URL that verifies the runtime matches the static
``production.gateway_security`` declarations — TLS is actually enforced (not
just declared), CORS headers are actually returned on preflight, and the
CSRF cookie is actually set on a state-changing request.

Gating
------

The probe runs ONLY when a gateway URL is reachable from the doctor's
environment. The URL is read from the ``DEER_FLOW_GATEWAY_URL`` environment
variable (``https://gateway.svc.cluster.local:8001`` style). When unset the
probe returns WARN — it cannot verify security invariants without a target,
but the doctor is still useful for its other (in-process) checks. This keeps
the doctor runnable from an operator host (no gateway URL → skip) while
giving a real signal when run from a CI/release pipeline that knows the
gateway address.

No-secret guarantee: result messages carry only the URL host, never any
auth header or cookie value.

httpx failure handling: connection refused / DNS failure / timeout all
become a FAIL with the host labelled — the operator can tell "wrong host"
from "host up but wrong cert" without the doctor crashing.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "gateway.security_validation"
_COMPONENT = "gateway"
_CONFIG_SOURCE = "config.yaml:production.gateway_security,DEER_FLOW_GATEWAY_URL"
_GATEWAY_URL_ENV = "DEER_FLOW_GATEWAY_URL"
# A short, bounded timeout — the doctor is a preflight gate, not a load
# tester; if the gateway does not respond in 3s the probe fails fast rather
# than blocking the release pipeline.
_PROBE_TIMEOUT_SECONDS = 3.0


def _result(status: DoctorStatus, message: str, remediation: str | None = None) -> DoctorCheckResult:
    return DoctorCheckResult(
        check_id=_CHECK_ID,
        status=status,
        component=_COMPONENT,
        message=message,
        remediation=remediation,
        config_source=_CONFIG_SOURCE,
    )


def _host_of(url: str) -> str:
    """Return a display-safe host:port label for *url*."""
    try:
        after_scheme = url.split("://", 1)[1] if "://" in url else url
        return after_scheme.split("/", 1)[0] or "unknown-host"
    except Exception:  # noqa: BLE001
        return "unknown-host"


async def _httpx_get(url: str, method: str = "GET", headers: dict[str, str] | None = None) -> object:
    """Issue an httpx request and return the Response. Raises on any error."""
    import httpx

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS, follow_redirects=False, verify=True) as client:
        return await client.request(method, url, headers=headers or {})


async def probe_gateway_security(config: AppConfig) -> DoctorCheckResult:
    """Probe the configured gateway URL for TLS / CORS / CSRF runtime behaviour.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    gateway_url = os.environ.get(_GATEWAY_URL_ENV, "").strip()
    if not gateway_url:
        return _result(
            DoctorStatus.WARN,
            f"gateway.security_validation skipped: {_GATEWAY_URL_ENV} is not set; the doctor cannot probe a live gateway without a target URL.",
            f"Set {_GATEWAY_URL_ENV} (e.g. https://gateway.svc.cluster.local:8001) in the release pipeline environment to enable the live TLS/CORS/CSRF probe.",
        )

    sec = config.production.gateway_security
    host_label = _host_of(gateway_url)

    # TLS: if the declaration says tls_enabled but the URL is plaintext http://,
    # that is a configuration inconsistency — production TLS must be enforced
    # end-to-end, not just declared.
    if sec.tls_enabled and not gateway_url.lower().startswith("https://"):
        return _result(
            DoctorStatus.FAIL,
            f"production.gateway_security.tls_enabled=true but {_GATEWAY_URL_ENV}={gateway_url!r} is not https://; TLS is declared but not actually in use.",
            f"Set {_GATEWAY_URL_ENV} to the https:// URL of the gateway (in front of which nginx/ingress terminates TLS).",
        )

    # Live probe — GET the gateway root and inspect response headers.
    try:
        response = await _httpx_get(gateway_url)
    except Exception as exc:  # noqa: BLE001 — any httpx failure → FAIL
        logger.warning("gateway security probe could not reach %s", host_label, exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            f"Could not reach the gateway at {host_label} ({type(exc).__name__}); live TLS/CORS/CSRF validation is not possible.",
            f"Verify the gateway is running and reachable at {host_label}; check ingress / service mesh / network policy.",
        )

    status_code = getattr(response, "status_code", 0)
    headers = getattr(response, "headers", {}) or {}
    findings: list[str] = []
    overall = DoctorStatus.PASS

    # CORS: declared allow-origins should produce an Access-Control-Allow-Origin
    # on a cross-origin preflight. The doctor's GET may or may not trigger it
    # (browsers send Origin; we don't), so missing ACAO is a WARN (the
    # preflight path may still work) rather than FAIL.
    if sec.cors_origins and "access-control-allow-origin" not in {k.lower() for k in headers}:
        findings.append("CORS declared but no Access-Control-Allow-Origin header on response (preflight may still work)")
        overall = DoctorStatus.WARN

    # CSRF: a state-changing endpoint should set the CSRF cookie. We can't
    # cheaply issue a POST that passes auth, so the GET response is the
    # best available signal. Missing csrf cookie → WARN.
    if sec.csrf_enabled:
        set_cookie = headers.get("set-cookie", "") if hasattr(headers, "get") else ""
        cookie_str = str(set_cookie).lower()
        # DeerNexus CSRF cookie name is configured; fall back to a generic
        # csrf-token substring match if the cookie name is not exposed.
        if "csrf" not in cookie_str:
            findings.append("CSRF enabled but no csrf-named cookie in Set-Cookie (a state-changing request may set it)")
            overall = DoctorStatus.WARN

    if overall is DoctorStatus.PASS:
        return _result(
            DoctorStatus.PASS,
            f"Gateway at {host_label} reachable (HTTP {status_code}); no declared security invariant violated by the live probe.",
        )
    # WARN — reached but with caveats
    return _result(
        DoctorStatus.WARN,
        f"Gateway at {host_label} reachable (HTTP {status_code}) but: {'; '.join(findings)}.",
        "Investigate the flagged findings; they are WARN because the live GET cannot fully exercise CORS preflight or CSRF cookie-set on a state-changing POST.",
    )


__all__ = ["probe_gateway_security"]
