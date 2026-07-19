"""Router-level RBAC decorator (PR-032).

Track C 第三刀:把 Thread / Run / Artifact 四个 runtime router 从
``authz.py`` 的 ``_ALL_PERMISSIONS`` flat-grant stub 切到 PR-031 的
:class:`~app.gateway.authorize.AuthorizeService`。

公共面是一个新装饰器 :func:`require_rbac`,语义等价于旧
``require_permission`` 但权限检查走 DB-backed
``AuthorizeService.authorize()`` (ADR-0003 §6 交集公式)。

设计要点 (pr-split-guide §8 / ADR-0003 §12 / observability §3.4):

* **Authorization**: ``tenant_context`` 已由
  ``TenantResolutionMiddleware`` 绑定到 ContextVar,本装饰器从
  ``get_tenant_context()`` 取出后直接调 ``authorize()``。
* **IM channel worker 白名单短路**: 单 Org bootstrap 阶段
  (``tenancy.multi_org.phase == "disabled"``) 不给 IM channel
  connection owner seed IAM 行,因此 trusted-internal-caller 流量
  (``auth_method == "internal"`` 且带 ``X-DeerFlow-Owner-User-Id``)
  会全部被 ``authorize()`` 挡。本装饰器在该组合出现时跳过
  ``authorize()``、只保留 thread ownership check,语义等价于
  切换前。TODO(multi_org active phase): seed owner membership +
  role binding 后改成全走 ``authorize()``、删此分支。
* **owner_check 不变**: ``resource_ref`` 在 MVP 是 no-op
  (ADR §17),因此 thread ownership / cross-Org → 404 仍由
  ``thread_store.check_access`` 承担。整段逻辑 (authz.py:278-313)
  原样保留,只是搬到新装饰器。
* **观测**: 每次 allow/deny 发一次 ``policy.evaluated``
  (observability §3.4 reserved event name)。allow → INFO,
  deny → WARNING (per §3.2 guidance: expected deny 不是 ERROR)。
  不接 AuditEvent —— ADR §13 audit 目录只含 IAM mutations,
  不含 per-request RBAC decision。
* **错误映射** (ADR §12): ``AUTHENTICATION_INVALID`` → 401;
  ``ORG_SUSPENDED``/``ORG_DELETING`` → 403;
  ``PERMISSION_DENIED`` → 403 default。invited/removed→404 的细化
  留 multi-Org active phase follow-up (single-Org bootstrap
  阶段实际不存在该场景)。
* **保留旧 stub**: ``authz.py`` 整体不动 —— Admin/Studio router
  (PR-033) 和它们的测试还在用旧 ``require_permission`` /
  ``_deerflow_test_bypass_auth``。``authz.py`` 的删除时机: ADR §14
  step 10,触发条件 = PR-033 切完 + 旧 acceptance §15 勾掉。
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, ParamSpec, TypeVar

from fastapi import HTTPException, Request

from app.gateway.authorize import AuthorizeError, get_authorize_service
from app.gateway.internal_auth import (
    INTERNAL_OWNER_USER_ID_HEADER_NAME,
    INTERNAL_SYSTEM_ROLE,
)
from deerflow.contracts import ErrorCode, Permission, get_tenant_context
from deerflow.observability.events import emit_event

P = ParamSpec("P")
T = TypeVar("T")

_LOGGER = logging.getLogger(__name__)


def _make_test_request_stub() -> SimpleNamespace:
    """Build a minimal Request stub so direct-call tests work without FastAPI.

    Mirrors ``authz._make_test_request_stub`` so the ``@require_rbac``
    decorator behaves the same as ``@require_permission`` when a route
    handler is invoked positionally from a unit test that doesn't pass
    a real ``Request``.
    """
    return SimpleNamespace(state=SimpleNamespace(), cookies={}, _deerflow_test_bypass_auth=True)


def _authorize_error_to_http(exc: AuthorizeError) -> HTTPException:
    """Map an :class:`AuthorizeError` to a FastAPI :class:`HTTPException`.

    ADR §12 status mapping. ``PERMISSION_DENIED`` defaults to 403 — the
    invited/removed/cross-Org → 404 distinction is the router layer's
    responsibility (per ``authorize.py`` docstring L88-91); the
    cross-Org / cross-user 404 case is actually produced by
    ``thread_store.check_access`` in the owner_check branch below, not
    by this mapping.
    """
    code = exc.code
    permission = exc.permission or ""
    if code == ErrorCode.AUTHENTICATION_INVALID:
        return HTTPException(status_code=401, detail="Authentication required")
    if code == ErrorCode.ORG_SUSPENDED:
        return HTTPException(status_code=403, detail="Organization is suspended")
    if code == ErrorCode.ORG_DELETING:
        return HTTPException(status_code=403, detail="Organization is being deleted")
    # PERMISSION_DENIED (and any future deny code): default 403.
    detail = f"Permission denied: {permission}" if permission else "Permission denied"
    return HTTPException(status_code=403, detail=detail)


def _is_internal_owner_request(request: Request) -> bool:
    """Return True for trusted-internal caller traffic that owns an impersonated user.

    The IM channel worker path: ``AuthMiddleware`` has already validated
    ``X-DeerFlow-Internal-Token`` and stamped the synthetic
    ``INTERNAL_SYSTEM_ROLE`` user; ``TenantResolutionMiddleware`` then
    bound ``auth_method="internal"``. The owner header is honored only
    on that path (see ``internal_auth.get_trusted_internal_owner_user_id``).
    """
    user = getattr(request.state, "user", None)
    if getattr(user, "system_role", None) != INTERNAL_SYSTEM_ROLE:
        return False
    header_owner = (request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME) or "").strip()
    return bool(header_owner)


def require_rbac(
    permission: Permission,
    *,
    owner_check: bool = False,
    require_existing: bool = False,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator that runs :class:`AuthorizeService.authorize` for ``permission``.

    Drop-in replacement for ``require_permission`` that consults the
    DB-backed Authorize Service instead of ``_ALL_PERMISSIONS``. Must
    be used AFTER ``@require_auth`` (the auth-middleware path is what
    stamps the User the Authorize Service reads).

    Args:
        permission: Frozen :class:`Permission` member. Pass the enum
            itself (not a raw string) so a typo fails at import time
            rather than at the first request.
        owner_check: If True, additionally validates that the current
            user owns the resource via ``thread_store.check_access``.
            Requires a ``thread_id`` path parameter. This is the
            cross-Org / cross-user → 404 carrier (``authorize()``'s
            ``resource_ref`` is a no-op in MVP per ADR §17).
        require_existing: Only meaningful with ``owner_check=True``. If
            True, a missing ``threads_meta`` row counts as denial (404)
            rather than "untracked legacy thread, allow". Use on
            destructive / mutating routes so a deleted thread can't be
            re-targeted via the missing-row path.

    Raises:
        HTTPException 401: Anonymous / invalid credentials / no tenant
            context bound.
        HTTPException 403: Permission denied, suspended membership, or
            suspended / deleting org.
        HTTPException 404: owner_check failed (cross-user / cross-Org
            thread, or destructive op on missing row).
        ValueError: owner_check=True but 'thread_id' parameter missing.
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request = kwargs.get("request")
            if request is None:
                # Unit tests may call decorated route handlers directly
                # without constructing a FastAPI Request object. Inject
                # a minimal stub when the wrapped function declares
                # ``request`` (mirrors authz.require_permission).
                if "request" in inspect.signature(func).parameters:
                    kwargs["request"] = _make_test_request_stub()
                else:
                    return await func(*args, **kwargs)
                request = kwargs["request"]

            if getattr(request, "_deerflow_test_bypass_auth", False):
                return await func(*args, **kwargs)

            tenant_context = get_tenant_context()
            if tenant_context is None:
                # TenantResolutionMiddleware didn't bind — anonymous /
                # not-yet-authenticated. Fail closed to 401 (matches
                # admin._require_org_id's posture but with the auth
                # status code ADR §12 expects for invalid creds).
                raise HTTPException(status_code=401, detail="Authentication required")

            perm_value = permission.value

            # IM channel worker white-list short-circuit. See module
            # docstring: single-Org bootstrap phase doesn't seed IAM
            # rows for connection owners, so authorize() would deny
            # every channel-triggered call. TODO(multi_org active):
            # remove this branch once owners get seeded memberships.
            skip_authorize = tenant_context.auth_method == "internal" and _is_internal_owner_request(request)

            if not skip_authorize:
                try:
                    await get_authorize_service().authorize(tenant_context, permission)
                except AuthorizeError as exc:
                    emit_event(
                        "policy.evaluated",
                        level=logging.WARNING,
                        message="RBAC deny",
                        permission=perm_value,
                        outcome="denied",
                        error_code=str(exc.code),
                        org_id=tenant_context.org_id,
                        principal_id=tenant_context.principal.user_id,
                        auth_method=tenant_context.auth_method,
                    )
                    raise _authorize_error_to_http(exc) from exc

            emit_event(
                "policy.evaluated",
                level=logging.INFO,
                message="RBAC allow",
                permission=perm_value,
                outcome="allowed",
                error_code=None,
                org_id=tenant_context.org_id,
                principal_id=tenant_context.principal.user_id,
                auth_method=tenant_context.auth_method,
                internal_bypass=skip_authorize,
            )

            # Owner check for thread-specific resources. Same logic as
            # authz.require_permission's owner_check block (L278-313):
            # ``check_access`` returns True for missing rows (untracked
            # legacy thread) and for NULL-owner rows (shared / pre-auth
            # data), so strict-deny rather than strict-allow — only an
            # existing row with a different user_id triggers 404.
            if owner_check:
                thread_id = kwargs.get("thread_id")
                if thread_id is None:
                    raise ValueError("require_rbac with owner_check=True requires 'thread_id' parameter")

                from app.gateway.deps import get_thread_store

                thread_store = get_thread_store(request)
                user = getattr(request.state, "user", None)
                user_id = str(user.id) if user is not None else tenant_context.principal.user_id

                allowed = await thread_store.check_access(
                    thread_id,
                    user_id,
                    require_existing=require_existing,
                )
                if not allowed and getattr(user, "system_role", None) == INTERNAL_SYSTEM_ROLE:
                    # Trusted internal callers (channel workers) act for
                    # the connection owner carried in
                    # X-DeerFlow-Owner-User-Id. Scope the check to that
                    # owner instead of bypassing it — a leaked internal
                    # token must not grant cross-user thread access.
                    header_owner = (request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME) or "").strip()
                    if header_owner:
                        allowed = await thread_store.check_access(
                            thread_id,
                            header_owner,
                            require_existing=require_existing,
                        )
                if not allowed:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Thread {thread_id} not found",
                    )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


__all__ = ["require_rbac"]
