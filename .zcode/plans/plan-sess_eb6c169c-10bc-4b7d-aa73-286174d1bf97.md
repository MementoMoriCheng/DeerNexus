## PR-032 实施方案:Runtime Router 切 RBAC

### 目标与范围

Track C 第三刀。把 Thread / Run / Artifact 四个 runtime router 的 `@require_permission` 旧 stub 切到基于 PR-031 `AuthorizeService.authorize()` 的新装饰器 `@require_rbac`,**真正激活 DB-backed 权限交集计算**(今天 `_ALL_PERMISSIONS` flat-grant 让所有 authenticated user 拿全权)。同时保留 owner_check 语义、IM channel worker 的 trusted-internal-caller 白名单短路、`policy.evaluated` 观测埋点。

### 已确认设计决策(用户已拍板)

1. **IM channel worker 白名单短路**:`tenant_context.auth_method == "internal"` 且 header 有 `X-DeerFlow-Owner-User-Id` → 跳 `authorize()`,只跑 `thread_store.check_access`。语义等价于今天,改动最小,IM 流量零回归。TODO 注释:等 multi-Org active phase + IAM seed 完成后改成全走 authorize()。
2. **新装饰器 `@require_rbac`** + 全量改 21 个 call site(非原地改写 `require_permission`)。语义最清晰、与 PR-031 设计对齐。
3. **四刀全切**(threads / thread_runs / runs / artifacts)。pr-split-guide §8 "按 Thread/Run/Artifact 拆分" 是子项枚举,不是 3 个 PR。
4. **`policy.evaluated` 单一事件**(事件名已在 observability §3.4 reserved)。allow=INFO / deny=WARNING,**不**接 AuditEvent(ADR §13 audit 目录不含 per-request deny)。

### 关键技术约束(调研发现)

- **`threads:delete` 映射 gap**:ADR-0003 §3 thread 域**只有** `runtime:thread:read|write`,没有 `delete`。`threads.py:214` DELETE 端点 → `Permission.RUNTIME_THREAD_WRITE`(ADR by-design,delete 归 write)。
- **`Permission` 枚举无 delete 成员**(`rbac.py:46-51`),所以无需扩 registry。
- **`_StubAuthMiddleware` 不 bind tenant_context**(`_router_auth_helpers.py:64-80`),新装饰器取 `get_tenant_context()` 会拿 None。需要升级 helper:bind tenant_context + 通过 sf seed 一行 active membership + org:admin role binding。
- **`call_unwrapped` 仍可用**:新装饰器用 `functools.wraps`,`__wrapped__` 链不断。
- **`AuthorizeError.code` 不区分 403/404**:authorize.py docstring L88-91 明示"router decides from context"。PR-032 默认 `PERMISSION_DENIED → 403`(single-Org bootstrap 阶段实际不存在 invited/removed 场景;cross-Org→404 已由 `check_access` 承担)。multi-Org active phase 启用后 follow-up 细化。
- **`runs.py` stateless 端点(L35/L60)是 undecorated**:owner check 在 `services.start_run` 内(`services.py:335-357`),本 PR 不动,在 §16.38 标 follow-up。
- **`_deerflow_test_bypass_auth` 和 `_ALL_PERMISSIONS` 必须保留**:Admin/Studio router(PR-033)和它们的测试还在用旧 stub。

---

### 文件改动清单

#### 新增文件

**1. `backend/app/gateway/rbac.py`** — 新装饰器 `require_rbac`

签名:`require_rbac(permission: Permission, *, owner_check: bool = False, require_existing: bool = False)`。

流程(参照 `authz.py:237-317` 既有结构):
1. 复用 `_deerflow_test_bypass_auth` 测试 bypass
2. `tenant_context = get_tenant_context()`;None → `HTTPException(401)`(照 `_require_org_id` `admin.py:143-158` 模式)
3. **IM 白名单短路**:`if tenant_context.auth_method == "internal" and request.headers.get(INTERNAL_OWNER_USER_ID_HEADER_NAME): skip authorize()`(TODO 注释)
4. `try: await get_authorize_service().authorize(tenant_context, permission) except AuthorizeError as exc:` 映射:
   - `AUTHENTICATION_INVALID` → 401
   - `ORG_SUSPENDED` / `ORG_DELETING` → 403
   - `PERMISSION_DENIED` → 403(default,follow-up 标 ADR §12 invited/removed→404)
5. `emit_event("policy.evaluated", level=INFO|WARNING, permission=..., outcome="allowed"|"denied", error_code=..., org_id=..., principal_id=...)`(成功失败都发)
6. **owner_check 分支:照搬 `authz.py:278-313` 完整逻辑**(`thread_store.check_access` + `INTERNAL_SYSTEM_ROLE` header owner fallback → 404)。这段是 cross-Org/cross-user → 404 的实际承担者,一字不动。

不删 `authz.py` 旧 stub —— PR-033 切 Admin/Studio 时才整体替换。

**2. `backend/tests/test_rbac_runtime_routers.py`** — RBAC 矩阵 + 状态测试

复用 `test_iam_authorize.py` 的 `_bootstrap` seed pattern(org + 3 builtin roles + user + membership + binding),通过 TestClient 跑端点:

- **§9.1 矩阵**(runtime 域子集):
  | 能力 \ 角色 | org:admin | org:developer | org:viewer |
  | --- | --- | --- | --- |
  | thread read | ✓ | ✓ | ✓ |
  | thread write/delete | ✓ | ✓ | ✗ (403) |
  | run create | ✓ | ✓ | ✗ (403) |
  | run read | ✓ | ✓ | ✓ |
  | run cancel | ✓ | ✓ | ✗ (403) |
- **§9.2 状态映射**:无 membership → 403;suspended membership → 403;org suspended → 403
- **trusted-internal-caller**:header owner 场景 threads.create 成功(白名单短路生效),foreign owner → 404
- **owner_check cross-user**:check_access deny → 404
- **观测验证**:mock `emit_event` 断言 `policy.evaluated` 调用,allow/deny 各一次
- marker:`@pytest.mark.anyio` + `@pytest.mark.parametrize`,docstring 引 `IAM-200`~`IAM-2NN`(承接 PR-031 的 IAM-1xx)

#### 修改文件

**3. `backend/app/gateway/routers/threads.py`** — 6 个 call site

| 行号 | 旧 | 新 |
| --- | --- | --- |
| 214 | `threads:delete` (owner_check+require_existing) | `Permission.RUNTIME_THREAD_WRITE` |
| 361 | `threads:write` (owner_check+require_existing) | `Permission.RUNTIME_THREAD_WRITE` |
| 390 | `threads:read` (owner_check) | `Permission.RUNTIME_THREAD_READ` |
| 448 | `threads:read` (owner_check) | `Permission.RUNTIME_THREAD_READ` |
| 500 | `threads:write` (owner_check+require_existing) | `Permission.RUNTIME_THREAD_WRITE` |
| 602 | `threads:read` (owner_check) | `Permission.RUNTIME_THREAD_READ` |

import:`from app.gateway.rbac import require_rbac` + `from deerflow.contracts.rbac import Permission`

**4. `backend/app/gateway/routers/thread_runs.py`** — 12 个 call site

| 行号 | 旧 | 新 |
| --- | --- | --- |
| 143, 151, 179 | `runs:create` (owner_check+require_existing) | `Permission.RUNTIME_RUN_CREATE` |
| 206, 216, 263, 291, 343, 388, 414 | `runs:read` (owner_check) | `Permission.RUNTIME_RUN_READ` |
| 228 | `runs:cancel` (owner_check+require_existing) | `Permission.RUNTIME_RUN_CANCEL` |
| 429 | `threads:read` (owner_check) | `Permission.RUNTIME_THREAD_READ` |

**5. `backend/app/gateway/routers/runs.py`** — 2 个 call site

| 行号 | 旧 | 新 |
| --- | --- | --- |
| 107 | `runs:read` | `Permission.RUNTIME_RUN_READ` |
| 138 | `runs:read` | `Permission.RUNTIME_RUN_READ` |

注:`POST /stream` (L35) 和 `POST /wait` (L60) 一直 undecorated,owner check 在 `services.start_run`,本 PR 不动。

**6. `backend/app/gateway/routers/artifacts.py`** — 1 个 call site

| 行号 | 旧 | 新 |
| --- | --- | --- |
| 104 | `threads:read` (owner_check) | `Permission.RUNTIME_THREAD_READ` |

**7. `backend/tests/_router_auth_helpers.py`** — 升级 stub 支持 RBAC

- 新增 `make_rbac_test_app(*, user_factory=None, sf, role_name=ORG_ADMIN_ROLE_NAME, owner_check_passes=True)`:
  - bind tenant_context(`org_id=default_org_id`,`principal=PrincipalRef(type="user", user_id=str(user.id))`,`auth_method="cookie"`)
  - 通过 sf seed:`_seed_org` + `_seed_user` + `_seed_membership(status="active")` + `_bind_role(role_name)`(复用 `test_iam_authorize.py` 的 helper)
  - stub `thread_store.check_access` 行为不变
- 旧 `make_authed_test_app` **保留**(PR-033 未切的 admin/studio 测试还在用)

**8. 现有 router 测试 setup 迁移**(~10-15 个文件)

需要从 `make_authed_test_app` → `make_rbac_test_app` 的测试文件:
- `test_threads_router.py`(主)
- `test_stateless_runs_owner_isolation.py`
- `test_runs_api_endpoints.py`
- `test_artifacts_router.py`
- `test_thread_run_messages_pagination.py`
- `test_thread_token_usage.py`
- 其他 grep 出的 `make_authed_test_app` + Thread/Run/Artifact 测试

每个文件:setup 升级(加 sf fixture + seed),call site 和断言不动。`call_unwrapped` 直接调用路径不需改(`__wrapped__` 链不断)。

**9. `docs/engineering/progress.md`** — PR-032 行更新

状态从「阻塞 → PR-031」改为「已交付 #(TBD)」,备注列填实际内容(21 call site 切换、新装饰器、IM 白名单、policy.evaluated、矩阵测试)。

**10. `docs/architecture/runtime-contracts.md`** — 追加 §16.37 PR-032 + §16.38 PR-032 不包含

参照 §16.35/16.36(PR-031)体例。§16.37 列交付清单,§16.38 列延后项(stateless `/api/runs/stream|wait` owner check 迁移、ADR §12 invited/removed→404 细化、`authz.py` 旧 stub 删除等 PR-033 触发条件)。

---

### 测试矩阵覆盖(testing-strategy §9)

**§9.1 runtime 子集 5×3**:每个 cell 一个 parametrized 测试,断言 HTTP status(200/403)。Oracle = `BUILTIN_ROLE_PERMISSIONS`(PR-030 已 pin)。

**§9.2 状态映射**:
| 状态 | 期望行为 |
| --- | --- |
| 无 membership | 403 |
| suspended membership | 403 |
| org suspended | 403 |
| trusted-internal-caller(header owner) | 白名单短路,200 |
| trusted-internal-caller(foreign owner) | 404(check_access deny) |
| cross-user thread(owner_check deny) | 404 |

**观测**:`policy.evaluated` 事件 emit,allow/deny 各一次断言。

---

### 回滚与风险

**回滚安全**:
- 新增 `rbac.py` 模块(零侵入,只有 router import 后才生效)
- 4 个 router 文件装饰器改动:`git revert` 即可回到旧 stub
- 旧 `authz.py` 完整保留(PR-033 切完才删)

**已识别风险**:
- **21 个 call site 一次性改**:diff 大,但每个改动机械(装饰器名 + permission 枚举值),review 成本可控
- **现有测试 setup 升级**:~10-15 个文件改 setup,断言不动
- **PERMISSION_DENIED → 403 不完美**:ADR §12 invited/removed→404 暂不达标;single-Org bootstrap 阶段实际不存在该场景,multi-Org active phase follow-up
- **`runs.py` stateless 端点未覆盖**:owner check 在 `services.start_run`,本 PR 不动,§16.38 标 follow-up
- **trusted-internal-caller 白名单是 temporary shim**:TODO 注释,等 multi_org active phase + IAM seed 完成后改成全走 authorize()

---

### PR 描述草案

**标题**:`feat(rbac): runtime router RBAC swap (PR-032)`

**Why**:Track C 第三刀。把 Thread/Run/Artifact 四个 runtime router 的 `@require_permission` 旧 stub 切到基于 PR-031 `AuthorizeService.authorize()` 的新装饰器 `@require_rbac`,真正激活 DB-backed 权限交集计算。IM channel worker trusted-internal-caller 流量走白名单短路(等 multi-Org active phase IAM seed 完成后移除)。

**Scope**:
- included:`app/gateway/rbac.py`(新装饰器)+ 4 router 21 call site 切换 + `_router_auth_helpers.py` 升级 + RBAC 矩阵测试 + `policy.evaluated` 观测埋点
- excluded:`authz.py` 旧 stub 删除(PR-033)+ Admin/Studio router(PR-033)+ stateless `/api/runs/stream|wait` owner check 迁移(follow-up)+ API Key scope(PR-035)+ 主动失效(PR-037)

**Tenant / Security**:
- org scope:`authorize` 按 `(org_id, principal.user_id)` 查 RoleBinding,跨 Org 不可见
- 错误码:照 ADR §12,`PERMISSION_DENIED`→403 默认映射,invited/removed→404 留 follow-up
- system 隔离:`auth_method=='internal'` + `X-DeerFlow-Owner-User-Id` 白名单短路

**Data**:无 schema/migration 变更(纯代码层,复用 PR-020B IAM 表 + PR-030 seed)

**Rollback**:`git revert` 即可,旧 stub 完整保留

---

### 实施顺序

1. 写 `app/gateway/rbac.py`(新装饰器 + IM 白名单短路 + 观测埋点 + owner_check 分支照搬)
2. 升级 `_router_auth_helpers.py`(`make_rbac_test_app` 工厂)
3. 改 4 个 router 的 21 个 call site(机械替换 + import)
4. 迁移现有 router 测试 setup(`make_authed_test_app` → `make_rbac_test_app`)
5. 写新 RBAC 矩阵测试(`test_rbac_runtime_routers.py`,§9.1 + §9.2 + 观测)
6. 跑全量测试 + lint(确认无回归 + 41 个 pre-existing Windows 失败不增量)
7. 更新 `progress.md` + `runtime-contracts §16.37/16.38`
8. 建分支 `pr-032-runtime-router-rbac` + push + 开 PR + 监控 CI(按 PR-030/031 流程)

需要我开始实施吗?