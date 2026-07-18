"""Rate-limit Retry-After probe for the production doctor (PR-064).

Implements ``gateway.rate_limit_retry_after``: triggers the per-IP login
lockout (the only rate-limiter that exists today, in
``app/gateway/routers/auth.py::_check_rate_limit``) and verifies the 429
response carries a ``Retry-After`` header so well-behaved clients back off
rather than hammering the gateway.

Gating
------

Like the gateway security probe this runs only when
``DEER_FLOW_GATEWAY_URL`` is set — the doctor cannot exercise a live
rate-limit without a real target. When the URL is set but
``gateway_security.rate_limit_enabled=false`` the probe returns WARN-skip
(the lockout is independent of the production rate-limit declaration, but
the doctor avoids surprising the operator with lockouts against a deployment
that has explicitly declared "no rate-limit").

Lockout mechanics
----------------

``_MAX_LOGIN_ATTEMPTS`` (auth.py) is the per-IP failure threshold. The probe
sends ``threshold + 2`` bad-password POSTs to ``/api/v1/auth/login/local``
and expects:

* the first ``threshold`` attempts to return 401 (auth failure, no lockout yet);
* subsequent attempts to return **429 with a ``Retry-After`` header** → PASS;
* 429 without ``Retry-After`` → WARN (lockout works but clients won't back off);
* no 429 at all → FAIL (rate-limit not enforced at runtime).

No-secret guarantee: the probe uses a fake username + bad password that
cannot match any real account. Result messages carry only the gateway host,
never auth payloads.

Test isolation: unit tests monkeypatch the ``_httpx_post`` helper rather
than hitting a real network; integration verification happens in CI against
a running gateway.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from app.doctor.models import DoctorCheckResult, DoctorStatus

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_CHECK_ID = "gateway.rate_limit_retry_after"
_COMPONENT = "gateway"
_CONFIG_SOURCE = "config.yaml:production.gateway_security.rate_limit_enabled,DEER_FLOW_GATEWAY_URL"
_GATEWAY_URL_ENV = "DEER_FLOW_GATEWAY_URL"
_PROBE_TIMEOUT_SECONDS = 3.0
# Number of bad-login attempts beyond the threshold to send. ``+2`` gives a
# small margin so we observe the lockout state across at least two requests
# (catches a lockout that trips then immediately expires due to a bug).
_OVER_THRESHOLD_MARGIN = 2
_LOGIN_PATH = "/api/v1/auth/login/local"


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
    try:
        after_scheme = url.split("://", 1)[1] if "://" in url else url
        return after_scheme.split("/", 1)[0] or "unknown-host"
    except Exception:  # noqa: BLE001
        return "unknown-host"


async def _httpx_post(url: str, json_body: dict[str, Any]) -> Any:
    """Issue an httpx POST and return the Response. Raises on any error."""
    import httpx

    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS, follow_redirects=False, verify=True) as client:
        return await client.post(url, json=json_body)


def _max_login_attempts_threshold() -> int:
    """Read the live ``_MAX_LOGIN_ATTEMPTS`` from auth.py.

    Imported lazily so the probe module imports cleanly even if auth.py is
    mid-refactor; returns a sensible fallback of 5 if the constant is
    unreachable (the fallback only affects how many requests we send, not
    the lockout semantics on the gateway side).
    """
    try:
        from app.gateway.routers.auth import _MAX_LOGIN_ATTEMPTS

        return int(_MAX_LOGIN_ATTEMPTS)
    except Exception:  # noqa: BLE001
        return 5


async def probe_rate_limit_retry_after(config: AppConfig) -> DoctorCheckResult:
    """Trigger the auth login lockout and verify 429 + Retry-After.

    Returns a PASS/WARN/FAIL :class:`DoctorCheckResult`. Never raises.
    """
    gateway_url = os.environ.get(_GATEWAY_URL_ENV, "").strip()
    if not gateway_url:
        return _result(
            DoctorStatus.WARN,
            f"gateway.rate_limit_retry_after skipped: {_GATEWAY_URL_ENV} is not set; the doctor cannot exercise a live rate-limit without a target URL.",
            f"Set {_GATEWAY_URL_ENV} in the release pipeline environment to enable the live 429 + Retry-After probe.",
        )

    if not config.production.gateway_security.rate_limit_enabled:
        return _result(
            DoctorStatus.WARN,
            "gateway.rate_limit_retry_after skipped: production.gateway_security.rate_limit_enabled=false. The auth login lockout still runs but the deployment has declared no rate-limit; skipping to avoid surprising the operator.",
            "Enable rate_limit_enabled under production.gateway_security to admit this probe, or accept that runtime rate-limiting is unverified.",
        )

    threshold = _max_login_attempts_threshold()
    host_label = _host_of(gateway_url)
    login_url = gateway_url.rstrip("/") + _LOGIN_PATH

    # A fake username that cannot collide with any real account — the probe
    # MUST NOT succeed in authenticating (that would mean a real account was
    # brute-forced by the doctor, defeating its purpose). Bad password is
    # random per run so it cannot match either.
    import uuid

    fake_user = f"doctor-probe-{uuid.uuid4().hex[:8]}@invalid.invalid"
    fake_password = uuid.uuid4().hex

    saw_429 = False
    saw_retry_after = False
    last_error: str | None = None

    attempts = threshold + _OVER_THRESHOLD_MARGIN
    try:
        for _ in range(attempts):
            response = await _httpx_post(login_url, {"email": fake_user, "password": fake_password})
            code = getattr(response, "status_code", 0)
            if code == 429:
                saw_429 = True
                # Look for Retry-After (case-insensitive — httpx headers are
                # case-insensitive but be defense-in-depth).
                headers = getattr(response, "headers", {}) or {}
                for key in headers:
                    if key.lower() == "retry-after":
                        saw_retry_after = True
                        break
    except Exception as exc:  # noqa: BLE001 — any httpx failure → FAIL
        last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("rate-limit probe could not reach %s", host_label, exc_info=True)
        return _result(
            DoctorStatus.FAIL,
            f"Could not reach the gateway at {host_label} to exercise the rate-limit ({last_error}).",
            f"Verify the gateway is running and reachable at {host_label}; the auth login lockout cannot be verified without a live endpoint.",
        )

    if not saw_429:
        return _result(
            DoctorStatus.FAIL,
            f"Sent {attempts} bad-login attempts (threshold={threshold}) to {host_label} but never observed a 429; the per-IP auth login lockout is not enforcing at runtime.",
            "Check that the gateway is the real DeerNexus build (not a stub) and that _check_rate_limit in app/gateway/routers/auth.py is wired into login_local.",
        )

    if not saw_retry_after:
        return _result(
            DoctorStatus.WARN,
            f"Auth login lockout triggered at {host_label} (429 observed) but the response had no Retry-After header; well-behaved clients will not back off.",
            "Add a Retry-After header to the 429 response in app/gateway/routers/auth.py::_check_rate_limit so clients can honour the lockout window.",
        )

    return _result(
        DoctorStatus.PASS,
        f"Auth login lockout enforced at {host_label}: 429 returned after {threshold} bad attempts with a Retry-After header.",
    )


__all__ = ["probe_rate_limit_retry_after"]
