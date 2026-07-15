# DeerNexus API 边界与契约

> 状态：MVP 接口草案  
> 关联：[目标架构](target-architecture.md) · [运行时契约](runtime-contracts.md) · [数据模型](data-model.md) · [ADR-0002](../adr/0002-tenant-workspace-keys.md)

本文定义 Runtime API、Admin API、Studio API 与兼容路由的职责边界。Fork 初始化后应把上游现有路由映射到本文分类；如实际前缀无法立即调整，可以保留兼容路径，但授权、租户解析和依赖方向必须符合本文。

---

## 1. 目标

1. 防止管理操作混入 Runtime 路由和 harness；
2. 为每个端点明确认证、授权、租户解析、幂等和审计要求；
3. 统一 `403` / `404`、错误 envelope、分页和并发控制；
4. 保留 DeerFlow / LangGraph 兼容能力，同时阻止其绕过企业控制面；
5. 让 OpenAPI、契约测试和前端客户端可以独立演进。

---

## 2. API 分区

建议前缀：

```text
/api/runtime/v1/*   # 创建和控制 Run、Thread、流式事件、运行制品
/api/admin/v1/*     # Organization、IAM、审计、Console、平台设置
/api/studio/v1/*    # Agent 制品、Catalog、发布通道
/api/internal/v1/*  # 可信服务间接口，不对浏览器和公网开放
/api/compat/*       # 上游或 LangGraph 兼容适配
```

### 2.1 Runtime API

负责：

- Thread 创建、读取和列表；
- Run 创建、状态、取消和恢复；
- SSE / 流式事件；
- Run Artifact 和允许的运行输出；
- 当前用户可用 Agent / Feature 的只读解析；
- 与运行直接相关的最小用量摘要。

不负责：

- 创建 Organization、角色或 API Key；
- 修改 Agent 已发布版本；
- 查询跨用户或跨 Org Console；
- 修改 Policy、Quota 或系统设置。

### 2.2 Admin API

负责：

- Organization / Workspace 生命周期；
- Membership、Role、RoleBinding；
- ServiceAccount、API Key；
- 审计查询与合规导出；
- Org 范围 Console：stats、runs、usage、failure summary；
- Connector / MCP 管理中的组织级配置；
- 后续 Policy、Approval、Quota 管理。

Admin API 不直接执行 Agent 图，不导入 harness 内部实现。

MVP 对外 Console 契约统一为 `/api/admin/v1/orgs/{org_id}/console/*`。目标架构和路线图中的 `routers/console.py` 只表示可复用的上游实现模块；已有旧路径如需保留，必须挂到 Compatibility API 并标记 deprecated。

### 2.3 Studio API

负责：

- AgentPackage、AgentVersion；
- 文件态 Agent/Skill 导入；
- Catalog 元数据；
- 校验、评审、发布、晋升与回滚；
- Release Channel 查询；
- 后续评测与发布门禁。

Studio API 只改变控制面状态。真正创建 Run 仍调用 Runtime API，并由 Runtime 解析已发布 ReleaseRef。

### 2.4 Internal API

仅用于可信服务间调用，例如：

- Worker lease / heartbeat / reconcile；
- 审计 outbox 投递；
- Sandbox Provisioner；
- 内部健康和就绪检查；
- 后续独立 Policy / Model Gateway。

要求：

- 不使用浏览器 Session；
- 使用 mTLS、短期工作负载身份或部署平台身份；
- 网络层不对公网暴露；
- 明确调用方 allowlist；
- 不能仅凭 `X-Internal: true` 等客户端 Header 信任请求。

### 2.5 Compatibility API

兼容路由必须：

1. 进入统一认证、TenantContext、RBAC 和审计中间件；
2. 转换为 Runtime Service 调用，不直接绕过仓储；
3. 明确不支持的企业字段与错误行为；
4. 在 OpenAPI 标记 `deprecated` 或 compatibility；
5. 不允许客户端提供可信 `org_id`；
6. 建立与原上游行为的回归测试。

---

## 3. 依赖与代码边界

```text
routers
  → application services
    → repositories / control-plane adapters
      → database / contracts implementations

runtime application service
  → deerflow.contracts
  → deerflow runtime
```

禁止：

- Router 直接编写跨表业务事务；
- Admin Router 调用 harness 内部对象；
- harness 调用 Admin HTTP API 或导入控制面 ORM；
- Compatibility Router 绕过统一授权；
- 前端直接访问 Internal API。

建议目录：

```text
backend/app/gateway/routers/runtime/
backend/app/gateway/routers/admin/
backend/app/gateway/routers/studio/
backend/app/gateway/routers/internal/
backend/app/gateway/routers/compat/
```

如果保留单个 Gateway 进程，逻辑模块也必须按上述边界组织。

---

## 4. 认证方式

| API | 浏览器 Session / OIDC | API Key | Workload Identity | 匿名 |
| --- | --- | --- | --- | --- |
| Runtime | 是 | 是 | 可选 | 否 |
| Admin | 是 | 限受控自动化 | 可选 | 否 |
| Studio | 是 | 限发布自动化 | 可选 | 否 |
| Internal | 否 | 否 | 是 | 否 |
| Health liveness | 否 | 否 | 否 | 可在内网匿名 |
| Health readiness 详情 | 运维身份 | 否 | 是 | 否 |

规则：

- OIDC 使用 `issuer + subject` 绑定平台 User；
- Session 必须防 CSRF，写操作不得只依赖 SameSite；
- API Key 固定绑定 ServiceAccount 和 Org；
- API Key 不允许通过 query string 传递；
- 原始 Token、Cookie、Key 不进入日志和 AuditEvent；
- 认证成功不等于授权成功。

---

## 5. Organization 与 Workspace 解析

### 5.1 允许的 Org 选择方式

浏览器用户：

- 从已认证 User 的 Membership 列表选择活动 Org；
- Org 可通过路径段、受控 Header 或 Session active-org 表达；
- 服务端必须验证 Membership；
- 不接受请求体中的 `org_id` 作为授权依据。

API Key：

- Org 固定来自 Key 关联的 ServiceAccount；
- 路径中的 Org 如与 Key 不一致，按跨 Org 请求处理；
- 不允许 API Key 切换 Org。

Internal：

- 从可信任务信封或 workload identity 解析；
- system scope 跨 Org fan-out 必须显式逐 Org 执行。

### 5.2 推荐表达

Admin / Studio 资源路径显式包含 Org：

```text
/api/admin/v1/orgs/{org_id}/...
/api/studio/v1/orgs/{org_id}/...
```

Runtime 路由可使用当前 TenantContext：

```text
/api/runtime/v1/threads
/api/runtime/v1/runs
```

Runtime 响应仍返回 `org_id` 供诊断，但客户端不能用其扩大权限。

### 5.3 Workspace

- 可通过路径或查询参数选择；
- 服务端验证 Workspace 属于当前 Org；
- MVP 权限仍按 Org 角色判断；
- 列表查询未指定 Workspace 时默认覆盖当前 Org 所有可见 Workspace；
- 如产品希望默认只看一个 Workspace，必须由前端筛选，不得改变服务端授权语义。

---

## 6. 授权与资源存在性

### 6.1 执行顺序

```text
authenticate
→ resolve org membership / service account
→ bind TenantContext
→ authorize operation
→ load resource scoped by org_id
→ execute
→ emit audit / metrics
```

对于按资源 ID 读取，仓储查询必须同时带 `org_id`，不能先全局加载再在应用层比较。

### 6.2 `401` / `403` / `404`

| 状况 | 响应 |
| --- | --- |
| 未认证、Token 无效或过期 | `401 Unauthorized` |
| 目标 Org 不在主体 Membership / Key 范围内 | `404 Not Found` |
| Membership 为 `invited` / `removed` | `404 Not Found`，不形成活动 Org 范围 |
| Membership 为 `suspended` | `403 Forbidden`，业务错误码 `permission_denied` |
| 当前 Org 内资源不存在 | `404 Not Found` |
| 当前 Org 内资源存在，但主体缺少明确操作权限 | `403 Forbidden` |
| Workspace 不属于当前 Org | `404 Not Found` |
| Policy 拒绝高风险动作 | `403 Forbidden`，业务错误码 `policy_denied` |
| Org 为 `suspended`，主体尝试新建 Run 或发布 | `403 Forbidden`，业务错误码 `org_suspended` |
| Org 为 `deleting`，主体尝试新 Run、发布或普通写操作 | `403 Forbidden`，业务错误码 `org_deleting` |

对外错误消息不能说明另一个 Org 中是否存在同 ID 资源。跨 Org 探测应产生安全审计或速率告警，但避免把每个普通 404 都记录为高成本审计事件。

---

## 7. 权限命名

MVP 建议使用：

```text
runtime:thread:read
runtime:thread:write
runtime:run:create
runtime:run:read
runtime:run:cancel
runtime:run:resume
system:org:create
system:org:read_all
system:org:operate_all
admin:org:read
admin:org:manage
admin:iam:read
admin:iam:manage
admin:audit:read
admin:console:read
studio:package:read
studio:package:write
studio:release:promote_dev
studio:release:promote
studio:release:rollback
connector:read
connector:manage
```

正式角色与服务账号语义见[ADR-0003](../adr/0003-rbac-and-service-accounts.md)，可执行矩阵见[测试策略 §9.1](../engineering/testing-strategy.md#91-权限矩阵)。Router 只能调用统一授权服务：

```text
authorize(context, permission, resource_ref?) -> allow | raise
```

禁止在不同 Router 中手写角色名判断。

`system:org:create`、`system:org:read_all`、`system:org:operate_all` 仅授予 `system:admin`，不属于任何 Org 角色；默认组织 bootstrap 可以通过受控部署命令执行同一应用服务。

---

## 8. MVP 端点目录

端点路径可在 Fork 后调整，但职责和控制项必须保留。

### 8.1 Runtime

| 方法与路径 | 权限 | 幂等 | Audit |
| --- | --- | --- | --- |
| `POST /threads` | `runtime:thread:write` | 建议 | 普通业务事件，不强制合规审计 |
| `GET /threads` | `runtime:thread:read` | 不适用 | 否 |
| `GET /threads/{thread_id}` | `runtime:thread:read` | 不适用 | 敏感读取可按 Policy 审计 |
| `POST /runs` | `runtime:run:create` | **必须** | 创建事实进入 Run journal；策略拒绝进 Audit |
| `GET /runs/{run_id}` | `runtime:run:read` | 不适用 | 否 |
| `POST /runs/{run_id}/cancel` | `runtime:run:cancel` | **必须** | 可进入 Audit / 运维事件 |
| `POST /runs/{run_id}/resume` | `runtime:run:resume` | **必须** | 重评估 admission；审批恢复产生 Audit |
| `GET /runs/{run_id}/events` | `runtime:run:read` | SSE 重连 | 否 |
| `GET /runs/{run_id}/artifacts` | `runtime:run:read` | 不适用 | 敏感下载按 Policy 审计 |

Resume 只允许处于可恢复中断状态的 Run。恢复前重新验证 Principal、Org 状态、ReleaseRef 可用性与 Run admission Policy；已完成步骤不重放。MVP 未启用企业审批适配器时，`approval_required` 状态不能通过普通 resume 绕过。

### 8.2 Admin

| 方法与路径 | 权限 | 幂等 / 并发 | Audit |
| --- | --- | --- | --- |
| `POST /orgs` | `system:org:create` | Idempotency-Key | 必须 |
| `GET /orgs/{org_id}` | `admin:org:read` | 不适用 | 否 |
| `PATCH /orgs/{org_id}` | `admin:org:manage` | If-Match | 必须 |
| `POST /orgs/{org_id}/memberships` | `admin:iam:manage` | Idempotency-Key | 必须 |
| `POST /orgs/{org_id}/role-bindings` | `admin:iam:manage` | Idempotency-Key | 必须 |
| `DELETE /orgs/{org_id}/role-bindings/{id}` | `admin:iam:manage` | 重复删除安全 | 必须 |
| `POST /orgs/{org_id}/service-accounts` | `admin:iam:manage` | Idempotency-Key | 必须 |
| `POST /orgs/{org_id}/api-keys` | `admin:iam:manage` | 不自动重放 Secret 响应 | 必须 |
| `DELETE /orgs/{org_id}/api-keys/{id}` | `admin:iam:manage` | 重复撤销安全 | 必须 |
| `GET /orgs/{org_id}/audit-events` | `admin:audit:read` | Cursor 分页 | 查询本身建议审计 |
| `GET /orgs/{org_id}/console/stats` | `admin:console:read` | 不适用 | 否 |
| `GET /orgs/{org_id}/console/runs` | `admin:console:read` | Cursor 分页 | 否 |
| `GET /orgs/{org_id}/console/usage` | `admin:console:read` | 时间范围 | 否 |

### 8.3 Studio

| 方法与路径 | 权限 | 幂等 / 并发 | Audit |
| --- | --- | --- | --- |
| `POST /orgs/{org_id}/agent-packages` | `studio:package:write` | Idempotency-Key | 必须 |
| `POST /orgs/{org_id}/agent-packages/{id}/versions` | `studio:package:write` | digest 去重 | 必须 |
| `POST /orgs/{org_id}/agent-packages/{id}/versions/{version_id}/review` | `studio:package:write` | Idempotency-Key + If-Match | `catalog.agent_version.reviewed` |
| `POST /orgs/{org_id}/agent-packages/{id}/versions/{version_id}/publish` | `studio:release:promote` | Idempotency-Key + If-Match | `catalog.agent_version.published` |
| `POST /orgs/{org_id}/agent-packages/{id}/versions/{version_id}/revoke` | `studio:release:promote` | Idempotency-Key + If-Match | `catalog.agent_version.revoked` |
| `GET /orgs/{org_id}/agent-packages` | `studio:package:read` | Cursor 分页 | 否 |
| `GET /orgs/{org_id}/release-channels` | `studio:package:read` | 不适用 | 否 |
| `POST /orgs/{org_id}/release-channels/{id}/promote` | dev：`studio:release:promote_dev` 或 `studio:release:promote`；staging/prod：`studio:release:promote` | Idempotency-Key + If-Match | 必须 |
| `POST /orgs/{org_id}/release-channels/{id}/rollback` | `studio:release:rollback` | Idempotency-Key + If-Match | 必须 |
| `GET /orgs/{org_id}/catalog` | `studio:package:read` | Cursor 分页 | 否 |

---

## 9. 请求幂等

### 9.1 Header

写端点使用：

```text
Idempotency-Key: <client-generated opaque value>
```

规则：

- 唯一范围至少是 `org_id + principal_id + endpoint + key`；
- 同 Key、同请求摘要返回原结果；
- 同 Key、不同请求摘要返回 `409 idempotency_conflict`；
- 保存时间不少于客户端最大重试窗口；
- Run 创建、发布、回滚和关键管理操作必须支持；
- API Key 创建不能在重放时再次返回完整 Secret，重放应返回元数据并提示 Secret 已仅展示一次。

### 9.2 状态转换

Run cancel、release promote/rollback 使用 compare-and-set 或 `If-Match`。并发冲突返回 `409 Conflict`，不得最后写入静默覆盖。

---

## 10. 分页、过滤和排序

列表默认 Cursor 分页：

```text
GET ...?limit=50&cursor=<opaque>&sort=-created_at
```

响应：

```json
{
  "data": [],
  "page": {
    "next_cursor": null,
    "has_more": false
  },
  "request_id": "..."
}
```

规则：

- 默认 `limit=50`，最大 `200`；
- Cursor 包含稳定排序键和租户范围，但需签名或不可伪造；
- 所有过滤先应用 `org_id`；
- Console 时间范围必须有上限，MVP 建议默认 24 小时、最大 90 天；
- 禁止客户端指定任意 SQL 字段和表达式；
- Audit 导出使用异步 Job，不能通过超大同步分页绕过限制。

---

## 11. 错误响应

统一 envelope：

```json
{
  "error": {
    "code": "permission_denied",
    "message": "The requested operation is not allowed.",
    "retryable": false,
    "details": {}
  },
  "request_id": "req_..."
}
```

### 11.1 HTTP 映射

| Contract Code | HTTP |
| --- | --- |
| `tenant_context_missing` | 500；边界层配置缺陷 |
| `tenant_mismatch` | 404 |
| `authentication_invalid` | 401 |
| `principal_disabled` | 403 |
| `org_suspended` | 403；只读、审计与受控导出仍按权限开放 |
| `org_deleting` | 403；仅删除流程、受控导出、审计和取消仍开放 |
| `permission_denied` | 403 |
| `policy_denied` | 403 |
| `policy_unavailable` | 503 |
| `approval_required` | 409；返回稳定 interrupt 引用 |
| `release_not_found` | 404 |
| `release_not_published` | 409 |
| `release_revoked` | 409 |
| `release_unpinned` | 409 |
| `release_tenant_mismatch` | 404 |
| `release_conflict` | 409 |
| `run_conflict` | 409 |
| `idempotency_conflict` | 409 |
| `audit_unavailable` | 503 |
| `validation_error` | 422 |
| `rate_limited` | 429 |

`details` 只包含安全白名单信息。生产环境不返回堆栈、SQL、内部路径、策略全文或资源所属 Org。

---

## 12. SSE 与 Run 控制

### 12.1 事件流

```text
GET /api/runtime/v1/runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: <optional>
```

要求：

- 建立流前完成 Org 范围读取授权；
- 每个事件具有单调可恢复的 `event_id`；
- 支持 `Last-Event-ID` 重连；
- keepalive 不计作业务首事件 SLI；
- 不把另一个 Org 的 StreamBridge channel 复用到当前连接；
- 客户端断开不自动取消 Run；
- 流结束前发送明确 terminal event，异常断开可通过 GET Run 查询最终状态。

### 12.2 Cancel

- Cancel 是幂等请求；
- 已 terminal 的 Run 返回当前终态，不重新写状态；
- cancel 与 completion 竞争使用原子状态转换；
- 多副本时通过 run ownership / lease 传播；
- 超时未完成 cancel 进入 reconcile，不向客户端伪报已取消。

---

## 13. Release API 原子性

Promote / rollback 请求至少包含：

```text
target_version_id
expected_channel_version
reason
```

单事务完成：

1. 校验 Org、Package、Version；
2. 校验 Version 状态和 digest；
3. CAS 更新 ReleaseChannel；
4. 写 ReleaseEvent；
5. 写 Audit outbox。

Run 创建在单个一致性边界内：

1. 解析当前 Channel；
2. 获取不可变 AgentVersion；
3. 持久化完整 ReleaseRef / digest 到 Run；
4. 提交 Run；
5. 执行阶段不再读取 Channel。

---

## 14. 安全控制

### 14.1 输入

- 所有请求体有大小上限；
- 上传 Agent 制品有类型、大小、摘要和恶意内容检查；
- URL 字段执行 SSRF 防护；
- 过滤、排序、搜索字段采用 allowlist；
- 文件名不直接拼接本地路径；
- Webhook 验证签名、时间窗和重放。

### 14.2 输出

- 默认不返回 Secret、Key Hash、保险库路径细节；
- API Key 明文只在创建成功时返回一次；
- 日志和错误响应脱敏；
- 对象下载使用短期签名 URL 或受控流；
- CORS 使用明确 Origin allowlist，不使用带凭证的 `*`。

### 14.3 速率限制

至少按以下维度：

- 来源 IP：登录、Webhook、未完成认证路径；
- principal + org：Runtime 创建 Run；
- principal：API Key 创建、发布、回滚；
- org：并发 Run 和重型 Console 查询。

`429` 返回 `Retry-After`。策略拒绝不能通过重试绕过。

---

## 15. OpenAPI 与变更治理

### 15.1 文档所有权

- Runtime OpenAPI：Runtime / Gateway Owner；
- Admin OpenAPI：Control Plane Owner；
- Studio OpenAPI：Studio / Release Owner；
- Internal OpenAPI：Platform Runtime Owner，不发布到公共开发者站点。

### 15.2 版本

- URL 主版本从 `/v1` 开始；
- 新增可选响应字段视为兼容；
- 删除、重命名、语义变化和默认权限变化属于破坏性变更；
- 破坏性变更需新主版本或兼容适配层；
- Deprecated 端点至少保留一个约定发布周期并有迁移指南。

### 15.3 PR 门禁

端点变更必须同时提交：

1. OpenAPI diff；
2. 权限与租户范围说明；
3. AuditEvent 影响；
4. 幂等与并发说明；
5. 正常、越权、跨 Org、重试测试；
6. 前端或 SDK 兼容影响；
7. 回滚方式。

---

## 16. 契约测试

### 16.1 通用

- [ ] 所有租户端点缺少认证返回 401
- [ ] OrgA 主体访问 OrgB 路径和资源 ID 返回 404
- [ ] 当前 Org 内权限不足返回 403
- [ ] 客户端请求体不能覆盖 TenantContext
- [ ] 响应始终包含 `request_id`
- [ ] 错误响应不包含堆栈与内部敏感信息

### 16.2 Runtime

- [ ] Run 创建幂等
- [ ] Run 保存 ReleaseRef 与 Policy version
- [ ] SSE 重连不跨 Org、不重复越界事件
- [ ] cancel 与 terminal 竞争最终一致

### 16.3 Admin

- [ ] RoleBinding 写入与删除产生审计事件
- [ ] API Key 只展示一次且可撤销
- [ ] Console stats / runs / usage 强制按 Org 聚合
- [ ] Audit 查询不能跨 Org

### 16.4 Studio

- [ ] prod 拒绝 draft / revoked Version
- [ ] promote / rollback 通过 CAS 防止丢失更新
- [ ] 发布事务同时产生 ReleaseEvent 与 Audit outbox
- [ ] 文件导入后执行只引用 digest

### 16.5 Compatibility

- [ ] 上游关键客户端路径可用
- [ ] 兼容路由经过统一鉴权与租户解析
- [ ] 不支持的上游行为返回明确错误，不静默降级安全控制

---

## 17. Fork 后待确认项

1. 上游已有路由前缀、LangGraph 兼容端点与 OpenAPI 生成方式；
2. 当前 Session/OIDC 中间件、CSRF 与 CORS 行为；
3. Console Router 的实际查询和权限装饰器；
4. StreamBridge SSE 事件 ID、重连与多副本能力；
5. Run cancel / resume 的现有状态码和幂等语义；
6. 前端客户端是否依赖未版本化路径；
7. 内部 Provisioner、Scheduler、IM 的现有通信方式。

确认后可以调整路径，但不能弱化本文定义的 API 所有权、租户隔离和授权规则。
