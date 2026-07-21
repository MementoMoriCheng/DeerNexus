# DeerNexus 运行时稳定契约

> 状态：MVP 契约草案  
> 版本：`v1alpha1`  
> 关联：[目标架构](target-architecture.md) · [ADR-0001](../adr/0001-fork-evolution-strategy.md) · [ADR-0002](../adr/0002-tenant-workspace-keys.md) · [API 边界](api-boundaries.md)

本文冻结控制面与 DeerFlow Runtime Kernel 之间的最小稳定协议。目标是让 `packages/harness/deerflow` 只依赖 DTO、Protocol 和 ContextVar，不导入 `app.control_plane.*`，同时保证租户、策略、发布、审计和用量语义可测试。

---

## 1. 设计原则

1. **内核不认识控制面表**：harness 不读取 Organization、RoleBinding、AgentPackage 等私有表。
2. **入口解析，运行时消费**：Gateway、Worker、Scheduler、Channel Adapter 负责构造可信上下文。
3. **默认拒绝**：租户上下文、权限结果或发布引用缺失时 fail-closed。
4. **Run 级可重现**：创建 Run 时固定租户、Agent 制品和普通策略版本；在途 Run 不跟随通道漂移。
5. **高风险动作实时决策**：高风险工具每次调用都评估当前策略，避免长 Run 使用过期授权。
6. **幂等可追踪**：异步信封、审计和用量记录具有稳定事件 ID 或幂等键。
7. **契约可演进**：字段只追加，破坏性变更升级主版本并提供兼容窗口。

---

## 2. 包边界

目标目录：

```text
backend/packages/harness/deerflow/contracts/
├── __init__.py
├── context.py        # TenantContext + ContextVar helpers
├── identity.py       # PrincipalRef
├── policy.py         # PolicyRequest / PolicyDecision / PolicyEvaluator
├── release.py        # ReleaseRef / ReleaseResolver
├── approval.py       # ApprovalTicket（MVP 仅预留）
├── events.py         # AuditEvent / UsageRecord / EventSink
├── runs.py           # RunEnvelope
└── errors.py         # 稳定错误码
```

依赖方向：

```text
deerflow runtime → deerflow.contracts ← app.control_plane adapters
```

允许：

- contracts 依赖 Python 标准库和无业务语义的基础类型；
- app 层实现 contracts 中的 Protocol；
- Gateway 在进入 harness 前绑定 TenantContext。

禁止：

- contracts 导入 ORM Model、FastAPI Router 或控制面 Service；
- harness 导入 `app.control_plane.*`；
- 运行时以 SQL、HTTP 路由细节或 UI 状态作为契约；
- 控制面把可变 ORM 对象直接传给 harness。

---

## 3. 通用类型约定

| 类型        | 约定                                                           |
| ----------- | -------------------------------------------------------------- |
| ID          | UUID 的规范小写字符串；外部系统 ID 可为字符串但必须带 provider |
| 时间        | UTC、RFC 3339、微秒可选；持久化使用带时区 timestamp            |
| 枚举        | 小写 snake_case；未知值由消费者安全拒绝或忽略，不能静默映射    |
| 金额        | 十进制定点字符串 + ISO 4217 币种，不使用浮点数                 |
| Token       | 非负整数                                                       |
| Metadata    | JSON 对象；只允许显式白名单字段，不放密钥或完整 Prompt         |
| Schema 版本 | `schema_version`，初始值 `v1alpha1`                            |

所有跨进程 DTO 必须可 JSON 序列化。进程内可以使用不可变 dataclass/Pydantic model，但序列化结果必须符合本文字段。

---

## 4. PrincipalRef

```text
PrincipalRef(
  type: "user" | "service_account" | "system",
  id: str,
  user_id: str | None,
  display_name: str | None
)
```

规则：

- `id` 是稳定审计主体 ID；
- `display_name` 只用于展示，不参与授权；
- `service_account` 的 `user_id` 必须为空；
- `system` 只用于平台任务，不可伪装为用户；
- 不在该 DTO 中传递角色或 permission，避免长期运行持有过期权限集合。

---

## 5. TenantContext

```text
TenantContext(
  schema_version: "v1alpha1",
  org_id: str,
  workspace_id: str | None,
  principal: PrincipalRef,
  auth_method: "oidc" | "session" | "api_key" | "internal",
  request_id: str,
  trace_id: str | None,
  issued_at: datetime
)
```

### 5.1 不变量

- 租户请求 `org_id` 非空；
- `workspace_id` 存在时必须属于 `org_id`；
- Context 对象不可变；
- 不包含密钥、原始 Token、Session Cookie 或完整 OIDC claims；
- 不把客户端请求体中的 `org_id` 当作可信来源；
- Run 创建后，Run 的 `org_id` 不可修改。

### 5.2 ContextVar 生命周期

统一提供：

```text
bind_tenant_context(context) -> token
get_tenant_context() -> TenantContext
reset_tenant_context(token) -> None
require_tenant_context() -> TenantContext
```

使用规则：

1. Gateway 在鉴权、组织解析和 Workspace 校验后绑定；
2. 绑定必须使用 `try/finally` 恢复；
3. 创建后台任务时显式复制需要的值，不依赖隐式线程继承；
4. Worker 从 RunEnvelope 重建上下文；
5. 测试用例结束必须断言没有遗留上下文；
6. 缺少上下文时抛出稳定错误 `tenant_context_missing`，不得回退默认 Org。

### 5.3 日志传播

日志上下文至少包含：

```text
request_id, trace_id, org_id, workspace_id, principal_type,
principal_id, thread_id?, run_id?, release_digest?
```

禁止把 `org_id` 作为无界高基数指标标签直接写入公共 Metrics；按 Org 的分析通过日志、Trace 或 UsageRecord 完成。

---

## 6. RunEnvelope

RunEnvelope 是 Gateway、Scheduler、Channel 与执行器之间的可信任务信封：

```text
RunEnvelope(
  schema_version: "v1alpha1",
  run_id: str,
  thread_id: str,
  tenant: TenantContext,
  release_ref: ReleaseRef,
  policy_snapshot: PolicySnapshotRef,
  created_at: datetime,
  idempotency_key: str,
  source: "api" | "scheduler" | "channel" | "webhook" | "internal",
  source_ref: str | None,
  integrity: EnvelopeIntegrity | None
)
```

策略快照引用：

```text
PolicySnapshotRef(
  schema_version: "v1alpha1",
  policy_version: str,
  evaluated_at: datetime,
  expires_at: datetime | None
)
```

`policy_version` 同时持久化到 Run。它标识 Run admission 和普通装载所依据的策略版本；高风险动作仍按 §7.3 逐次实时评估。

跨信任边界传输时的完整性信息：

```text
EnvelopeIntegrity(
  algorithm: "hmac-sha256" | "jwt",
  key_id: str,
  signature: str
)
```

约束：

- `run_id + org_id` 唯一；
- `tenant.org_id` 必须与持久化 Run、Thread、ReleaseRef 一致；
- 信封从可信数据库或签名消息队列读取，不接受客户端直接提交；
- Gateway 与内嵌执行器从同一可信数据库读取时 `integrity` 可以为空；经过消息队列或跨信任边界传输时必须携带并验证 `integrity`；
- 重复消费相同 `idempotency_key` 不得创建第二个 Run；
- Worker 执行前重新验证 Run 仍处于可执行状态；
- `source_ref` 只保存引用，不保存外部事件中的密钥。

---

## 7. Policy 契约

### 7.1 PolicyRequest

```text
PolicyRequest(
  schema_version: "v1alpha1",
  request_id: str,
  tenant: TenantContext,
  run_id: str | None,
  action: str,
  resource: ResourceRef,
  risk_class: "low" | "medium" | "high" | "critical",
  context: dict
)
```

`ResourceRef`：

```text
ResourceRef(
  type: str,
  id: str | None,
  org_id: str,
  workspace_id: str | None,
  attributes: dict
)
```

`context` 只允许策略所需的白名单属性，例如工具名、目标域名、模型 ID、数据分类；禁止放完整 Secret、Prompt 或文件内容。

### 7.2 PolicyDecision

```text
PolicyDecision(
  schema_version: "v1alpha1",
  decision: "allow" | "deny" | "require_approval",
  reason_code: str,
  reason: str,
  rule_id: str | None,
  policy_version: str,
  evaluated_at: datetime,
  expires_at: datetime | None,
  obligations: list[PolicyObligation]
)
```

```text
PolicyObligation(
  type: "audit" | "redact" | "limit" | "approval_stub",
  parameters: dict
)
```

`parameters` 必须按 obligation 类型使用白名单 Schema；未知 obligation 必须安全拒绝，不能忽略后继续执行。

MVP 支持的 obligation：

- `audit`：必须产生指定类型 AuditEvent；
- `redact`：对结果应用指定脱敏规则；
- `limit`：施加超时、大小、token 或网络范围限制；
- `approval_stub`：返回待审批中断信息；MVP 不交付完整审批工单系统。

### 7.3 评估时机

| 动作                               | MVP 策略                                     |
| ---------------------------------- | -------------------------------------------- |
| 创建 Run、选择模型、加载普通 Skill | 创建 Run 时评估并记录 `policy_version`       |
| 读取 Agent ReleaseRef              | 创建 Run 时评估并固定                        |
| 高风险/关键工具调用                | 每次调用实时评估                             |
| 外部网络、写操作、凭证访问         | 每次调用实时评估                             |
| 长 Run 恢复                        | 恢复前重评估 Run admission；已完成步骤不重放 |

高风险判断由工具元数据与租户策略共同决定，调用方不得自行降级 `risk_class`。

### 7.4 超时和失败

- `high` / `critical` 动作评估超时：`deny`，错误码 `policy_unavailable`；
- `low` / `medium` 动作：MVP 默认同样 fail-closed；后续如需缓存放行必须单独 ADR；
- `require_approval` 但审批能力未启用：安全中断，不得当作 `allow`；
- 每次 `deny`、`require_approval` 和评估异常均写审计事件；
- PolicyEvaluator 不得返回空 decision。

### 7.5 Protocol

```text
PolicyEvaluator.evaluate(request: PolicyRequest) -> PolicyDecision
```

实现可以是进程内适配器、缓存快照或远程服务，但 harness 只认识该 Protocol。MVP 优先使用进程内 app adapter，避免过早引入独立 Policy 服务。

---

## 8. Release 契约

### 8.1 ReleaseRef

```text
ReleaseRef(
  schema_version: "v1alpha1",
  org_id: str,
  workspace_id: str | None,
  package_id: str,
  agent_name: str,
  version: str,
  digest: str,
  channel: "dev" | "staging" | "prod",
  resolved_at: datetime
)
```

规则：

- `digest` 是不可变内容摘要，初始使用 `sha256:<hex>`；
- `version` 是展示和排序信息，执行身份以 digest 为准；
- `prod` 只能解析已发布且未撤销的版本；
- Run 创建后完整 ReleaseRef 持久化，不在执行阶段重新读取 channel；
- channel 回滚只影响回滚后新创建的 Run；
- 同 Org 内解析，禁止跨 Org 引用；
- 开发态文件 Agent 必须先导入为制品，才能进入 prod。
- 迁移标记为 `legacy_unpinned=true` 的 Run 在 prod 不得 admit、resume 或继续执行，只允许读取、取消和归档。

### 8.2 ReleaseResolver

```text
ReleaseResolver.resolve(
  tenant: TenantContext,
  agent_name: str,
  channel: str
) -> ReleaseRef
```

Resolver 位于 app adapter；harness 只消费已解析 ReleaseRef。解析失败抛出：

- `release_not_found`
- `release_not_published`
- `release_revoked`
- `release_tenant_mismatch`

---

## 9. ApprovalTicket（MVP 预留）

```text
ApprovalTicket(
  schema_version: "v1alpha1",
  ticket_id: str,
  org_id: str,
  run_id: str,
  action: str,
  risk_class: str,
  status: "pending" | "approved" | "rejected" | "expired",
  resume_token_ref: str,
  created_at: datetime,
  expires_at: datetime
)
```

MVP 约束：

- 仅允许 Runtime 产生 `require_approval` 中断和稳定引用；
- 不交付会签、SLA、审批 UI 或完整状态机；
- `resume_token_ref` 是安全引用，不直接暴露可复用 Token；
- 未实现审批适配器时，Run 保持安全终止或明确的不可恢复等待状态；
- `ask_clarification` 不得创建 ApprovalTicket。

---

## 10. AuditEvent

```text
AuditEvent(
  schema_version: "v1alpha1",
  event_id: str,
  idempotency_key: str,
  org_id: str | None,
  workspace_id: str | None,
  actor: PrincipalRef,
  action: str,
  resource: ResourceRef | None,
  outcome: "success" | "denied" | "failure",
  reason_code: str | None,
  request_id: str,
  trace_id: str | None,
  run_id: str | None,
  occurred_at: datetime,
  payload: dict
)
```

MVP 必须事件：

```text
auth.login
iam.role_binding.created
iam.role_binding.deleted
catalog.skill.changed
catalog.mcp.changed
policy.tool.denied
policy.approval.required
release.agent.published
release.agent.rolled_back
```

`release.agent.published` 表示 ReleaseChannel promote 成功；AgentVersion 状态进入 published 使用 ADR-0005 的 `catalog.agent_version.published`，不能混用。

规则：

- `payload` 使用事件类型白名单 Schema；
- 不记录 Secret、原始 Token、完整 Prompt 或工具原始敏感结果；
- 业务事务成功但审计写入失败时，高风险管理操作必须回滚或进入 outbox；
- 工具拒绝等运行事件可异步写入，但不得静默丢弃；
- 详细防篡改与保留由[ADR-0005](../adr/0005-audit-event.md)冻结。

EventSink：

```text
AuditSink.emit(event: AuditEvent) -> None
```

---

## 11. UsageRecord

```text
UsageRecord(
  schema_version: "v1alpha1",
  record_id: str,
  idempotency_key: str,
  org_id: str,
  workspace_id: str | None,
  run_id: str,
  release_digest: str,
  provider: str,
  model: str,
  attempt: int,
  input_tokens: int,
  output_tokens: int,
  cached_tokens: int,
  cost_amount: str | None,
  cost_currency: str | None,
  started_at: datetime,
  completed_at: datetime,
  status: "success" | "failure" | "cancelled"
)
```

规则：

- token 计量来源于模型适配器，不能由客户端提交；
- `org_id` 从 RunEnvelope 继承；
- provider 重试产生多条底层记录时，用 attempt 维度区分，汇总避免重复；
- 缺少价格表时允许 cost 为空，但 token 不得丢失；
- UsageRecord 是计量事实，不是账单；MVP 不做发票和 chargeback。

---

## 12. 稳定错误模型

错误响应内部统一包含：

```text
ContractError(
  code: str,
  message: str,
  retryable: bool,
  request_id: str,
  details: dict
)
```

MVP 错误码：

| Code                      | 语义                                               | Retryable        |
| ------------------------- | -------------------------------------------------- | ---------------- |
| `tenant_context_missing`  | 可信租户上下文不存在                               | 否               |
| `tenant_mismatch`         | 资源与当前 Org/Workspace 不一致                    | 否               |
| `authentication_invalid`  | 凭证无效、过期或无法映射主体                       | 否               |
| `principal_disabled`      | 已认证主体被禁用或撤销                             | 否               |
| `org_suspended`           | Org 已暂停，不允许新 Run 或发布                    | 否               |
| `org_deleting`            | Org 正在删除，只允许删除流程、受控导出、审计和取消 | 否               |
| `permission_denied`       | 已知作用域内权限不足                               | 否               |
| `policy_denied`           | 策略明确拒绝                                       | 否               |
| `policy_unavailable`      | 策略无法安全评估                                   | 视调用方退避重试 |
| `approval_required`       | 动作需要企业审批                                   | 否               |
| `release_not_found`       | 通道没有可用制品                                   | 否               |
| `release_not_published`   | prod 指向未发布版本                                | 否               |
| `release_revoked`         | 制品已撤销                                         | 否               |
| `release_unpinned`        | prod Run 缺少不可变 ReleaseRef / digest            | 否               |
| `release_tenant_mismatch` | 制品跨 Org                                         | 否               |
| `release_conflict`        | ReleaseChannel 的 If-Match / row version 冲突      | 可               |
| `run_conflict`            | 幂等键或状态转换冲突                               | 可               |
| `idempotency_conflict`    | 同幂等键对应不同请求                               | 否               |
| `audit_unavailable`       | 强审计写路径不可用                                 | 可               |
| `validation_error`        | 请求不符合 Schema 或业务前置条件                   | 否               |
| `rate_limited`            | 请求超过主体或 Org 限制                            | 可               |

对外 HTTP 映射见 [API 边界](api-boundaries.md)。内部日志可记录诊断信息，对外消息不得泄露资源存在性、策略细节或密钥信息。

---

## 13. 兼容性与版本策略

### 13.1 `v1alpha1`

Alpha 阶段允许调整字段，但每次变更必须：

- 更新本文；
- 更新生产者和消费者契约测试；
- 在同一 PR 中更新序列化 fixture；
- 标注迁移与回滚影响。

### 13.2 兼容规则

兼容变更：

- 新增可选字段；
- 新增消费者可忽略的 metadata；
- 新增错误码或枚举值，前提是旧消费者能安全拒绝。

破坏性变更：

- 删除、重命名或改变字段语义；
- 可选字段改必填；
- 改变默认授权、fail-open/fail-closed 或 Run pin 行为；
- 改变 ID、时间、摘要格式。

进入 `v1` 后，破坏性变更使用新主版本，旧版本至少保留一个发布周期，并提供双读或适配器。

---

## 14. 测试与验收

### 14.1 边界测试

- [ ] `packages/harness/deerflow` 不导入 `app.control_plane`
- [ ] contracts 不导入 ORM、Router 和 app Service
- [ ] app adapters 通过 Protocol 注入

### 14.2 Context 测试

- [x] 并发请求、协程、线程池之间不串 TenantContext
- [x] 请求完成和异常退出均恢复 ContextVar
- [ ] Worker、Scheduler、IM 可从可信信封重建上下文
- [x] 上下文缺失或 Org 冲突 fail-closed

### 14.3 Policy 测试

- [ ] 高风险工具逐次实时评估
- [ ] 评估超时不执行工具
- [ ] deny / require_approval 产生审计事件
- [ ] 普通策略版本随 Run 保存

### 14.4 Release 测试

- [ ] prod 不能解析草稿或已撤销版本
- [ ] channel 从 v1 晋升 v2 后，在途 Run 仍使用 v1 digest
- [ ] 回滚后新 Run 使用 v1，历史 Run 不变
- [ ] 跨 Org ReleaseRef 被拒绝

### 14.5 事件测试

- [ ] AuditEvent 与 UsageRecord 幂等
- [ ] 敏感字段不进入 payload
- [ ] 强审计管理操作在 sink 不可用时不提交
- [ ] UsageRecord 重试不重复计费

---

## 15. MVP 非目标

- 独立 Policy 微服务；
- 完整 Approval 工单与 UI；
- 实时账单、发票和 chargeback；
- 跨区域契约复制；
- Workspace 级 RBAC；
- 把 ORM Model 暴露为公共 SDK。

---

## 16. 实现状态

本文冻结契约语义；代码按 [PR 拆分指南](../engineering/pr-split-guide.md) Track A 分阶段交付。本节记录已落地的模块、对应 PR、测试 ID 与尚未实现部分，保证文档、代码和边界测试可双向追溯。字段语义以 §2–§14 为唯一来源，本节不重定义字段。

### 16.1 PR-010：基础包（已交付）

落地模块（`backend/packages/harness/deerflow/contracts/`）：

| 模块            | 内容                                                     | 对应章节 |
| --------------- | -------------------------------------------------------- | -------- |
| `versioning.py` | `CURRENT_SCHEMA_VERSION = "v1alpha1"`                    | §3、§13  |
| `identity.py`   | `PrincipalRef`、`PrincipalType`                          | §4       |
| `context.py`    | `TenantContext`、`AuthMethod`（DTO 与不变量）            | §5、§5.1 |
| `errors.py`     | `ContractError`、`ErrorCode` 注册表、`is_retryable_code` | §12      |
| `__init__.py`   | 公开导出与分阶段交付说明                                 | §2       |

关键不变量已在 DTO 层强制：

- `PrincipalRef`：`service_account` / `system` 不得携带 `user_id`；`id` 非空；`type` 为闭集 Literal，未知值拒绝；`extra="ignore"` 防止凭据随 DTO 传播。
- `TenantContext`：`org_id`、`request_id` 非空；`auth_method` 为闭集 Literal；`issued_at` 强制带时区并归一化为 UTC；`schema_version` 默认 `v1alpha1`；`extra="ignore"` 丢弃客户端 `org_id`、cookie、token 等未知字段。
- `ContractError`：`from_code` 根据 §12 表派生 `retryable`，禁止把安全相关失败误标为可重试；`request_id` 非空。

Canonical JSON fixture（`backend/tests/fixtures/contracts/`）：`principal_ref.json`、`tenant_context.json`、`contract_error.json`。

测试（`backend/tests/test_contracts_base.py`，标记 `CONTRACT-010-*`）：

- `CONTRACT-010-IDENT`：PrincipalRef 类别、`user_id` 约束、必填与未知字段丢弃；
- `CONTRACT-010-TENANT`：schema 版本、必填、`auth_method` 闭集、`issued_at` UTC 归一化与 naive 拒绝、`workspace_id` 可空、客户端字段不覆盖可信 `org_id`；
- `CONTRACT-010-IMMUTABLE`：`PrincipalRef` / `TenantContext` 冻结、嵌套冻结、`model_copy(update=...)` 仍可用；
- `CONTRACT-010-ERROR`：注册表与 §12 一致（21 码）、可重试集合、`tenant_context_missing` / `release_unpinned` 不可重试；
- `CONTRACT-010-FIXTURE`：fixture 载入、稳定往返、`v1alpha1` 与 UTC；
- `CONTRACT-010-COMPAT`：未知可选字段忽略、缺必填失败。

边界（`backend/tests/test_harness_boundary.py`，testing-strategy.md §8）：`deerflow.contracts` 仅允许标准库与 `pydantic`，禁止导入 `app.*`、ORM、FastAPI、LangGraph/LangChain 及其余 `deerflow.*` 业务子包；采用 allow-list fail-closed。

### 16.2 PR-010 不包含

显式排除（后续 PR 交付）：

- §5.2 ContextVar 生命周期（`bind_tenant_context` / `get_tenant_context` / `require_tenant_context` / `reset_tenant_context`）与 `TEN-001`~`TEN-009` → **PR-012**；
- 真实 OIDC / Membership 查询、Gateway Tenant 解析适配器、异步入口 Tenant 传播 → PR-013 / PR-014。

（§6–§11 的 DTO 与 Protocol 由 PR-011 交付，见 §16.3。）

### 16.3 PR-011：Policy / Release / Event 契约（已交付）

落地模块（`backend/packages/harness/deerflow/contracts/`）：

| 模块          | 内容                                                                                                                                               | 对应章节 |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| `policy.py`   | `ResourceRef`、`PolicyRequest`、`PolicyDecision`、`PolicyObligation`、`PolicyEvaluator` Protocol；`RiskClass` / `Decision` / `ObligationType` 闭集 | §7       |
| `release.py`  | `ReleaseRef`、`ReleaseResolver` Protocol；`ReleaseChannel` 闭集                                                                                    | §8       |
| `runs.py`     | `RunEnvelope`、`PolicySnapshotRef`、`EnvelopeIntegrity`；`EnvelopeSource` / `IntegrityAlgorithm` 闭集                                              | §6       |
| `approval.py` | `ApprovalTicket`（MVP 仅预留中断引用）、`ApprovalStatus` 闭集                                                                                      | §9       |
| `events.py`   | `AuditEvent`、`AuditSink` Protocol、`UsageRecord`、`UsageRecorder` Protocol；`AuditOutcome` / `UsageStatus` 闭集                                   | §10、§11 |

关键不变量已在 DTO 层强制：

- **Policy**：`ResourceRef.org_id` 非空（资源始终带租户）；`risk_class` 闭集且调用方不得降级；`decision` 闭集且永不返回空；`ObligationType` 闭集，未知 obligation 必须被消费者拒绝而非忽略（§7.2）；`context` 为白名单策略包，不含 Secret。
- **Release**：`digest` 为不可变内容身份（`sha256:<hex>`），`version` 仅展示；`channel` 闭集 `dev`/`staging`/`prod`；`org_id` 非空，禁止跨 Org 引用；`resolved_at` 强制带时区并归一化 UTC。prod 只解析 published 版本由 resolver 适配器强制，非 DTO。
- **RunEnvelope**：不可变、`extra="ignore"`；`idempotency_key` 非空（重复消费不得创建第二个 Run）；`source` 闭集；`integrity` 可空（同库读可空，跨信任边界必填并校验）；携带完整 `tenant` 与 `release_ref` 供适配器做一致性校验（§6、§7.3）。
- **AuditEvent**：`event_id` / `idempotency_key` 非空；`org_id` 仅系统全局事件可空（ADR-0002 §4.1）；`outcome` 闭集；`payload` 在 DTO 边界剥离 forbidden key（`api_key`、`cookie`、`oauth_token`、`full_prompt`、`full_model_response`、`full_file_content`、`signed_url_query`、`database_dsn` 等，ADR-0005 §6），防御性纵深。
- **UsageRecord**：token 为来自模型适配器的非负整数，不接受客户端提交；`org_id` 从 RunEnvelope 继承且非空；`cost_*` 可空（无价表时），但 token 不得丢失；`attempt` 区分供应商重试（§11）。

Protocol（`PolicyEvaluator`、`ReleaseResolver`、`AuditSink`、`UsageRecorder`）为结构化类型，app 层适配器满足签名即可注入；harness 只依赖 Protocol。

Canonical JSON fixture（`backend/tests/fixtures/contracts/`）：`policy_request.json`、`policy_decision.json`、`release_ref.json`、`run_envelope.json`、`audit_event.json`、`usage_record.json`、`approval_ticket.json`。

测试（`backend/tests/test_contracts_policy_release_event.py`，标记 `CONTRACT-011-*`）：Policy（资源 org、风险闭集、决策闭集、obligation 闭集）、Release（channel 闭集、digest/org 必填、UTC）、RunEnvelope（source 闭集、幂等键、integrity 可空与校验、pin 语义、tenant/release 可携带不同 Org 以暴露不一致）、ApprovalTicket（status 闭集、`resume_token_ref` 必填、`expires_at` 必填）、AuditEvent（outcome 闭集、event_id 必填、org 可空仅系统、forbidden payload key 全量拒绝）、UsageRecord（status 闭集、非负整数、cost 可空、release_digest 必填）、Protocol 可被 duck-type 满足、不可变（含嵌套）、fixture 往返与前向兼容。

边界：沿用 PR-010 的 allow-list（标准库 + `pydantic` + 内部）；新模块只依赖 `deerflow.contracts.*`、`pydantic`、标准库，fail-closed。

### 16.4 PR-011 不包含

显式排除（后续 PR 交付）：

- §5.2 ContextVar 生命周期与 `TEN-001`~`TEN-009` → **PR-012**；
- Policy / Release / Audit / Usage 的**具体 app 适配器实现**与跨 Org 一致性、签名校验、outbox 投递 → RBAC / Audit / Release Track（PR-030+ / PR-040+ / PR-050+）；
- Action 注册表（ADR-0005 §5 全量）与每 action 的 payload Schema → Audit Track；
- 真实 OIDC / Membership 解析与异步入口传播 → PR-013 / PR-014。

### 16.5 PR-012：ContextVar 生命周期（已交付）

落地模块（`backend/packages/harness/deerflow/contracts/context.py`）：§5.2 的四个生命周期函数与 `TenantContextError`，与 `TenantContext` DTO 同模块。

| 符号                                            | 语义                                                                                | 对应章节   |
| ----------------------------------------------- | ----------------------------------------------------------------------------------- | ---------- |
| `_current_tenant`                               | `ContextVar[TenantContext \| None]`，`default=None`，名为 `deerflow_current_tenant` | §5.2       |
| `bind_tenant_context(context) -> Token`         | 绑定当前任务上下文，返回 reset token                                                | §5.2       |
| `reset_tenant_context(token) -> None`           | 用 token 恢复先前值（必须 try/finally）                                             | §5.2       |
| `get_tenant_context() -> TenantContext \| None` | 读取或返回 None，**不回退默认 Org**                                                 | §5.2、§5.1 |
| `require_tenant_context() -> TenantContext`     | 未绑定时 `raise TenantContextError(TENANT_CONTEXT_MISSING)`                         | §5.2       |
| `TenantContextError(RuntimeError)`              | 携带稳定 `ErrorCode`，入口层可据此产出 `ContractError` 信封                         | §12        |

关键不变量已在代码层强制：

- `bind` 必须配 `reset`（调用方 try/finally）；测试以 autouse fixture 在每个用例 teardown 断言无残留（TEN-006）。
- `get` 永不合成默认 Org；`require` 缺失即抛 `tenant_context_missing`（fail-closed），不可重试。
- `TenantContextError` 携带 `ErrorCode` 而非字符串，PR-013/014 入口层据此稳定捕获，无需字符串匹配。

测试（`backend/tests/test_tenant_context_lifecycle.py`，标记 `TEN-001`~`TEN-008`）：绑定可读（TEN-001）、正常退出恢复含嵌套（TEN-002）、异常退出恢复（TEN-003）、并发协程 OrgA/OrgB 不串且子任务快照不被父任务后续 rebind 污染（TEN-004）、裸线程池不继承且 `copy_context().run` 转义有效（TEN-005）、autouse 断言无残留（TEN-006）、缺失抛 `tenant_context_missing` 且码不可重试（TEN-007）、不回退默认 Org（TEN-008）。

边界：`contextvars` 为标准库，已加入 `test_harness_boundary.py` 的 `CONTRACTS_ALLOWED_MODULES`（§2 允许标准库 + `pydantic`）；`context.py` 未引入 `app.*`、ORM、FastAPI 等被禁依赖。

### 16.6 PR-012 不包含

显式排除（后续 PR 交付）：

- `TEN-009`（数据库连接池复用时清理 tenant session state / RLS）→ DB 相关，属 CI `connection-pool tenant reuse` 阶段与 90 天测试出口（testing-strategy.md §22.1 / §27），不归 PR-012；
- Gateway Tenant 解析适配器、异步入口（RunEnvelope / Scheduler / IM-Webhook）Tenant 传播 → PR-013 / PR-014。

### 16.7 PR-013：Gateway Tenant 解析适配器（已交付）

落地模块（`backend/app/gateway/`）：

| 模块        | 内容                                                                                                                           | 对应章节                  |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------- |
| `config.py` | `DEFAULT_BOOTSTRAP_ORG_ID` 常量 + `GatewayConfig.default_org_id`（env `DEER_FLOW_DEFAULT_ORG_ID`）                             | §5.2、ADR-0001 §6         |
| `tenant.py` | `TenantResolutionMiddleware`、`resolve_principal`、`resolve_tenant_context`（单 Org bootstrap）、auth-source→`AuthMethod` 映射 | §5.2、api-boundaries §6.1 |
| `app.py`    | 中间件注册（tenant 在 auth 之前 add，因 `BaseHTTPMiddleware` 反序执行）                                                        | §5.2                      |

解析语义（单 Org bootstrap，ADR-0001 §6 / ADR-0002 §5 行 1）：

- 中间件在 `AuthMiddleware` 鉴权完成后读取 `request.state.user` / `auth_source`，构造可信 `TenantContext` 并 `bind_tenant_context`，`try/finally reset`（§5.2 rule 1/2）。
- `org_id` 来自配置的 bootstrap org，**不读请求体 org_id**（不可信，§5.1、ADR-0002 §2.1、TM-001）。
- `auth_source` 映射：`session→session`、`internal→internal`、`auth_disabled→internal`（契约 `AuthMethod` 无 `auth_disabled`）。
- 可信内部调用带 `X-DeerFlow-Owner-User-Id` 时，`principal.user_id` 取该 header（已信任）；`request_id` 取 `X-Request-Id` header，缺则合成。
- 非公开路径无认证 principal → fail-closed 503（`authentication_invalid`），不静默放行。
- bind 成功后 emit 结构化日志（`request_id/org_id/principal_type/principal_id/auth_method`，对齐 §5.3）。

测试（`backend/tests/test_gateway_tenant_resolver.py`，`TEN-入口` 系列 / TM-001）：session 解析到 bootstrap org、internal 调用 honor owner header、auth-disabled 仍 bind、客户端 org_id（header/query）被忽略、bootstrap org 可经 env 配置、try/finally 正常与异常退出恢复、`X-Request-Id` 透传与缺省合成、公开路径绕过。

边界：resolver 在 app 层（`app.gateway`），依赖方向 app→contracts；不动 `deerflow.contracts` allow-list（`contextvars` 已在 PR-012 加入）。`create_app()` 仍可构建（gate smoke 绿）。

### 16.8 PR-013 不包含

显式排除（后续 PR 交付）：

- 真实 Membership / OIDC group 查询（ADR-0003 §10 为 additive-only，MVP 不做 authoritative）→ RBAC Track（PR-030+）；
- 第二 Org 对外开放 → PR-025；
- 异步入口（RunEnvelope / Worker / Scheduler / IM-Webhook）Tenant 传播 → PR-014A/B/C；
- 持久层按 `org_id` owner 过滤（当前 bind 仅建立可信入口不变量与审计主体，尚无消费者）→ PR-024。

### 16.9 PR-014A：Worker RunEnvelope 重建 + Tenant 绑定（已交付）

落地模块：

| 模块                            | 内容                                                                      | 对应章节      |
| ------------------------------- | ------------------------------------------------------------------------- | ------------- |
| `app/gateway/tenant_rebuild.py` | `rebuild_tenant_context(envelope)`、`bind_tenant_from_envelope(envelope)` | §5.2 rule 4   |
| `runtime/runs/worker.py`        | `RunContext.tenant` 字段（可选，向后兼容）；`run_agent` 入口防御性 rebind | §5.2 rule 3/4 |
| `app/gateway/deps.py`           | `get_run_context` 从 contextvar 填充 `tenant`                             | §5.2          |

解析语义（§5.2 rule 3/4，ADR-0002 §3 invariant 6）：

- `rebuild_tenant_context` 从可信 `RunEnvelope.tenant` 重建 `TenantContext`——**从信封而非 contextvar 继承**，为未来物理 Worker 拆分（ADR-0006）铺路。envelope/tenant 缺失 → `TenantContextError(TENANT_CONTEXT_MISSING)` fail-closed（不回退默认 Org）。
- `run_agent` 入口防御性 rebind：若 `RunContext.tenant` 已设置但 contextvar 未设置（模拟继承失效或物理 Worker），从 `RunContext.tenant` 显式 rebind，不依赖隐式继承。contextvar 已设置时不覆盖（信任继承的作用域）。`finally` 恢复。
- 当前内嵌模式下 tenant 通过 `create_task` 继承 + 防御性 rebind 双重保障；RunEnvelope 暂不持久化（同进程不需要）。

测试（`backend/tests/test_tenant_rebuild.py`，`TEN-入口` Worker 系列 / TM-024）：rebuild 返回信封 tenant、bind 可读、org 来自信封非默认、None envelope fail-closed、Worker contextvar 未设置时防御性 rebind、contextvar 已设置时不覆盖、无 tenant 时无作用域运行、异常退出恢复。

边界：`worker.py` 在 `deerflow.runtime`，可依赖 `deerflow.contracts`（runtime→contracts 允许）；`tenant_rebuild.py` 在 app 层。不动 contracts allow-list。`RunContext.tenant` 为可选字段，现有 worker 测试无回归。

### 16.10 PR-014A 不包含

显式排除（后续 PR 交付）：

- **PR-014B Scheduler**：scheduler 模块完全 greenfield（无 ScheduledTask 模型、无 cron/APScheduler），属独立功能，待 scheduler 存在后做 tenant 传播；
- ~~**PR-014C Channel/IM/Webhook**~~：已在 PR-014C 交付（见 §16.11）；
- RunEnvelope 持久化与跨进程 `EnvelopeIntegrity` 校验（同进程不需要；属未来物理 Worker 拆分，ADR-0006）；
- `release_ref` / `policy_snapshot` 一致性校验（属 Release Track PR-050+ / RBAC Track PR-030+）；
- TEN-009（DB 连接池 RLS 清理）→ CI 阶段。

### 16.11 PR-014C：Channel / IM dispatch Tenant 直接绑定（已交付）

落地模块：

| 模块                      | 内容                                                                                                                                                                                                                              | 对应章节        |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `app/gateway/tenant.py`   | `resolve_channel_tenant_context(owner_user_id, request_id)`（无 `Request` 解析器，镜像 HTTP 路径的 `resolve_tenant_context`）；`channel_tenant_scope(owner_user_id, request_id)` contextmanager（bind/reset，owner 缺失时 no-op） | §5.2 rule 2/3/6 |
| `app/channels/manager.py` | `ChannelManager._handle_message` 在 `_apply_effective_owner` 后、现有 try/except 外层用 `channel_tenant_scope` 包裹分发                                                                                                           | §5.2 rule 3     |

解析语义（§5.2 rule 3，补足而非替代 HTTP 回环）：

- channel 触发的 run 创建走 **HTTP 回环**（`_owner_headers` → internal token → `TenantResolutionMiddleware`），tenant 已在接收侧绑定。但分发任务自身此前**从不** `bind_tenant_context`（`grep backend/app/channels` 零命中）。
- PR-014C 让分发任务自身成为可审计的 tenant-scoped 入口（§5.2 rule 3「显式非隐式」）：`_handle_message` 用 `channel_tenant_scope` 包住 COMMAND + CHAT 两条路径，per-dispatch 生成 `request_id`，`org_id` 来自配置（非合成默认值），`principal` 来自可信连接 owner，`auth_method="internal"`（回环恒走 internal token）。
- owner 缺失时 scope 为 no-op（镜像 `_owner_headers` 返回 `None`），**不合成默认 Org**（§5.2 rule 6）——回环仍是 fail-closed 关口。
- `with` 作最外层 scope，不重构现有 try/except；异常退出时 contextmanager 的 `finally` 恢复 contextvar（§5.2 rule 2）。

测试（`backend/tests/test_channel_tenant_binding.py`，`TEN-入口` Channel 系列 / TM-001 / TM-024）：解析器 org 来自 config、principal 命名自 owner、auth_method=internal、request_id 回显、issued_at 时区感知；scope bind/restore、异常退出 restore、owner 缺失 no-op；集成用例在 mock `runs.wait` 闭包内捕获 `get_tenant_context()`，证明分发期间 tenant 已绑定且 org 正确、owner 缺失时不绑定（10 测试）。

边界：纯 app 层改动，不动 contracts allow-list；不动 `_owner_headers` / 回环 HTTP 行为 / 运行准入。**诚实标注**：本 PR 是增量补强（回环路径已让接收侧 tenant 可用），价值在于分发任务自身成为 tenant-scoped 审计入口并为未来直驱 runtime 铺路。现有 channel 测试无回归（13 个 Windows 文件系统/symlink/UUID 时序失败在 clean main 同样存在，与本 PR 无关）。

### 16.12 PR-014C 不包含

显式排除（后续 PR 交付）：

- **PR-014B Scheduler**：继续阻塞（scheduler 模块 greenfield）；PR-014C 完成后 Track A 出口以 A+C 达成，Track B 解锁，B 不阻塞 Track B；
- 不在 `_owner_headers` 加 `X-Request-Id` 端到端传播（未来增强，本 PR 聚焦 tenant scope）；
- 不做 principal-disabled / Org-suspended / Workspace-mismatch 完整矩阵（需 Track B Membership 数据）；
- 不构建 IM webhook 路由（不存在；所有 IM 为出站 WebSocket/Socket Mode/长轮询连接）；
- 不直驱内嵌 runtime（当前仍走 HTTP 回环；直驱属未来 Worker 拆分）。

### 16.13 PR-024：Repository Org Scope（已交付）

落地模块（按 §5.2 rule 6 / data-model §11.2 的应用仓储强制过滤要求）：

| 模块                                                                                         | 内容                                                                                                                                                                                                                                   | 对应章节          |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- |
| `contracts/context.py`                                                                       | `AUTO_ORG` sentinel + `_OrgIdSentinel` + `resolve_org_id(value, *, method_name)`（三态：AUTO_ORG 读 bound tenant，缺失即 `RuntimeError` fail-closed；显式 `str` 覆盖；显式 `None` 绕过）                                               | §5.2、§11.2       |
| `persistence/thread_meta/{base,sql,memory}.py`                                               | `ThreadMetaStore` 全方法加 `org_id` kw；SQL/memory 实现写入盖戳、读取/变更按 `org_id` 过滤（与 `user_id` 并存做纵深防御）；`check_access` 跨 Org 行恒 deny（即便 `user_id is None` 的 permissive 模式）                                | §7.1              |
| `persistence/run/sql.py` + `runtime/runs/store/{base,memory}.py` + `runtime/runs/manager.py` | `RunStore.put` 仅 insert 盖戳 `org_id`（不可变，update 分支不覆盖）；`get`/`list_by_thread`/`delete` 加 `org_id` 谓词；`RunRecord.org_id` 字段经 `_store_put_payload` 显式线程化，保证 `RunManager` 重试仍 tenant-scoped               | §7.2              |
| `runtime/events/store/db.py`                                                                 | `DbRunEventStore.put`/`put_batch` 软读 tenant 盖戳 `org_id`（Worker PR-014A 防御性 rebind 后通常有 tenant）；`list_messages`/`list_events`/`list_messages_by_run`/`count_messages`/`delete_by_thread`/`delete_by_run` 加 `org_id` 谓词 | §10（run_events） |
| `persistence/feedback/sql.py`                                                                | `FeedbackRepository.create`/`upsert`（仅 insert 盖戳，org_id 不可变）；`get`/`list_by_run`/`list_by_thread`/`delete`/`delete_by_run`/`list_by_thread_grouped` 加 `org_id` 谓词                                                         | feedback          |
| `app/gateway/services.py` `start_run`                                                        | 从 `run_ctx.tenant.org_id` 解析并显式传入 `create_or_reject(org_id=...)` 与 `thread_store.create(org_id=...)`；旧行修复路径补 `org_id=None`                                                                                            | §5.2 rule 3       |
| `app/gateway/deps.py` 启动恢复、`app/gateway/routers/threads.py` 旧行修复                    | 显式 `org_id=None`（system-admin 等价扫描/修复路径）                                                                                                                                                                                   | §11.2             |

org scope 语义（与 §11.2 一致）：

- **硬过滤**：PR-023 backfill 已覆盖全部 4 张表（`threads_meta`/`runs`/`run_events`/`feedback`），存量行 `org_id` 全部非空（=默认 Org）。因此 PR-024 采用 `WHERE org_id = X` 硬过滤，无 NULL-tolerant 双读窗口；缺失 tenant 上下文且 `org_id=AUTO_ORG` → `RuntimeError` fail-closed（§5.2 rule 6，不回退默认 Org）。
- **纵深防御**：`org_id` 过滤与既有 `user_id` 过滤**并存**；移除 `user_id` 分支属 Contract 阶段 PR-025D。`org_id` 不可变：upsert/update 分支不覆盖行 `org_id`。
- **OrgA 看不到 OrgB 的证明**：每个入口（PR-013 中间件 / PR-014A Worker 防御性 rebind）已绑定可信 `TenantContext`；仓储读取经 `resolve_org_id` 取 `org_id` 作查询谓词；跨 Org 行被数据库层排除；`check_access` 对跨 Org 行恒 deny。
- **绕过**：仅显式 `org_id=None`（迁移脚本 / CLI / system-admin 扫描 / 启动恢复 / 旧行修复）。

测试（`backend/tests/test_org_isolation.py`，`TEN-隔离` 系列）：4 张表各做 OrgA 写入 → OrgB 读不到 / 改不了 / 删不掉、OrgA 可见；`run_events` 内容不泄漏（最敏感向量）；`org_id=None` 绕过可见全部；`AUTO_ORG` 无 tenant → fail-closed 抛错（10 测试）。`test_owner_isolation.py` 的 `_as_user` 现同步绑定默认 Org tenant（同 Org 内跨 user 隔离不变）。conftest autouse fixture 镜像生产每请求绑 tenant，并 seed 默认 Org 行满足 `org_id` FK。

边界：`resolve_org_id` 仅依赖 stdlib + 本模块既有 `_current_tenant`，不破坏 contracts allow-list（`test_harness_boundary.py` 绿）。依赖方向：`persistence` / `runtime` → `contracts`（已允许）。

### 16.14 PR-024 不包含

显式排除（后续 PR 交付）：

- `RunRepository` 单 `run_id` 主键的状态回写（`update_status` / `update_model_name` / `update_run_completion` / `update_run_progress`）：Worker 已持有该 run，不加 org 谓词以免破坏 writeback（与现状一致，它们也不收 `user_id`）；
- `RunRepository.list_pending` / `list_inflight`：启动恢复全量 system 扫描（已显式 `org_id=None`）；
- `RunRepository.aggregate_tokens_by_thread` / `FeedbackRepository.aggregate_by_run`：全局聚合，调用前已 thread-gated（归属校验）；纵深防御加 org 谓词留 follow-up；
- Memory/Artifact/Skill/MCP/Scheduler 资源（024B–E）：表尚不存在；
- `org_id NOT NULL` + 复合唯一约束（Enforce，PR-025A）、multi-org Feature Flag（PR-025B）、移除 `user_id` 分支与清理临时兼容索引（Contract，PR-025D）；
- 内存事件 store（`MemoryRunEventStore`）org 过滤：该 store 为 dev/test stub，`RunEventStore` ABC 本身不含过滤参数（与 `RunStore`/`ThreadMetaStore` 不同）；隔离矩阵在 SQL 后端（生产强制点）验证；
- RLS（§11.1 spike）、Audit 接入（Track D）。

### 16.15 PR-025A：Enforce `org_id` NOT NULL（已交付）

data-model.md §13.3 **Enforce** 阶段的 schema 部分。把 PR-024 已在应用层强制的 `org_id` 约束升级到数据库层：即便绕过应用（直连脚本、误操作、旧版本应用连接），也无法写入 NULL `org_id`，从数据侧兜底 §5.2 rule 6 fail-closed 不变量。

落地：

- 迁移 `0006_enforce_org_not_null`（链自 `0005_resource_org_id`）：4 张 Run-lifecycle 资源表（`threads_meta` / `runs` / `run_events` / `feedback`）`org_id` `nullable=True → False`；`threads_meta` 增 `UNIQUE(org_id, thread_id)`（§7.1，命名 `uq_threads_meta_org_thread`）。SQLite 经 `batch_alter_table` 重建，命名 FK `fk_<table>_org_id` RESTRICT 由 batch 反射重新应用，无损。
- 新增幂等 helper：`migrations/_helpers.py` `safe_set_column_nullable`（镜像 `safe_add_column` 的 inspect→drift-warn→batch-alter 哲学）、`safe_create_unique_constraint` / `safe_drop_unique_constraint`（与 ORM `UniqueConstraint` 声明对齐，而非 unique index——二者物理同构但 SQLAlchemy 经不同 inspector 反射，必须匹配 ORM 才能保 schema-parity）。
- ORM 模型同步：4 个模型 `org_id` 改 `nullable=False`；`ThreadMetaRow.__table_args__` 增 `UniqueConstraint("org_id","thread_id",name="uq_threads_meta_org_thread")`。`test_create_all_and_alembic_upgrade_produce_same_schema` 守护 parity。

语义与边界：

- **不可逆（生产语义）**：Enforce 后生产 DB 不容忍 NULL `org_id`；pr-split-guide §7 禁止把 Enforce 与 multi-org Feature（PR-025B）合成一次发布。本 PR 只做 schema 收紧，不动 Feature Flag、不动应用过滤逻辑（PR-024 已就位）。
- **零数据风险**：PR-023 backfill 已让存量全非空（空 `org_id=0`），PR-024 保证新写必 stamp → NOT NULL ALTER 零数据丢失；残留 NULL 会让 ALTER fail-loud（正确、surface 行为），doctor（PR-025C）后续补友好预检。
- **downgrade 安全**：re-nullable + drop unique；因 PR-024 持续 stamp，回滚不产生新 NULL。
- **`UNIQUE(org_id, thread_id)` 当前为声明性**：`thread_id` 已全局 PK，约束恒满足；确立 §1 #9 org 前缀唯一范式，为后续 org-scoped 业务键铺路。

测试：`test_resource_org_schema.py`（NOT NULL 矩阵 + 复合唯一 + 0006↔0005 round-trip）、`test_persistence_bootstrap.py`（HEAD=`0006`、parity）、`test_backfill_default_org.py`（schema 钉到 0005 验证 backfill job）、既有持久化/隔离套件零改动即绿。

### 16.16 PR-025A 不包含

显式排除（后续 PR 交付）：

- multi-org Feature Flag + 不对外开放验证 Org（PR-025B）：Feature Flag 机制本仓库尚不存在，PR-025B 从零引入；ci-cd §10.3 把 Flag 启用硬性门禁在本 PR（Enforce）上线且双 Org 矩阵全绿、空 `org_id=0` 之后；
- doctor 启用流程与迁移阶段探测（PR-025C）：production-runbook §5.2 doctor 必须读明确迁移阶段（不得只凭 Flag 猜测），并对「Feature ON + 残留 NULL `org_id`」「租户过滤关闭但存在多 Org」两态判 FAIL；
- 移除 `user_id` 隔离分支 + 清理 5 个临时兼容索引 + `uq_events_thread_seq`/`uq_feedback_thread_run_user` 的 org-scoping（Contract，PR-025D）：ci-cd §10.2 要求 Contract 至少晚一个稳定窗口；
- `runs.UNIQUE(org_id, idempotency_key)`（§7.2）：`idempotency_key` 列尚不存在，属 ReleaseRef 强制执行 track；
- `release_digest NOT NULL` / `legacy_unpinned` 门禁（§7.2，ReleaseRef track）；
- 迁移内数据守卫（COUNT NULL 断言）：留给 doctor（PR-025C）做友好检查；迁移本身 fail-loud 已正确；
- RLS（§11.1 spike）、Audit 接入（Track D）。

### 16.17 PR-025B：Multi-org Feature Flag + 验证 Org（已交付）

data-model.md §13.3 **Enforce** 阶段的「创建不对外开放的验证 Org」+「开启多组织 Feature Flag」的机制部分。从零引入本仓库此前不存在的 Feature Flag 基础设施（ci-cd §11 八字段元数据规范），并把验证 Org bootstrap 接到 Gateway lifespan。对应 ci-cd §10.3 CD 顺序中的「create non-public validation Org」一步。

落地：

- **Config（版本化配置，runbook §4.2）**：新增 `deerflow/config/tenancy_config.py`，`tenancy.multi_org.phase` 三态 `disabled`（默认）/`validation`/`active`；`validation` 与 `active` 强制要求 `validation_org`，`disabled` 禁止（pydantic `@model_validator`）。`config.example.yaml` 增 `tenancy:` 块，`config_version` 15→16（安全叠加：无 `tenancy:` 的存量 config 解析为 `disabled` 默认，不硬断）。
- **Flag registry（ci-cd §11）**：新增 `deerflow/tenancy/feature_flags.py`。`FeatureFlag` frozen dataclass 记录八字段（name/owner/default/environment/dependencies/enable_criteria/rollback_behavior/expires_at）+ description；`MULTI_ORG_FLAG` 实例列 Track B 四个前置（PR-021/023/024/025A）与三个启用准则（Enforce 上线、双 Org 矩阵全绿、空 `org_id=0`）；`get_feature_flags()` / `get_feature_flag(name)` 访问器；`current_multi_org_phase()` 从 `get_app_config()` 读实时 phase（配置未加载时回落 `disabled`）。
- **验证 Org bootstrap**：`deerflow/tenancy/bootstrap.py` 新增 `ensure_validation_org(sf, *, org_id, slug, name)`，镜像 `ensure_default_org`（probe-by-id、幂等插入、`status="active"`），经既有 `emit_tenant_event` 通道发 `validation_org_created` / `validation_org_exists`。**不**创建 Membership / RoleBinding——验证 Org 在 B 中是惰性的（仅 `organizations` 行，无主体绑定，收不到流量）。
- **Gateway lifespan**：`app/gateway/app.py` 新增 `_ensure_validation_org(app)`，在 `_ensure_default_org` 之后调用，仅当 `phase == "validation"` 触发；`disabled` / `active` 均不调用。非致命（try/except + log），同 `_ensure_default_org` 模式。
- **职责分离**：registry = 静态元数据（代码评审承载安全属性）；config = 实时状态（per-environment，无需重布元数据）。doctor（PR-025C）两者都读。

语义与边界：

- **请求路径解析器在 B 中不变**：`app/gateway/tenant.py` 仍把每个请求映射到 `config.default_org_id`（单 Org）。Flag + 验证 Org 存在、被审计、被单测覆盖，但**没有任何请求消费它们**——基于 Membership 的 org 解析留给 PR-025C+。这是 B 完全可回滚的根本原因：`phase=disabled` = 今天一模一样的行为。
- **不翻 active**：B 只交付机制；把 `phase` 翻到 `active` 是操作者的 CD 动作，门禁在 ci-cd §10.3（双 Org 隔离矩阵生产全绿 + 空 `org_id=0` + Enforce 上线）。B 不违反任何 CD 门禁。
- **验证 Org 不对外开放**：B 不为它建 Membership/RoleBinding；它无法接收流量（解析器仍单 Org）。它存在为 FK 合法目标 + 审计里程碑，供后续操作者显式绑定验证队列主体。
- **可回滚**：设 `phase=disabled` 即回滚（纯 config 变更，无代码回滚）；验证 Org 行可保留（无害，不对外开放）。Flag 关闭不影响历史数据可读性（`org_id NOT NULL` 与 Flag 无关）。
- **`expires_at=2026-10-31`**：ci-cd §11 要求临时 Flag 携清理日期；此为 Contract（PR-025D）+ 一个稳定窗口后的最早安全移除时点，doctor（PR-025C）会强制具体日期。

测试：`test_tenancy_config.py`（默认 disabled、phase↔org 耦合、extra=forbid、YAML 往返）、`test_feature_flags.py`（八字段元数据完整性、registry 形状、`current_multi_org_phase` 回落）、`test_validation_org_bootstrap.py`（create/幂等/惰性-无 Membership 与 RoleBinding、审计事件 create-vs-exists、lifespan hook 三态门禁）、`test_default_org_bootstrap.py`（单 Org 不变量负例）。既有持久化/隔离/doctor 套件零改动即绿；全套 backend suite 零新失败（52 失败全为 main 上预存的 Windows/sandbox/symlink 环境性失败）。

### 16.18 PR-025B 不包含

显式排除（后续 PR 交付）：

- doctor 迁移阶段探测 + 启用流程 runbook（PR-025C）：runbook §5.2「Doctor 必须读取明确迁移阶段，不得只根据 Feature Flag 猜测状态」；doctor 必须对「Flag ON 但残留 NULL `org_id`」「租户过滤关闭但存在多 Org」两态判 FAIL；并对接近 `expires_at` 的 Flag 判 WARN、过期判 FAIL；
- 请求路径解析器切 Membership-based org 解析（PR-025C+）：B 中解析器保持单 Org；真实多租户请求路径（Membership/OIDC-group 解析、负向测试、观测）属 C；
- 把 `phase` 翻到 `active`：操作者的 CD 动作，门禁在 ci-cd §10.3；B 不执行此步；
- 移除 `user_id` 隔离分支 + 清理临时兼容索引 + org-scope 现有全局唯一（Contract，PR-025D）：ci-cd §10.2 要求 Contract 至少晚一个稳定窗口；
- RLS（§11.1 spike）、Audit 接入（Track D）。

### 16.19 PR-025C：Doctor 租户迁移阶段探测（已交付）

runbook §5.2「Doctor 必须读取明确迁移阶段，不得只根据 Feature Flag 猜测状态」的兑现。把 production doctor 的 `tenant.migration_state` 占位（原 `DEFERRED_LIVE_CHECKS` 之一，恒 FAIL）换成真正的 live-DB 探测：读取 config phase（经 `current_multi_org_phase()` 单一读点）+ 交叉校验观测到的 DB 状态，按 runbook §5.2 状态表判 PASS/WARN/FAIL。

落地：

- **首个 live-DB check**：现有 doctor 设计上 config-only（10 `STATIC_CHECKS` + 1 special `check_secret_references` + `DEFERRED_LIVE_CHECKS` 占位，零 DB 访问）。phase 探测是首个需要 DB 连接的 check，是新架构形态。
- **`app/doctor/tenant_probe.py`**：`async def probe_tenant_migration_phase(config) -> DoctorCheckResult`。内部 `create_async_engine(config.database.app_sqlalchemy_url)` 建临时只读 engine（**不复用全局 engine、不跑 alembic/bootstrap**——区别于 `init_engine_from_config` 会迁移 DB），跑两个查询（4 张资源表 NULL `org_id` 行数总和，复用 `tenancy/backfill.py::_null_org_count` 模式；`organizations` 行数），`dispose()` 后按 runbook §5.2 表分类。**只读保证**：探测只发 `SELECT COUNT(*)`，临时 engine 用完即弃，不触碰全局 `_engine`/`_session_factory`，doctor 永不修改 DB。
- **判定表（runbook §5.2 逐行）**：disabled+多Org→FAIL（§5.2 第 5 行）；validation+残留NULL→FAIL；active+残留NULL→FAIL（§5.2 第 4 行「Feature ON 但仍有空 org_id」）；active+仅1Org→WARN；其余清洁态→PASS。
- **`check_feature_flag_expiry`（config-only，新 STATIC_CHECK）**：读 `MULTI_ORG_FLAG.expires_at`（"2026-10-31"），≤30 天 WARN（接近清理日期）、过期 FAIL。兑现 ci-cd §11「临时 Flag 有清理日期」的可观测。
- **`run_production_checks` 加 `extra_checks` 参数**：探测是异步的，在 `scripts/doctor.py::_run_production_doctor`（已改 async，`main` 用 `asyncio.run` 包裹）await 后作为预计算 `DoctorCheckResult` 传入。**`run_production_checks` 保持同步签名**——单元测试不需 async 化。
- **DB 连接失败容错**：探测内部 try/except，连接失败返回 FAIL（"无法连接 DB 验证迁移状态"），不抛异常中断 doctor。

语义与边界：

- **不改请求路径**：本 PR 不动 `app/gateway/tenant.py`，解析器仍单 Org。doctor 是纯只读观测层，请求路径行为零变化。
- **不翻 `active`**：doctor 只读不翻开关；`phase` 翻 `active` 仍是操作者 CD 动作（ci-cd §10.3）。doctor 在 CD §10.3「post-enable verification」步骤作为门禁：任一 FAIL 阻断生产准入。
- **`tenant.migration_state` 移出 `DEFERRED_LIVE_CHECKS`**：它是真实 check 不再是占位；`DEFERRED` 数 13→12，`STATIC_CHECKS` 数 10→11（加 expiry）。

测试：`test_doctor_tenant_probe.py`（纯函数 `_classify` 8 分支 + live-DB 4 态 + DB 连接失败容错 + 无 secret 泄漏）、`test_production_doctor.py` 更新（`test_all_runbook_placeholders_remain_fail_closed` 从 expected 移除 `tenant.migration_state`、`test_production_cli_json_uses_report_exit_code` mock 改 async）。全套 backend suite 零新失败（52 失败全为 main 上预存的 Windows/sandbox/symlink flake，`comm -23` 对比 main 为空集）。

### 16.20 PR-025C 不包含

显式排除（后续 PR 交付）：

- 请求路径解析器切 Membership-based org 解析（PR-025C+）：C 中解析器保持单 Org；真实多租户请求路径（新建 membership-read helper——仓库无此代码、改 `resolve_tenant_context`/`resolve_channel_tenant_context`、定义多 membership 选择策略、处理可信内部/auth-disabled 主体回退、负向测试、观测）属 C+；
- 把 `phase` 翻到 `active`：操作者的 CD 动作，门禁在 ci-cd §10.3；doctor 只读不翻；
- 移除 `user_id` 隔离分支 + 清理临时兼容索引 + org-scope 现有全局唯一（Contract，PR-025D）：ci-cd §10.2 要求 Contract 至少晚一个稳定窗口；
- RLS（§11.1 spike）、Audit 接入（Track D）。

### 16.21 PR-025C+：解析器切 Membership-based Org 解析（已交付）

让多租户在 HTTP 请求路径真正生效的关键一步。把 `resolve_tenant_context` 从单 Org bootstrap（所有人 → `default_org_id`）切换为**基于 OrgMembership 的 org 解析**，门禁在 `tenancy.multi_org.phase`。PR-025B 的 Flag 机制 + PR-025C 的 doctor 探测都为此铺路。

落地：

- **新建 membership-read helper（仓库此前无此代码，只有写侧 `ensure_admin_membership`）**：`deerflow/tenancy/membership.py::get_active_membership(sf, *, user_id) -> OrgMembershipRow | None`。**单 membership 严格语义**：0 个 active → `None`（调用方 fail-closed）；1 个 active → 该 row（`org_id` 绑 TenantContext）；>1 个 active → raise `MultiMembershipError`（携带 user_id + count，调用方 fail-closed）。查询命中 `idx_org_memberships_user_status (user_id, status)` 索引，按 `created_at ASC` 确定性排序；纯读不 commit。
- **`resolve_tenant_context` 改 `async` + phase 门禁**：顶部读 `current_multi_org_phase()`。`disabled` → 今天的快速单 Org 路径（无 await、无 DB，行为与今天完全一致——可回滚）；`validation`/`active` → 经延迟 import 取 `get_session_factory()`，调 `get_active_membership(sf, user_id=principal.user_id)`，org 来自返回的 membership。`principal.user_id` 已由 `resolve_principal` 正确计算（`get_trusted_internal_owner_user_id(request)` else `str(getattr(user, "id"))`——覆盖 session UUID id 与内部 owner header）。
- **middleware 一行 await 改动**：`TenantResolutionMiddleware.dispatch`（已是 async）调用处 `resolve_tenant_context(...)` → `await resolve_tenant_context(...)`。现有 `try/except Exception → 503` fail-closed wrapper **不变**——`MultiMembershipError`、`RuntimeError`（无 membership / sf=None）自动捕获成 503。

语义与边界：

- **仅 HTTP 路径**：channel dispatch 路径（`resolve_channel_tenant_context` + `channel_tenant_scope`，同步 `@contextmanager`）**保持单 Org 不动**——它只有 `owner_user_id` 无 Request，且 `channel_tenant_scope` 在 `manager.py:1074` 以同步 `with` 调用。channel membership 切换留给后续 PR。
- **TEN-008 不变量保持**：无 membership 不合成默认 org——`validation`/`active` phase 下无 active membership → fail-closed 503（不回退 `default_org_id`）。
- **backend=memory 处理**：memory 模式 `get_session_factory()` 返回 None。`disabled` phase 不触达 DB（memory 测试不受影响）；`validation`/`active` 遇 sf=None → fail-closed RuntimeError → 503（正确：memory 模式不该开多租户）。
- **可信内部/auth-disabled 主体**：它们的 `principal.user_id` 是真实 owner（内部）或合成 id（auth-disabled）。`disabled` phase 走单 Org 不受影响；翻到 validation/active 后若无 membership 行 → fail-closed（操作者须先建 membership，这是 CD 步骤）。
- **可回滚**：`phase=disabled` = 今天的同步单 Org 行为（无 DB 查询、无 await 成本）。Flag 回滚即行为回滚。
- **不翻 `active`**：本 PR 只交付机制；`phase` 翻 `active` 仍是操作者 CD 动作（ci-cd §10.3 门禁）。

测试：`test_membership_resolver.py`（`get_active_membership` 三态 0/1/>1 + 非 active status 排除 + resolver phase 门禁 disabled/validation/active + fail-closed 无 membership / 多 membership / sf=None）。既有 `test_gateway_tenant_resolver.py` 12 测试在默认 `disabled` phase 下保持绿（回归 backstop）。全套 backend suite 零新失败（52 失败全为 main 上预存 Windows/sandbox/symlink flake，`comm -23` 对比 main 为空集）。

### 16.22 PR-025C+ 不包含

显式排除（后续 PR 交付）：

- channel dispatch 路径切 Membership-based org 解析：`resolve_channel_tenant_context` + `channel_tenant_scope`（需把 `@contextmanager` 改 `@asynccontextmanager`，改 `manager.py` 调用点）；属后续 PR；
- 多 membership 选择策略（workspace 路由、OIDC group 映射、当前 org 切换 API）：本 PR 单 membership 严格（>1 → fail-closed），多 membership 选择留给 PR-036（OIDC Group Mapping）之后的专门 PR；
- 把 `phase` 翻到 `active`：操作者 CD 动作，门禁在 ci-cd §10.3；
- 移除 `user_id` 隔离分支 + 清理临时兼容索引 + org-scope 现有全局唯一（Contract，PR-025D）：ci-cd §10.2 要求 Contract 至少晚一个稳定窗口；
- RLS（§11.1 spike）、Audit 接入（Track D）。

### 16.23 PR-062：结构化日志与 Trace 基础层（已交付）

从零建立 DeerNexus 可观测性基础层（仓库此前无结构化日志、无 OTel、无 prometheus——`logging.basicConfig` 在 gateway 导入期裸跑，135 个文件用 `getLogger(__name__)`）。落地 `docs/ops/observability-and-slo.md` §2 / §3 / §5 的**机制 + HTTP 请求路径 + 一个深路径示例（Run span）**，一致于 PR-025B/C/C+ 的切片模式。规范很大（15 关联 ID、19 日志字段、14 命名事件、10+ span 层级、tail sampling）——一次性全做会失控，明确范围如下。

落地（命名刻意避开已占用的 `deerflow/tracing/`——那是 Langfuse/LangSmith LLM callback）：

- **新建 `deerflow/observability/` 包（7 模块）**：
  - `correlation.py`：`@dataclass(frozen=True) CorrelationContext`（§2 全集，除 `request_id` 外皆可选）+ `_current_correlation` ContextVar；`bind_correlation` / `reset_correlation` / `get_correlation` 镜像 `contracts/context.py:116-178` 的 tenant ContextVar 模式（task-local、`asyncio.create_task`/`to_thread` 继承、`try/finally` 配对）；`new_request_id()`（uuid4().hex）；`validate_inbound_request_id(raw)`（§2 "校验长度和字符，不能造成日志注入"：长度 1-128 + 仅 `[A-Za-z0-9._-]`，不合法返回 None → 视为未提供 → 生成新 id）。
  - `scrubbing.py`：§3.3 禁止字段清洗 choke-point（authorization / cookie / api_key / secret / token / dsn / password / prompt / response / claims / file_body / signed_url）。**token-aware 匹配**：单词条目（`token`/`secret`/…）按 key 拆 token 后做集合成员判断（`bearer_token`→[`bearer`,`token`]→命中 `token`；`tokens`→[`tokens`]→不命中），多词条目（`api_key`/`file_body`/`signed_url`）按子串。这样既抓 `httpx_authorization` / `sqlalchemy_password` 三方库前缀，又不误杀 `tokens`（计数）/`tokenization_ms`（耗时）这类良性复数。命中后值替换为 `"<redacted>"` 不丢键（让读者看到清洗介入，便于发现回归）。
  - `logging_setup.py`：替换 `app.py:40-44` 模块级 `logging.basicConfig`。`JsonFormatter`（§3.1 19 字段 JSON，canonical 顺序，timestamp=ISO8601 UTC millis、`deployment_version` 空则不写、`event_name`/`error_code`/`duration_ms`/`outcome` 从 `extra` 提顶、剩余 `extra` 经 `scrub_extra` 并入、exception/stack_info 序列化）；从 `get_correlation()` 读关联字段，从 `opentelemetry.trace.get_current_span().get_span_context()` 读 `trace_id`/`span_id`（invalid all-zeros 视为无 span，不写零 id 污染查询）。`TextFormatter` 保留今天 `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"` + `[request_id=… org_id=…]` 后缀（无绑定时无后缀 = 今天行为）。`configure_logging(config)` 幂等：按 `_deerflow_observability` 标记只移除自己装的 handler（不动三方），重复调用不堆叠。`apply_logging_level`（`app_config.py:73`）不动，仍是单一 level 调整点。
  - `tracing.py`：`init_tracing(config) -> shutdown_callable | None`——`otel.exporter_endpoint=None`（默认）返回 None（API 层 no-op tracer 已就位，调用点零成本）；否则延迟 import SDK + OTLP exporter，装配 `TracerProvider(resource=Resource.create({service.name/namespace/deployment.environment/version}))` + `BatchSpanProcessor(OTLPSpanExporter(endpoint))` + `ParentBased(TraceIdRatioBased(rate=sampler_ratio))`，返回 flush shutdown。**§5.4 tail sampler 延后**——TODO 标注：100% errors/Policy deny/Sandbox 违规保留规则需 Track C/E 的 deny/violation 代码路径。`get_tracer(name)` 薄封装；`set_span_attributes(span, **attrs)` §5.3 allow-list（org_id/run_id/thread_id/release_digest/policy_version/route/model/provider/tool_registry_name/decision/error_code + http.\* 语义约定 + event_name/duration_ms），非白名单键 DEBUG 日志后丢，None 值跳过，非 recording span 不抛。
  - `events.py`：`emit_event(event_name, *, level=INFO, message=None, **fields)`——单一 choke-point（镜像 `tenancy/audit_events.py::emit_tenant_event`，便于 PR-063/未来 outbox 替换 sink）。从 `get_correlation()` 注入关联 ID；`fields` 经 `scrub_extra`；经独立 logger `observability.events` 记录（便于独立路由/索引），`event_name` 提顶为 log 字段且 set 到当前 span 的 `event_name` 属性（log↔trace join）。**MUST NOT raise**（observability 非正确性门禁）。
  - `__init__.py`：导出上述公共 API。
  - `deerflow/config/observability_config.py`（非 `observability/` 内——config 模型与 `tenancy_config.py`/`production_config.py` 同层）：`ObservabilityConfig` + `OtelConfig`（pydantic `ConfigDict(extra="forbid")`）。默认 = 今天行为（`log_format="text"` + `otel.exporter_endpoint=None`），可回滚。
- **新建 `app/gateway/correlation_middleware.py`**：`CorrelationMiddleware(BaseHTTPMiddleware)`，注册为**最外层**（`create_app()` 中 `add_middleware` 最后调用 → 运行时最先执行）。`dispatch`：解析 inbound `X-Request-Id` → `validate_inbound_request_id` → 合法则用，否则 `new_request_id()`；打开 HTTP 根 span（§5.1 `HTTP <method> <route_template>`，`route_template` 从 `request.scope["route"].path_format` 取，无则 raw path；**`update_name` 在 `call_next` 返回后做**——最外层中间件运行时路由尚未 match，span 先以 raw path 开，`scope["route"]` populate 后 rename + set `http.route`）；从 span context 读 `trace_id`/`span_id` 构造 `CorrelationContext`，`bind_correlation` + `request.state.request_id = request_id`；`try: response = await call_next(request) finally: reset_correlation(token)`；span set `http.method/route/status_code/response.status_class`/`duration_ms`，error 时 OTel `with` 自动 record；emit `gateway.request.completed`（§3.4 首条——本 PR 唯一埋点事件），level 按 §3.2（5xx=ERROR、4xx=INFO 不当 ERROR、else INFO）。**Tracer 不缓存到模块级**——`ProxyTracer._tracer` property 缓存首次 non-None provider 的 SDK tracer 后不再刷新，模块级缓存会钉死在首次请求时的 provider（测试 provider swap / 未来热重载都会失效）；改 per-request `get_tracer(_TRACER_NAME)`（成本是一次 provider 上的 dict 查询）。**fail-open**：correlation/event 失败不阻断请求（observability 非正确性门禁，与 `TenantResolutionMiddleware` fail-closed 正交）。响应回 `X-Request-Id` 头让客户端关联。
- **`runtime/runs/worker.py::run_agent` 包 Run 根 span**（§5.1 `run <run_id>`）：tenant 防御性 rebind 之后、`try:` 之前，用显式 `__enter__`/`__exit__`（不重新缩进现有 230 行 try/except/finally 体）开 span，set §5.3 允许属性（`run_id`/`thread_id`/`org_id`（来自 `ctx.tenant.org_id`）/`model`）；`except Exception` 分支显式 `record_exception(exc)`（捕获的异常不会经 `__exit__` auto-record，因为 `__exit__(None,None,None)`）；`finally` 末尾 `__exit__(None,None,None)` 关 span。**仅根 span + §5.3 属性**——§5.2 深层级（graph node / model call / tool-mcp call / sandbox acquire/execute/release / audit enqueue）延后。
- **集成**：`AppConfig` 加 `observability` 字段（`tenancy` 后）；`packages/harness/pyproject.toml` 加 `opentelemetry-api`/`opentelemetry-sdk`/`opentelemetry-exporter-otlp>=1.28`（延迟 import 保 no-op 路径零 SDK 成本）；`app/gateway/app.py` 删模块级 `logging.basicConfig`，改 `configure_logging(ObservabilityConfig())`（try/except 读 config 失败回退 text），`lifespan` 内 `apply_logging_level` 后 `configure_logging(startup_config.observability)`（真 config 重配）+ `tracing_shutdown = init_tracing(startup_config.observability)`，shutdown 段 `if tracing_shutdown: tracing_shutdown()`；`create_app()` CORS 后 `app.add_middleware(CorrelationMiddleware)`（最外层）；`tenant.py:271` `request_id = getattr(request.state, "request_id", None) or _resolve_request_id(request)`（CorrelationMiddleware 已设置则用之，`_resolve_request_id` 保留为防御回退）；`config.example.yaml` `config_version: 16→17` + `observability:` 块（默认 text + 注释 otel 示例）。

语义与边界：

- **默认即今天行为（可回滚）**：`log_format="text"` + `otel.exporter_endpoint=None` = 今天的纯文本日志 + OTel no-op tracer。`log_format=json` + 设 endpoint 是纯 config 开关，无代码回滚。
- **零调用点改动**：135 个 `getLogger(__name__)` 调用点零改动——只换 root handler 的 formatter。
- **TEN-008 / fail-closed 不受影响**：CorrelationMiddleware fail-**open**（observability 非正确性门禁），`TenantResolutionMiddleware` 仍 fail-closed 503，两者正交。
- **secret 不入日志**：`scrub_extra` 在 formatter 和 `emit_event` 双重应用；§3.3 全字段测试覆盖（含三方库前缀 `httpx_authorization` / `sqlalchemy_password`）。
- **关联 ID 注入防御**：inbound `X-Request-Id` 严格校验（长度 1-128 + `[A-Za-z0-9._-]`），杜绝换行 / JSON 结构 / 控制字符日志注入。
- **OTel no-op 路径零成本**：endpoint 未配 → `trace.get_tracer` 返回 API 层 no-op tracer，`start_as_current_span` 是零成本 context manager。

测试（148 个，全绿）：`test_observability_correlation.py`（contextvar 生命周期 + asyncio `create_task` 继承 + sibling 不泄漏 + inbound request_id 校验含日志注入 / JSON 结构 / 控制字符 / 128 字符边界）、`test_observability_scrubbing.py`（§3.3 全字段覆盖 + token-aware 良性复数 `tokens`/`responses`/`tokenization_ms` 不误杀 + 三方库前缀命中 + 不改输入）、`test_observability_config.py`（pydantic schema 默认 + AppConfig additive wiring + sampler_ratio 范围 + `extra="forbid"`）、`test_observability_logging.py`（JsonFormatter §3.1 19 字段 + canonical 顺序 + ISO8601 UTC + correlation 注入 + OTel trace_id 注入 + 不写零 trace_id + scrubbing choke-point + lifted 字段提顶 + exception 序列化；TextFormatter 今天形状 + 关联后缀；`configure_logging` 幂等不堆叠）、`test_observability_tracing.py`（`init_tracing` no-op vs wired 返回 callable + `set_span_attributes` §5.3 allow-list + None 跳过 + 非白名单丢 + 非 recording span 不抛）、`test_observability_events.py`（`emit_event` 形状 + correlation 注入 + scrubbing + active span set attribute + 无 active span 不抛 + logger/otel 失败不抛的 never-raises 契约）、`test_correlation_middleware.py`（request_id 生成/honor/校验拒绝/trim + HTTP 根 span 开闭 + route template rename + http.\* 属性 + 4xx/5xx status_class + gateway.request.completed 事件 + §3.2 level 映射 + fail-open）。OTel 测试隔离用 `tests/conftest.py::otel_in_memory` fixture——直接赋 `opentelemetry.trace._TRACER_PROVIDER` 全局 + 重置 `_TRACER_PROVIDER_SET_ONCE._done`（OTel 公私 API 都被 `Once.do_once` 门禁至多一次/进程，`log=False` 只静默 warning 不启用 override；硬重置是唯一办法）。

全套 backend suite 零新失败（52 失败全为 main 上预存 Windows/sandbox/symlink flake，`comm -23` 对比 main 为空集；branch 多 148 通过：5104→5252）。

### 16.24 PR-062 不包含

显式排除（后续 PR 交付）：

- **14 个命名事件中其余 13 个的调用点**（§3.4）：本 PR 只埋 `gateway.request.completed`（CorrelationMiddleware）。`run.created` / `run.status.changed` / `run.owner.changed` / `run.reconcile.result` / `policy.evaluated` / `tool.call.completed` / `mcp.call.completed` / `sandbox.lease.changed` / `model.call.completed` / `release.resolved` / `audit.outbox.result` 等由各自功能 PR 埋点（`policy.evaluated` → Track C 权限注册表；`run.owner.changed` → ownership PR；`sandbox.lease.changed` → Track E；`audit.outbox.result` → Track D PR-041；`run.*` / `model.call.completed` / `tool/mcp.call.completed` → Runtime 各 instrument PR）。本 PR 只交付 `emit_event` helper + `gateway.request.completed` 一个示例。
- **§5.2 span 层级深度**：本 PR 仅 HTTP 根 span + Run 根 span。`authenticate → resolve tenant → authorize → resolve release → policy admission → persist run → run execution → {graph node, model call, policy tool evaluation, tool/mcp call, sandbox acquire/execute/release} → audit/usage enqueue` 各层 instrument 由后续 PR 随各层落地逐层补。
- **§5.4 tail-based 采样**（errors / Policy deny / Sandbox 违规 100% 保留）：需 deny / violation 代码路径存在（Track C / Track E）；本 PR 交付 `ParentBased(TraceIdRatioBased)` head sampler 作文档回退，`tracing.init_tracing` TODO 标注依赖。
- **§11 发布标记**（deployment_id / git_commit / image_digest / database_revision / feature_flags / agent_release_event_id / release_digest / operator / occurred_at）：需 CI / release pipeline 配合，延后。
- **Metrics / Dashboard / Alerts**：属 **PR-063**（独立 PR——规范未把 063 标为依赖 062，`progress.md §8` 两者都标 `可并行`）。本 PR 只在 `JsonFormatter` 注入 `deployment_version`，不动 metrics 注册表 / `/metrics` 端点 / dashboard JSON / 告警规则。
- **Trace 保留期 / 日志平台后端 / SLO 查询**：属运维平台落地（observability-and-slo §12 / §15 item 2/3/6），代码 PR 不交付。

### 16.25 PR-063：Metrics / Dashboard / Alerts（已交付）

在 PR-062 的观测基础层（结构化日志 + OTel）之上，交付 `observability-and-slo.md` §4 / §8 / §9 的 **Prometheus 指标 + Grafana dashboard + PrometheusRule 告警**。仓库此前**无** prometheus、**无** `/metrics` 端点、**无** deploy/ 目录——从零建。

落地（一致于 PR-062 切片模式；不交付空壳指标是核心护栏——见 §16.26）：

- **新建 `deerflow/observability/metrics.py` 指标注册表**：lazy singleton accessors（`@lru_cache` keyed on registry arg）+ §4.1 标签基数护栏（`ALLOWED_LABELS` allow-list，`_validate_labelnames` 在构造时 raise，非白名单标签直接拒绝——`org_id`/`user_id`/`run_id`/`request_id`/`trace_id`/`thread_id`/`raw_url`/`artifact_name` 永不进公共指标）+ fail-open wrapper（所有 bump 包 try/except，observability 永不阻断请求）+ 常量标签（`service`/`environment`/`deployment_version`，lifespan 通过 `_set_constant_labels` seed，镜像 OTel Resource attributes）+ test-only `reset_accessor_caches_for_tests`（per-test `CollectorRegistry` 隔离）。`prometheus-client>=0.21` 加到 harness pyproject。
- **新建 `app/gateway/routers/metrics.py` + `/metrics` 端点**：`generate_latest()` + canonical content-type，门禁 `ObservabilityConfig.metrics.enabled`。公网路径（业界惯例——§4.1 已禁止高基数 ID 标签，payload 无敏感数据；限制抓取用 ingress 不用 auth）。`/metrics` 加到 CorrelationMiddleware / TenantResolutionMiddleware / AuthMiddleware 的 `_PUBLIC_PATH_PREFIXES`，且 **CorrelationMiddleware 早返回**——抓取不经 HTTP span / 不 bump `http_requests_total`（避免 Prometheus 抓取污染请求图）。
- **`emit_event` §3.4 → §4 metric fan-out**（`observability/events.py`）：`_EVENT_METRIC_FANOUT` 派发表把命名事件映射到 counter bump（`gateway.request.completed` → `record_http_request`），未来 PR 埋的事件名加进表即自动驱动指标。**CorrelationMiddleware 不再直接调 `record_http_request`**——改经 `emit_event` 传 method/route_template/outcome/duration_ms/error_code fields，由 fan-out 驱动 counter（避免双计）。unmapped 事件名跳过（无自然 counter 的事件不强加）。
- **§4.2 Gateway 指标**（CorrelationMiddleware `dispatch` 的 `finally` emit_event 扇出）：`http_requests_total` / `http_request_duration_seconds` / `http_request_size_bytes` / `http_response_size_bytes`（Counter+Histogram，labels method/route_template/status_class/error_code）；`active_sse_connections`（Gauge，`services.py::sse_consumer` 进/出 inc/dec）；`sse_first_business_event_seconds`（Histogram，首 non-heartbeat 事件计时，重连跳过——§6.4 "重连单独统计"）；`rate_limit_total`（Counter label reason，`auth.py::_check_rate_limit` 429 处——OIDC login_total 延后因 OIDC 代码路径不存在）。
- **§4.3 Run core 指标**（`RunManager` + `worker.py`）：`runs_created_total`（`create_or_reject` 成功后）；`runs_status_total`（`set_status` 每次转换，label run_status）；`run_duration_seconds`（Histogram label terminal_status，worker.py Run span 闭合时计时）；`run_admission_duration_seconds`（worker.py body-entry → `set_status(running)`，§6.3 P95<3s）；`run_cancel_total`（`cancel` 真实 initiate，不含 idempotent 已 interrupted）；`run_reconcile_total`（label outcome=recovered/skipped_live/persist_failed/row_map_failed）+ `run_reconcile_backlog`（Gauge，`reconcile_orphaned_inflight_runs`）；`worker_active`（Gauge，每次 `set_status` 重算 pending+running 数——避免单独 scraper task）。
- **§4.4 Model/Tool/MCP 指标**（minus cost + policy）：`model_calls_total` + `model_call_duration_seconds`（`LLMErrorHandlingMiddleware.awrap_model_call`，labels model/provider/outcome——best-effort 从 `request.model` 的 `.model`/`.model_name`/`.deployment_name` attr 提取，provider 从 `langchain_<provider>.…` 模块名）；`model_tokens_total`（label model/direction=in|out，`TokenUsageMiddleware._apply` 读 `usage_metadata`——model 名此中间件无权访问，label 暂 `"unknown"`，dashboard 后续 join）；`tool_calls_total` + `tool_call_duration_seconds`（`SandboxMiddleware.awrap_tool_call`，label tool_name）；`mcp_calls_total` + `mcp_call_duration_seconds`（`mcp/tools.py`——body 提取到 `_call_with_persistent_session_inner` 让 timer 包整个调用）。**tool 名归一**：`normalize_tool_name(name, known)` §4.4 "未知 → other" 规则，`known=None` 时（今天无中央 registry）名字透传。
- **§4.5 Sandbox acquire/active 指标**：`sandbox_acquire_duration_seconds`（`SandboxMiddleware._acquire_sandbox[_async]` 计时）；`sandbox_active` / `sandbox_pending`（Gauge，acquire inc/release dec）。OOM/quarantine/timeout/cleanup-failure 延后——代码路径不存在。
- **§4.6 DB pool 指标**（partial）：`db_pool_in_use` / `db_pool_size`（Gauge，新增 `engine.py::get_pool_stats()` + `refresh_db_pool_metrics()` 解析 `_engine.pool.status()`，gateway lifespan + runtime-init 后刷新）；`db_query_duration_seconds` + `db_transaction_failure_total`（`engine.py::_install_db_metrics_listeners` 装 SQLAlchemy `before_cursor_execute`/`after_cursor_execute`/`handle_error` event listener）。Redis / Audit outbox / usage / backup / object-digest 延后——基础设施不存在。
- **`deploy/` 目录**（新建）：4 个 Grafana dashboard JSON（§8.1 平台总览 / §8.2 Runtime / §8.3 Control Plane / §8.4 Tenant Ops）+ `prometheus-rules.yaml`（§9 P1/P2 告警，每条带八字段 annotation owner/severity/summary/impact/dashboard/runbook/silence_rule/escalation，**只对已连线指标建告警**，延后项在 YAML 注释列出）+ `README.md`（部署说明：Grafana provisioning 挂载 + `kubectl apply`）。Tenant Ops dashboard 按 §8.4 / §4.1 **不暴露 org 标签**——按 Org 数据走 UsageRecord / Tenant Console（PR-060/061），dashboard 只交付平台级总量。

语义与边界：

- **不交付空壳指标**（§7.1 "不在缺少真实基线时伪设精确告警"）：只对存在代码路径的指标建 counter/histogram/gauge + dashboard 面板 + 告警；不存在的明确延后并在 YAML/docstring 标注「待 PR-X」。`model_cost_amount`（无价格表）/`policy_decisions_total`（无 Policy 引擎，Track C）/`oidc_login_total`（无 OIDC）/Profile-W HA（无 ownership/lease/heartbeat/queue）/Sandbox OOM-quarantine（无检测）/Redis（无客户端）/Audit outbox（阻塞 PR-041）/usage-backup-object-digest（无基础设施）全部延后，详见 §16.26。
- **§4.1 标签基数护栏**：`ALLOWED_LABELS` allow-list + 构造时 raise + 测试 pin；额外加 4 个低基数 internal-use 标签（`direction`/`terminal_status`/`reason`/`error_class`，各有 docstring 说明闭合词表）。
- **fail-open 契约不变**：所有 counter/histogram/gauge bump 包 try/except（observability 非正确性门禁，与 PR-062 一致）；`/metrics` 端点不经 auth。
- **可回滚**：`MetricsConfig.enabled=false` → `/metrics` 404，counter 仍 bump（bump 成本远低于 trace，§6 SLO 全靠它——禁用是操作者 opt-out 不是安全默认）。
- **不改 PR-062 fail-open / fail-closed 正交**：CorrelationMiddleware 仍 fail-open，TenantResolutionMiddleware 仍 fail-closed 503。

测试（35 个，全绿）：`test_observability_metrics.py`（§4.1 allow-list 含高基数 id 拒绝 + `_make_counter/histogram/gauge` 拒绝 + `/metrics` payload bytes+content-type + supplied registry + `registry_health` + fail-on-bad-label + zero-tokens noop + `normalize_tool_name` 4 分支 + 全部 27 wired metrics bump 后 payload 含期望名 + 常量标签 stamp + `emit_event` fan-out 驱动 `http_requests_total` + unmapped 事件不 bump）；`test_observability_metrics_endpoint.py`（`/metrics` 200 canonical content-type + body 含 python*/process* + 抓取不污染 http_requests_total + 4 dashboard JSON 解析且每面板有 expr + alerts YAML 解析 + 每条告警带 §9 八字段 annotation + severity ∈ p1/p2）。per-test `CollectorRegistry` 隔离 fixture（patch `_registry_or_default` + `reset_accessor_caches_for_tests`）避免污染 process-global REGISTRY。

全套 backend suite：52 失败在 main、54 失败在 branch——`comm -23` 显示 branch 多的 2 个是 `test_mcp_file_migration`（main 上 15 失败 / branch 上 16 失败的同一 Windows/symlink flake，特定子测试随机抖动）+ `test_metrics_path_skipped_by_correlation_middleware`（独立运行 + 邻近 middleware 套件 108 测试全绿——属顺序相关 flake）。branch 多 33 通过（5252→5285，本 PR 35 新测试 - 2 重叠）。

### 16.26 PR-063 不包含

显式排除（后续 PR 交付——不交付空壳指标的核心护栏）：

- **§4.3 Profile-W HA**：`run_terminal_convergence_seconds` / `run_ownership_acquire_total` / `run_ownership_conflict_total` / `run_lease_expired_total` / `run_heartbeat_failure_total` / `worker_claim_total` / `worker_dead_letter_total` / `run_dispatch_backlog` / `run_dispatch_oldest_age_seconds` / `run_resume_total`——需 ownership/lease/heartbeat/message-queue 代码路径（未来 HA + Worker-queue PR）。
- **§4.5 Sandbox hardening**：`sandbox_oom_total` / `sandbox_quarantine_total` / `sandbox_timeout_total` / `sandbox_cleanup_failure_total`——需 OOM 检测 / quarantine / 超时 / 清理失败追踪（Track E）。
- **§4.6 Redis / Audit outbox**：`redis_command_duration_seconds` / `redis_stream_lag_seconds` / `redis_memory_bytes`（无 Redis 客户端——Redis stream consumer PR）；`audit_outbox_pending` / `audit_outbox_oldest_age_seconds` / `audit_publish_failure_total` / `audit_archive_lag_seconds`（阻塞 PR-041 Audit outbox）；`usage_ingest_lag_seconds`（无 usage-ingest pipeline）；`object_digest_mismatch_total`（无 content-addressed store）；`backup_last_success_timestamp`（无 backup job）。
- **§4.4 `model_cost_amount`**（无价格表——价格表 PR）、**`policy_decisions_total`**（无 Policy 引擎——Track C）。
- **§4.2 `oidc_login_total`**（无 OIDC 代码路径——只有 local login；OIDC/OAuth PR）。本 PR 的 `rate_limit_total` 只覆盖现有 auth login lockout。
- **§8.4 Tenant Ops dashboard 按 Org 面板**：按 §8.4 / §4.1 不在共享 dashboard 暴露 org 标签——按 Org 数据走 UsageRecord / Tenant Console（PR-060 Org Console API + PR-061 Admin Console UI）。
- **§9 延后告警**：prod digest 错误/缺失（P1）、跨 Org 泄露证据（P1）、Audit Class A outbox 不可写（P1）、Audit 归档/摘要/dead letter、Sandbox 逃逸（P1）、Redis 不可达 Profile H（P1）、Console 默认查询 P95>500ms（P2）、Redis Stream lag（P2）、Audit outbox oldest>5m（P2）、Backup 超 RPO（P1）、Object digest mismatch（P1）、Certificate/Secret 到期——各自代码路径不存在，YAML 注释列出。

### 16.27 PR-064：Doctor 完整检查（已交付）

把 production doctor 从「config-only + 1 live probe（PR-025C 的 tenant_probe）+ 12 个通用 FAIL 占位」升级为**真正的发布门禁**：5 个今天可连线的新 live probe + 8 个仍 FAIL 但按 Track 阻塞精确分类的 deferred check（不再是通用 "PR-064" 文案）。pr-split-guide §11「把骨架接入真实配置与依赖，生产 FAIL 条件进入发布门禁」；runbook §5.1 列出 ~20 个必检项，本 PR 让其中能做的真正可执行，不能做的精确标注阻塞源。

落地（一致于 PR-062/063「不交付空壳」护栏——不伪造不能做的 probe）：

- **新建 `app/doctor/probes/` 包（5 probe 模块 + `__init__.py`）**，每个 probe `async def probe_xxx(config) -> DoctorCheckResult`，镜像 `tenant_probe.py` 模式（throwaway / 进程内资源，所有失败容错为 FAIL 不抛，无 secret 泄漏——result message 只带 host 标签不带完整 URL/密码）：
  - **`postgres_probe.py::probe_postgres_connectivity`**（`postgres.connectivity`）：throwaway `create_async_engine`（不复用全局 engine，镜像 tenant_probe 隔离契约）→ `SELECT 1` + `SELECT version()` 校验版本 ≥15（runbook §5.1 FAIL 阈值）+ pool 配置信息性报告。backend=sqlite/memory → WARN 跳过（postgres probe 仅对 backend=postgres 有意义）。DB 错误 → FAIL 含 host 不含密码。
  - **`metrics_probe.py::probe_metrics_presence`**（`metrics.presence`，新 check*id）：进程内调 `generate_metrics_payload()`（**不**做 HTTP `/metrics` 抓取——doctor 是 preflight 门禁常在 gateway 起之前跑），断言 29 个 wired §4 metric 名（`EXPECTED_METRIC_NAMES` tuple 显式列出，pin rename 回归）全在 payload。`observability.metrics.enabled=false` → WARN。缺 wired metric 但只有 `python*\_`/`process\_\_`（doctor 跑在 gateway pod 外）→ WARN（环境条件非 wiring 回归）。缺部分 wired metric（至少有 `http_requests_total`）→ FAIL（rename/调用点回归）。
  - **`deployment_evidence_probe.py::probe_deployment_evidence`**（`deployment.evidence_validation`）：纯 config 校验。Profile S → PASS（无额外证据要求）；Profile H 缺 `profile_h_evidence` → FAIL；Profile W 缺 `profile_w_evidence` / `profile_w_rollback_evidence` / `profile_w_soak_hours(>0)` 任一 → FAIL（runbook §5.1 Profile W 最复杂、未记录 dispatch/rollback 是已知事故源）。不做 HTTP 可达性（那是发布流程的事）。
  - **`gateway_security_probe.py::probe_gateway_security`**（`gateway.security_validation`）：读 `DEER_FLOW_GATEWAY_URL` 环境变量（未设 → WARN 跳过，doctor 可独立验证 DB+观测层）。配置了 URL 时用 httpx 探测：`tls_enabled=true` 但 URL 是 `http://` → FAIL（声明与运行时不符）；CORS 声明但响应无 `Access-Control-Allow-Origin` → WARN（preflight 可能仍工作）；CSRF 启用但无 csrf cookie → WARN。httpx 失败 → FAIL 含 host 不含 auth。
  - **`rate_limit_probe.py::probe_rate_limit_retry_after`**（`gateway.rate_limit_retry_after`）：同样依赖 `DEER_FLOW_GATEWAY_URL`（未设或 `rate_limit_enabled=false` → WARN 跳过）。触发 auth login lockout：对 `/api/v1/auth/login/local` 连发 `threshold + 2` 次坏密码（fake user 不可命中真实账号），期望 429 + `Retry-After` 头 → PASS；429 无 Retry-After → WARN；无 429 → FAIL（rate-limit 运行时未生效）。
- **`app/doctor/production.py` 改造**：`DEFERRED_LIVE_CHECKS` 从 12 项缩为 8 项（移除 5 个已实现为 probe 的 + `metrics.presence` 从来不在 deferred），每项第 5 字段（remediation）从通用 `'Implement and verify this live probe in PR-064...'` 改为**精确 Track 阻塞文案**（`'Blocked on Track X (PR-XXX): <具体原因>'`，让操作者知道等什么、谁能解锁）。新增 `LIVE_PROBE_REGISTRY`（lazy-imported，5 probe 各带 check_id/component/config_source）+ `_live_probe_registry()` 访问器（lazy 避免 config-only 测试拖入 httpx/sqlalchemy）。`run_production_checks` 签名不变（仍同步，extra_checks 接收预 await 的 probe 结果），deferred 渲染改用每条自带 remediation（不再硬编码通用文案）。
- **`scripts/doctor.py::_run_production_doctor` 改造**：在 await `probe_tenant_migration_phase` 之后，**并发 await 5 个新 probe**（`asyncio.gather(..., return_exceptions=True)` per-probe try/except），全部作为 `extra_checks` 传入。顺序：STATIC_CHECKS → secret_references → tenant_probe（既有）→ 5 新 probe → DEFERRED_LIVE_CHECKS。probe 抛非预期异常（违反 containment 契约）→ 渲染为 FAIL（含异常类型不含 str(exc) 防 secret 泄漏）+ 提示「file a bug」。
- **`config.example.yaml` `config_version` 18→19**（纯文档变更——`DEER_FLOW_GATEWAY_URL` 是环境变量不是 config 字段，按 release-pipeline 设置不按 deployment）。

语义与边界：

- **不交付空壳**（§7.1 精神）：5 个 probe 都有真实代码路径；8 个 deferred 保持 FAIL 但精确标注 Track 阻塞。这是本 PR 的核心护栏——不伪造不能做的 probe 让操作者误以为已验证。
- **probe 失败容错**：每个 probe 的所有外部交互（DB/httpx/metrics）包 try/except，失败 → FAIL result（不抛），doctor 永不崩溃。镜像 tenant_probe 契约。
- **无 secret 泄漏**：每个 probe 的 result message 不含 URL 密码 / 完整 DSN——`_host_of` 只返回 host 标签。测试 pin（`test_doctor_probes.py` 每个 probe 的 `test_no_secret_leak` + 既有 `test_report_json_never_contains_configured_secret_values`）。
- **不改 `run_production_checks` 同步签名**：probe 是 async，在 CLI 层 `asyncio.gather` await 后作为 `extra_checks` 传入（既有模式）。
- **不依赖 gateway 运行**：postgres/metrics 进程内做；gateway security/rate-limit 仅在配置了 `DEER_FLOW_GATEWAY_URL` 时跑，否则 WARN 跳过。doctor 可独立验证 DB + 观测层。
- **测试可离线跑**：httpx probe 单元测试 monkeypatch `_httpx_get`/`_httpx_post`（不打真网）；postgres probe 用隔离 SQLite（backend=sqlite 走 WARN skip 路径）+ 不可达 postgres URL（走 FAIL 容错路径）；metrics probe monkeypatch `generate_metrics_payload`。

测试（64 个 doctor 相关，全绿）：`test_doctor_probes.py`（33 新——postgres 版本解析 6 case + backend skip + 不可达 FAIL + 无 secret 泄漏；metrics 5 case 含 disabled/outside-pod-WARN/all-present-PASS/partial-missing-FAIL + EXPECTED_METRIC_NAMES 非空唯一；deployment_evidence Profile S/H/W 全分支 + partial；gateway_security 5 case 含 no-URL-skip + tls-mismatch-FAIL + unreachable-FAIL + reachable-PASS + no-secret-leak；rate_limit 6 case 含 skip + 429+Retry-After-PASS + 429-no-Retry-WARN + no-429-FAIL + httpx-fail-FAIL）、`test_production_doctor.py` 更新（`test_static_declarations_pass_but_deferred_live_probes_block` 的 `fail_count == len(DEFERRED_LIVE_CHECKS)` 仍成立现在 8；新增 `test_deferred_live_checks_have_track_specific_remediation` pin 无通用 PR-064 占位 + 每条有 'Blocked on'；`test_all_runbook_placeholders_remain_fail_closed` 集合从 4 改 8 移除 `gateway.rate_limit_retry_after` 加 5 新 deferred）。

全套 backend suite：零新失败（52 失败在 main、52 失败在 branch——`comm -23` 对比 main 为空集；52 全为预存 Windows/sandbox/symlink flake；branch 多 34 通过：5287→5321）。

### 16.28 PR-064 不包含

显式排除（后续 PR/Track 交付——保持 FAIL 但精确标注阻塞源）：

- **`redis.connectivity`** → Track G（PR-071/073 Redis stream consumer）：无 redis 客户端。
- **`oidc.jwks_validation`** → Track C（PR-036 OIDC）：只有 local login。
- **`sandbox.provisioner_create`** → Track E（sandbox hardening）：LocalSandboxProvider 可用但生产 provisioner（docker/k8s）create/destroy 路径才是本 probe 要 exercise 的；local-mode smoke 会给误导性 PASS。
- **`backup.freshness`** → PR-065（Backup/Restore Automation）：无 backup job。
- **`secret_store.access`** → Secret Store provider PR：只有 env_dev_only + reference 解析。
- **`object_storage.security`** → object-storage config 字段 PR：ProductionConfig 无 `object_storage` 字段。
- **`agent.release_ref_enforcement`** → Track E（PR-054 Release Resolve）：ReleaseResolver 是 Protocol 无具体实现。
- **`audit.outbox`** → Track D（PR-041 Audit outbox）：`emit_tenant_event` 仍是 logger.info sink。

每条 deferred 的 remediation 在 `production.py::DEFERRED_LIVE_CHECKS` 明文标注 Track / PR，doctor 报告直接展示给操作者。各 Track 落地后，把对应 deferred 行改为真实 probe（镜像本 PR 5 probe 模式）并从 `DEFERRED_LIVE_CHECKS` 移除。

### 16.29 PR-060：Org Console API（已交付）

PR-063 §16.25/§16.26 明确：按 Org 数据走 `UsageRecord / Tenant Console (PR-060/061)`，**不**进共享 Grafana dashboard（§8.4/§4.1 不在共享 dashboard 暴露 org 标签）。本 PR 交付 Tenant Console 的后端只读 API 半边（PR-061 交付 UI）。

**3 endpoints**（`backend/app/gateway/routers/admin.py`，前缀 `/api/v1/admin`，全 org-scoped）：

- `GET /stats` → `OrgStatsResponse`：`total_runs`、`runs_by_status`（status→count）、`failure_rate`（`(error+timeout+interrupted)/total`）、`recent_runs_24h`、`recent_failures_24h`（独立于窗口的「现在」信号）、`window_start/window_end`。默认窗口 7 天。
- `GET /runs` → `OrgRunListResponse`：keyset 分页 on `(created_at DESC, run_id DESC)`，`{data, has_more, next_cursor}` 信封。status/model/time-window 过滤。`OrgRunSummary.error` 截断 200 字符 + §3.3 禁词 substring 清洗（命中 → `<redacted>`，不截断输出避免首字节泄漏）。
- `GET /usage` → `OrgTokenUsageResponse`：org 级 token 聚合，shape 镜像 `ThreadTokenUsageResponse`（`by_model`/`by_caller` 复用既有子模型），`org_id` 替换 `thread_id` + 加 `window_start/window_end`。`include_active` 控制是否含 running。

**门控——临时 `system_role`**：复用 `deps.require_admin_user`（既有 helper，mcp/channels/channel_connections 已用），**不**触碰 `authz.py` stub（Track C PR-030/031 整体替换 authz 时一并迁移）。原因：`authz._authenticate` 给所有认证用户 `_ALL_PERMISSIONS`，`require_permission` 不是真实门控；`require_admin_user` 读 `request.state.user.system_role == "admin"` 是今天唯一的真实角色判断（ADR-0003 §15 line 436「Router 无手写角色判断」——门控集中在 authz 层，不在 router 内联）。Track C 落地后 admin.py 把 `require_admin_user(request, detail=...)` 换成 `@require_permission("admin", "console:read")`，零 router 业务逻辑改动。

**org_id 解析**：`_require_org_id(request)` 从 bound `TenantContext`（`TenantResolutionMiddleware` 绑定）取 `ctx.org_id`，未绑定 → 400（Org Console 无 tenant 上下文无意义）。Admin 在 Org A 不能看 Org B——per-Org 隔离。

**3 新 RunRepository 方法**（`persistence/run/sql.py` + `runtime/runs/store/base.py` ABC + `runtime/runs/store/memory.py` 三后端对称）：

- `aggregate_tokens_by_org(org_id, *, since=None, until=None, include_active=False)`：镜像 `aggregate_tokens_by_thread`，filter 从 `thread_id` 换 `org_id` + 时间窗，走 `ix_runs_org_status_created` 索引。同 return shape。
- `aggregate_stats_by_org(org_id, *, since=None, until=None)`：3 查询（status GROUP BY + COUNT 24h + COUNT 24h failures），全走 org 索引。
- `list_runs_by_org(org_id, *, status, model, since, until, limit, cursor)`：keyset 分页 `(created_at DESC, run_id DESC)`，`limit+1` has_more，cursor 是 `(created_at, run_id)` tuple。返回 `(rows, has_more)`，rows 经 `_row_to_dict`。

**cursor 编码**（`pagination.py`）：`encode_cursor(created_at, run_id)` → base64 url-safe `<iso>|<run_id>`；`decode_cursor` 反向 + rsplit("|", 1) 防 isoform 含 `|`。malformed cursor → 400。

**注册**：`app.py` import + `include_router`（auth.router 旁），openapi_tags 加 `admin`。

**测试**：`test_admin_console_api.py` 26 测试——mock 单测（200 shape / 401 / 403 非 admin / 400 malformed cursor / 503 store=None / since+until 转发 / cursor round-trip / error scrubbing secret-substring / error 截断 200 / 3 endpoint × 门控矩阵）+ memory store 3 方法 + cursor codec + **DB-backed 生产规模测试**（pr-split-guide §11 要求）：seed 1000 runs（6 status × 3 model × 4 user × 14d 分布），断言 stats total==1000 + failure_rate 正确、tokens 求和正确、keyset 翻页 20 页遍历 1000 行无重复、status filter 收窄、跨 org 隔离。

### 16.30 PR-060 不包含

**不交付空壳**（§7.1）——以下代码路径今天不存在，本 PR 不伪造：

- **UsageRecord 持久化**：契约（`contracts/events.py:124-198`）是 Pydantic DTO + Protocol，无 ORM 实现。契约要求 `release_digest`（非空），耦合未交付的 Track E PR-054 Release Resolver。本 PR 用 `RunRow` token 列 + `token_usage_by_model` JSON 作数据源——usage 端点今天返回 token 聚合，**不返回 cost 字段**（无价格表，§7.1 不伪造）。等 PR-054 + UsageRecorder 实现 PR 落地后，usage 端点可切到 metering-grade 数据源（含 attempt/cached_tokens/cost）。
- **真实 RBAC**：Track C（PR-030 permission registry + PR-031 Authorize Service + PR-033 router RBAC）全未交付，本 PR 用 `require_admin_user` 临时 `system_role` 门控。ADR-0003 §4.1 `org:admin` 携带 `admin:console:read`，等 Track C 落地后 router 换 `@require_permission("admin", "console:read")`。
- **跨 Org super-admin 视图**：admin 只能看自己 active Org 的数据，无「看所有 Org」视图（需 super-admin 角色 + 多 org 查询，今天无此角色）。
- **AuditSink-backed audit query**：`AuditSink` Protocol 存在但 `emit_tenant_event` 仍是 logger.info sink（PR-041 未交付），Failure/Audit 入口今天基于 `RunRow.status IN ('error','timeout','interrupted')` 而非独立 audit log。等 PR-041 落地后可加 `/audit` 端点查 AuditEvent 流。
- **`error_code` 结构化失败分类**：RunRow 只有 `error: str|None`（free text）+ `status`，无 `error_code` 列；`recent_failures_24h` 靠 status 计数，不能按 error_code 分组。`run_events.content/event_metadata` 有更丰富失败细节但 free text，需独立结构化 PR。
- **结构化 cost / price table**：无价格表 → `/usage` 不返回 cost；Cost attribution 需独立 PR（依赖 provider price config + release_digest → UsageRecord cost_amount）。

### 16.31 PR-061：Admin Console UI（已交付）

消费 PR-060 的 3 个 endpoints，交付 3 个 Console 页面。pr-split-guide §11 明确「只做 Runs、Usage、Failure/Audit 入口；不扩审批、市场、KB」。

**路由结构**：新建 `frontend/src/app/admin/` 独立 segment（不混 workspace sidebar——admin 上下文与 chat 语义错配）。

- `layout.tsx`：`export const dynamic = "force-dynamic"` + SSR admin 门控（镜像 `workspace/layout.tsx` 的 `getServerSideUser` tagged-union switch），`authenticated` 分支检查 `result.user.system_role !== "admin"` → redirect `/workspace`（零客户端闪烁）；挂 `AuthProvider` + `QueryClientProvider` + `AdminShell` + `<Toaster />`（admin layout 自带 toaster 不复用 workspace）。
- `page.tsx`：`redirect("/admin/runs")`（默认入口）。
- `runs/page.tsx`、`usage/page.tsx`、`audit/page.tsx`：3 个 `"use client"` 页面。

**`AdminShell`**（`src/components/admin/admin-shell.tsx`）：顶部 sticky nav bar（Admin Console 标题 + Runs/Usage/Failure-Audit 三 link），`usePathname()` 高亮 active，`max-w-(--container-width-lg)` 内容区。复用 design tokens，不复用 workspace sidebar。

**Admin API client**（`src/core/admin/{types,api,hooks,index}.ts`，镜像 `core/mcp/api.ts` + `core/memory/api.ts` 模式）：

- `AdminRequestError`（`get isAdminRequired() → status === 403`，镜像 `MCPConfigRequestError`）+ FastAPI `{"detail": ...}` envelope 解析。
- 3 fetch 函数 `fetchAdminStats/fetchAdminRuns/fetchAdminUsage`，全用 `import { fetch } from "@/core/api/fetcher"`（CSRF + credentials + 401→login 免费）；URL prefix `${getBackendBaseURL()}/api/v1/admin/*`（`getBackendBaseURL` 返回 "" → relative → 走 `next.config.js:71` catch-all rewrite → gateway）。
- TanStack Query hooks：`useAdminStats`（`useQuery`）、`useAdminRuns`（`useInfiniteQuery`，`getNextPageParam: lastPage => lastPage.next_cursor`，消费 PR-060 keyset）、`useAdminUsage`（`useQuery`）。QueryKey `["admin","stats",params]` 等。

**新依赖**：`recharts@3.9.2`（React 19 兼容，零 peer dep 冲突）。Usage 页 by-model BarChart，颜色映射 `var(--chart-1..5)` token（已在 `globals.css` 定义）；超 5 个 model 取 top 5 + "other" 桶。

**Table primitive**（`src/components/ui/table.tsx`）：手写 shadcn new-york table（~80 行，标准 `<table>` + Tailwind + `data-slot` 属性），避免 `pnpm dlx shadcn add` 在 CI 跑网络。

**Runs 页**（`src/app/admin/runs/page.tsx`）：

- `RunsFilterBar`（status `Select` + 24h/7d/30d/All `Tabs`，`useState` 不引表单库，镜像 settings page）。
- `RunsTable`（`useAdminRuns` infinite，keyset 翻页 `next_cursor`）—— 7 列（run_id 截断+Tooltip / status Badge 按 variant 映射 / model / total_tokens `formatTokenCount` / user / created_at `formatTimeAgo` / error 截断 200 字符）+ 底部 `Load more` 按钮（`fetchNextPage`，disabled 当 `isFetchingNextPage`）。
- Loading → `<Skeleton>` 占位行；Error → `<Empty>` + error message；空列表 → `<Empty>`（icon + "No runs in this window"）。

**Usage 页**（`src/app/admin/usage/page.tsx`）：24h/7d/30d/All Tabs + `UsageCharts` 组件（4 KPI Card：Total tokens / Total runs / Avg per run / Output:Input ratio + by-model BarChart + by-caller breakdown 3 Progress 条 lead_agent/subagent/middleware）。

**Failure/Audit 页**（`src/app/admin/audit/page.tsx`）：3 KPI Card（Failures 24h / Failure rate / Total runs 24h，`useAdminStats`）+ failure-status `Select`（pinned error/timeout/interrupted，PR-060 单 status 过滤）+ `RunsFilterBar`（hideStatus）+ `RunsTable` 预过滤。**诚实文案**：「Structured audit events require PR-041」——不伪造独立 audit log。

**导航入口**（`src/components/workspace/workspace-nav-menu.tsx`）：footer DropdownMenu Settings 项后加 `{user?.system_role === "admin" && (<><DropdownMenuSeparator/><DropdownMenuItem asChild><Link href="/admin/runs"><ShieldCheckIcon/> Admin Console</Link></DropdownMenuItem></>)}`。`useAuth()` 取 `user`，零新 store（复用既有 `AuthProvider`）。

**复用既有**（不重复造轮子）：`AuthProvider`/`getServerSideUser`/`QueryClientProvider`/`fetcher`/`formatTokenCount`/`formatTimeAgo`/`Badge`/`Card`/`Tabs`/`Select`/`Empty`/`Skeleton`/`Toaster`/design tokens（`--chart-1..5`）。

**门禁**：typecheck + lint + 337 tests + build 全绿。零新失败（input-box.tsx 1 个 pre-existing exhaustive-deps warning，非本 PR 代码）。

### 16.32 PR-061 不包含

**不交付空壳**（§7.1）——以下今天不存在或后端未返回，本 PR 不伪造：

- **cost / 价格表渲染**：后端 `/usage` 不返回 cost（PR-060 §16.30），UI 不渲染 cost 列/卡片。等价格表 + UsageRecorder cost_amount 落地后加。
- **独立 audit log 查询**：Failure/Audit 页基于 `RunRow.status IN (error,timeout,interrupted)`（PR-060 已支持 status 过滤），**不**伪造 AuditSink-backed AuditEvent 流。等 PR-041（Audit outbox）落地后可加真实 `/audit` 端点 + UI。
- **per-user / per-thread 聚合**：后端 `/stats`/`/usage` 是 org 级，无 per-user 或 per-thread 维度（RunRow 有 user_id 列但 PR-060 未加此聚合）。UI 不提供按 user/thread 拆分视图。
- **error_code 结构化分类**：RunRow 只有 free text `error`，UI 列只能截断展示原文 + Tooltip，不能按 error_code 分组统计。等 `error_code` 列 PR 落地后加分类筛选。
- **跨 Org super-admin 视图**：admin 只看 active Org（后端 `_require_org_id` 从 TenantContext 解析），UI 无 Org 切换器 / 全 Org 汇总。等 super-admin 角色 + 多 org 查询 PR 落地后加。
- **multi-status OR 过滤**：PR-060 `/runs` 单次只接受 1 个 status，audit 页用 failure-status Select（默认 error）让用户切换而非一次看全部 failure status。等后端支持 status 列表 OR 过滤后改 multi-select。
- **command palette admin 入口**：本 PR 只加 sidebar nav menu 入口；command palette（`Ctrl+K`）admin 命令是后续 polish。
- **numbered pagination**：PR-060 是 keyset（无总页数），UI 用 Load more 按钮消费 `next_cursor`，不强行套 numbered（会语义错）。

### 16.33 PR-030：权限注册表与内置角色（进行中）

Track C 入口。把 ADR-0003 §3-§5 的 22 条权限字符串固化为 frozen registry，seed 3 个内置 Org 角色，建立 system 权限隔离校验，配套正反向矩阵测试。

- **新建 `deerflow.contracts.rbac`**（与 `ErrorCode`/`PrincipalRef` 同层，自动通过 harness boundary）：
  - `Permission(StrEnum)` 22 成员，5 domain（`runtime`/`admin`/`studio`/`connector`/`system`），值逐字对齐 ADR §3 §48-75。**注意 ADR §3 本身的命名不一致**：格式声明是三段式 `<domain>:<resource>:<action>`，但 `connector:read`/`connector:manage` 是 ADR §3 permission list 自己用的两段式特例——registry 照 ADR 字面收录，测试断言放宽到 `count(":") in (1, 2)`，registry 是权威源而非 regex。
  - `BUILTIN_ROLE_PERMISSIONS: dict[str, frozenset[Permission]]`：`org:admin`(19)/`org:developer`(10)/`org:viewer`(4)，逐条对齐 ADR §4.1-4.3 矩阵。frozenset 防下游 mutation drift。
  - `SYSTEM_PERMISSIONS` frozenset（从 prefix 派生）+ `SYSTEM_PERMISSION_PREFIX = "system:"`。
  - `validate_role_permissions(permissions, *, is_system)` 写侧 guard：未知权限字符串 raise `PermissionValidationError(ErrorCode.VALIDATION_ERROR)`；`is_system=False` 时拒绝 `system:*` 前缀（ADR §3 §82）。异常类 `(ValueError)` 携带 `.code` + `.permission`，镜像 `TenantContextError` 模式——`ContractError` 是 pydantic BaseModel（数据信封），不是异常，不能直接 raise。该函数供未来自定义角色 write API 使用，PR-030 本身不调用（builtin 模板是权威源，不受 re-validation）。
  - `BUILTIN_ROLE_TEMPLATE_VERSION: int = 1`：seed 模板版本号，bump 触发审计（ADR §5/§13）。

- **新 alembic revision `0007_builtin_roles`**（链自 0006）：`safe_add_column` 加 `roles.template_version BigInteger nullable`（系统模板用，自定义角色 NULL）+ 幂等 seed 3 角色。seed 用 `sa.table("roles", ...)` + `op.get_bind()` 执行 SQL，probe `(name, is_system=true)`——存在则 UPDATE permissions+template_version，不存在则 INSERT（手动传 `created_at`/`updated_at`，因 `sa.table()` 不带 ORM default）。从 `deerflow.contracts.rbac.BUILTIN_ROLE_PERMISSIONS` import 单一权威源。downgrade 精确删 3 行（`WHERE is_system AND name IN (...)`，不碰未来租户角色）+ `safe_drop_column`。

- **`RoleRow` ORM 同步加列**（`iam/model.py`）：`template_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)`，注释说明语义。**parity guard 强制**——`test_create_all_and_alembic_upgrade_produce_same_schema` 逐列反射比较，ORM 与 migration 必须同步。

- **`tenancy/bootstrap.py` 接管 seed**（PR-022 预留交接点，原 docstring 明示「PR-030 delivers...」）：新 `ensure_builtin_roles(sf)` 循环 `BUILTIN_ROLE_NAMES`，每角色单 session probe-insert（参照原 `ensure_system_admin_role` 模式，SQLite FK-at-commit hygiene）；`ensure_system_admin_role` 改 thin wrapper——调 `ensure_builtin_roles` 后返回 `org:admin` 行。**两调用点不变**（`app.gateway.app._ensure_default_org` lifespan + `app.gateway.routers.auth._establish_admin_tenant_relationships`），保持签名/返回类型向后兼容。

- **三路径一致性**（关键设计约束，非选择）：DB bootstrap `empty` 分支（`create_all` + `stamp head`）**不跑 migration**，role 完全由 lifespan `ensure_builtin_roles` 建；`legacy`/`versioned` 分支跑 migration。靠 lifespan helper + migration 共享 `BUILTIN_ROLE_PERMISSIONS` 常量收敛，避免 fresh DB 与 upgrade DB 角色内容不一致。

- **测试**：44 新契约测（`test_contracts_rbac.py`：registry set-equality + len pin + 3 角色矩阵 + system 隔离 + validate 正反向 + testing-strategy §9.1 的 9×3 网格，ServiceAccount 列「按 scope」延后 PR-034）+ seed round-trip（`test_iam_schema`：downgrade 0006→0 行，upgrade head→3 行）+ 同步更新（`test_default_org_bootstrap` role_count 1→3 + permissions 断言从 `[]` 改为 registry 期望；`test_persistence_bootstrap*` HEAD 常量 0006→0007）。

### 16.34 PR-030 不包含

**严格不越界**（避免越界 Track C 后续 PR）：

- **运行时授权计算**：`effective_permissions` 交集公式（ADR §6 `active_membership ∩ role.permissions ∩ …`）→ **PR-031 Authorize Service**。PR-030 只交付公式右边的 `role.permissions` 输入数据。
- **router 切流**：所有 `@require_permission` 装饰器保持现状 → **PR-032/033**。
- **`authz.py` 旧 stub 删除**：`_ALL_PERMISSIONS` 全放行路径、旧 `Permissions` 类（两段式 `threads:read` 等）保持原样，**等 PR-031/033 整体替换**（runtime-contracts §16.30 已记录此约定）。PR-030 不碰 `backend/app/gateway/authz.py`。
- **ServiceAccount / API Key**：表已建（PR-020B），但生命周期/scope 交集/明文返回 → **PR-034/035**。
- **`system:admin` 角色 seed**：ADR §4.4 定义了它，但 pr-split-guide §8 PR-030 清单只提 Admin/Developer/Viewer。`system:admin` 独立于 RoleBinding（需专用 API/Repository + MFA + AuditEvent），seed 它会暗示错误的 grant 路径。`system:*` 三条权限进 registry 但不绑任何角色。
- **canonical JSON fixture**：registry 是纯枚举/常量，不是 DTO，无 round-trip 需求（与 `ErrorCode` 一致，它也没有 fixture）。
- **403/404 错误码映射**：→ PR-031（ADR §8 invited/removed→404、suspended→403 permission_denied 等）。
- **缓存 key/TTL**：→ PR-031（ADR §8 system-admin 独立 namespace）。
- **OIDC group mapping / 撤权 SSE**：→ PR-036/037。

### 16.35 PR-031：统一 Authorize Service（进行中）

Track C 第二刀。交付 ADR-0003 §6 的 `authorize(tenant_context, permission, resource_ref) -> None | raises` 运行时授权函数，基于 DB 计算 `effective_permissions` 交集，映射 §12 的 403/404 错误码，提供 §11 的 in-memory TTL≤60s 缓存。PR-030 交付了权限 registry + 角色模板（公式右边的 `union(role.permissions)` 输入数据），本 PR 让 RBAC 真正可计算。

- **新建 `app/gateway/authorize.py`**（与 `tenant.py`/`deps.py` 同层，DB-backed 必须在 app 层——contracts 层无 DB 依赖）：
  - 纯函数 `compute_effective_permissions(*, membership_status, role_permissions, org_status, system_role, api_key_scopes) -> frozenset[str]`：无 IO，完全可测。实现 ADR §6 交集公式的每一项——`active_membership`（status=="active"）、`active_principal`（system_role 门控，UserRow 无 status 列当前粗粒度）、`non_expired_role_bindings`（调用方已过滤 expires_at）、`union(role.permissions)`（调用方已 JOIN 聚合）、`api_key.scopes_if_present`（None=全集，非 None=收窄交集，ADR §6「scope 只能收窄」）、`organization_state`（suspended/deleting 由 `authorize()` 抛错，不进 compute）、`policy_decision`（恒全集，Track E 延后）。**system_role==admin 特例**：直接返回 `SYSTEM_PERMISSIONS`（ADR §4.4 独立于 RoleBinding），API Key scope 仍应用。
  - `AuthorizeService` 类：持 `session_factory` + `PermissionCache`。`compute_permissions_for_user(user, *, org_id, api_key_scopes=None) -> frozenset[str]`（DB-backed，查 membership/role_bindings/roles/org_status + 缓存查找 + 调纯函数）；`authorize(tenant_context, permission, resource_ref=None, *, api_key_scopes=None) -> None`（ADR §6 统一签名，调 compute + permission membership test，失败抛 `AuthorizeError`）。`_fetch_role_permissions` 单 JOIN 查 `role_bindings`→`roles` 按 `(org_id, principal_type, principal_id)` + 过滤 `expires_at IS NULL OR > now`，**防御性 drop** 未注册权限串 + `system:*` 串（registry 是写侧权威，读侧对历史脏数据容错）。
  - `AuthorizeError(Exception)` 携带 `.code: ErrorCode` + `.permission: str | None`。**关键**：`ContractError` 是 pydantic BaseModel（数据信封），不是 Exception 子类，不能 `raise`。`AuthorizeError` 是 raise-able 形式，镜像 `TenantContextError`/`PermissionValidationError` 模式；HTTP 层（PR-032/033 router/middleware）从 `.code` 映射 status 码，需要时构造 `ContractError.from_code(...)` 信封。

- **新建 `app/gateway/authorize_cache.py`**：`PermissionCache` Protocol（`get`/`set`/`invalidate`/`clear`，PR-037 可 drop-in Redis 实现）+ `InMemoryPermissionCache`（dict + `time.monotonic()` 时钟，TTL 在 `set` 时钳到 ≤60s 硬约束 ADR §11，lazy eviction on read）。`org_cache_key(org_id, principal_type, principal_id)` = `authz:{org_id}:{ptype}:{pid}`（跨 Org 天然隔离）；`system_cache_key(principal_id)` = `authz:system:{pid}`（system-admin 独立 namespace，ADR §11）。主动失效接口 `invalidate(key)` 留着但 PR-031 不接变更事件（→ PR-037）。

- **`tenancy/membership.py` 扩展**（不改既有 `get_active_membership`——它字面量 `status=="active"` 把 suspended/invited/removed 一视同仁归 None，PR-031 需区分 403 vs 404）：
  - `get_membership_any_status(sf, *, user_id, org_id) -> OrgMembershipRow | None`：按 `(user_id, org_id)` 查任意 status，`uq_org_memberships_org_user` UNIQUE 保证至多 1 行（无 MultiMembershipError 面）。
  - `get_org_status(sf, *, org_id) -> str | None`：查 `OrganizationRow.status`（active/suspended/deleting/deleted），用于 `organization_state` 维度。

- **错误码映射**（ADR §12 + testing-strategy §9.2，全部用既有 ErrorCode，无新增）：
  - invited / removed / 无 membership / 无 Org 行 → `PERMISSION_DENIED`（router 包装 **404**，存在性隐藏，ADR §12「不得用 permission_denied 暴露 Org 范围」）
  - suspended membership / 权限不足 → `PERMISSION_DENIED`（**403**）
  - org suspended → `ORG_SUSPENDED`（403）
  - org deleting / deleted → `ORG_DELETING`（403）
  - 非 user principal（service_account/system）→ `AUTHENTICATION_INVALID`（401，PR-034/后续扩展）
  - **HTTP 映射延后 PR-032/033**——PR-031 只抛 `AuthorizeError`，router 层根据 `.code` + 上下文（membership 缺失信号 404 vs 权限不足 403）决定 status。

- **严格回滚红线**（runtime-contracts §16.34:1190 + pr-split-guide §8:350）：**不改 `authz.py`**（`_authenticate`/`_ALL_PERMISSIONS`/`require_permission`/`AuthContext` 全部保持原样）/ **不动 `require_admin_user`** / **不碰任何 router 装饰器**。PR-031 合入时**零调用方**——`authorize()` 只被自己的单测调用，真实 router 路径要等 PR-032（runtime router）/PR-033（admin/studio router）。这保证 `git revert` 不影响任何现有 API 行为（否则一次 bug 锁死全平台 ~30 个 `@require_permission` 调用点）。

- **测试**（48 新测，`test_iam_authorize.py`）：membership helpers（active/non-active/missing/wrong-org + org status 4 态）+ 纯函数 5 例（admin 短路/user 路径/scope 收窄/scope=None 全集/admin+scope 仍收窄）+ cache（roundtrip/missing/ttl=0/clamp 60s/invalidate/clear/system vs org namespace）+ AuthorizeService（3 角色矩阵对齐 registry/admin 短路/无 binding 空集/多 binding 并集/过期 binding 排除/未来 binding 包含）+ §9.1 矩阵 spot-check（admin×console/run/prod vs developer vs viewer）+ §9.2 拒绝（invited/removed/suspended/无 membership/suspended org/deleting org/deleted org/missing org 共 8 态）+ scope 收窄（admin+scope/user+scope/None 全集）+ cache（hit/invalidate/system namespace）。marker 只用 `@pytest.mark.anyio`+`@pytest.mark.parametrize`，docstring 引用 `IAM-0xx`。

### 16.36 PR-031 不包含

**严格不越界**（避免锁死全平台 API + 避免 Track C 后续 PR 越界）：

- **router 切流**：所有 `@require_permission("threads"/"runs"/"artifacts", ...)` 调用点（threads/runs/thread_runs/artifacts/feedback/suggestions/uploads 共 ~30 处）保持现状走 stub → **PR-032 Runtime Router**。admin router（admin.py/channels.py/channel_connections.py/mcp.py 共 9 处 `require_admin_user`）继续用 `system_role` 门控 → **PR-033 Admin/Studio Router**。
- **`authz.py` 旧 stub 删除**：`_ALL_PERMISSIONS` 全放行 + 旧两段式 `Permissions` 类（`threads:read` 等）保持原样，**等 PR-031/033 整体替换**（runtime-contracts §16.34:1190 约定）。PR-031 不碰 `backend/app/gateway/authz.py`。
- **ServiceAccount 生命周期**：principal_type 多态已支持（cache key + 查询都带 principal_type），但 SA 创建/disable/delete/限流 → **PR-034**。当前 SA principal 走 user 路径会因缺 user_id 抛 `AUTHENTICATION_INVALID`（设计如此，PR-034 扩展）。
- **API Key 全套**：scope 交集计算原语已交付（`compute_effective_permissions` 的 `api_key_scopes` 参数 + `AuthorizeService.compute_permissions_for_user` 的 scope 收窄），但 Key 生成/hash/校验/明文返回/过期轮换撤销 → **PR-035**。PR-031 只接受调用方已校验后的 scopes。
- **OIDC group mapping**：PR-031 只读已存在的 RoleBinding 行，不从 OIDC group 合成角色 → **PR-036**。
- **主动失效 + SSE 重验证**：缓存 TTL=60s 是 fallback 兜底（ADR §11「主动失效失败时仍不得超过 60 秒」），但 Membership/RoleBinding/SA/Key 变更的主动 invalidate + SSE 重验证 + P99 撤权证据 → **PR-037**。PR-031 的 `InMemoryPermissionCache.invalidate()` 留接口但不接变更事件。
- **resource-level check**：`authorize()` 的 `resource_ref` 参数保留但 MVP 不用——Workspace 级 RBAC 是**非目标**（ADR §17）。
- **UserRow status 字段**：data-model §4.3 列了 `status: active/disabled` 但 ORM 未实现，`active_principal` 维度当前只能靠 `token_version`/`needs_setup` 粗粒度。完整 user disable 延后到独立 PR（不阻塞 Track C）。
- **policy_decision**：ADR §6 公式的最后一项，Track E（Policy 引擎）独立工程，PR-031 视为恒全集（预留 `policy_evaluator` 注入点但 MVP 不接）。
- **ErrorCode 新增**：PR-031 所需 code（`PERMISSION_DENIED`/`ORG_SUSPENDED`/`ORG_DELETING`/`AUTHENTICATION_INVALID`）全部已在 `contracts/errors.py` registry，无新增。
- **全局 ContractError exception handler**：当前代码库无 `@app.exception_handler(ContractError)`，每个调用点手动映射（照 `tenant.py:284-299`）。PR-031 不引入新模式，HTTP 映射由 PR-032/033 router 层在调用 `authorize()` 时按 `.code` 决定。

### 16.37 PR-032：Runtime Router 切 RBAC（已交付）

Track C 第三刀落地。把 Thread/Run/Artifact/Upload/Feedback/Suggestion 七个 runtime router 的 `@require_permission` 旧 stub 切到基于 PR-031 `AuthorizeService.authorize()` 的新装饰器 `@require_rbac`，**真正激活 DB-backed 权限交集计算**（此前 `_ALL_PERMISSIONS` flat-grant 让所有 authenticated user 拿全权）。

- **新装饰器 `app/gateway/rbac.py`**：`require_rbac(permission: Permission, *, owner_check=False, require_existing=False)`。流程：复用 `_deerflow_test_bypass_auth` 测试 bypass（direct-call stub 设实例属性 / TestClient middleware 设 `request.state`，两路共存因 Starlette `BaseHTTPMiddleware` 重建 Request 对象）→ `get_tenant_context()` None→401 → **IM channel worker 白名单短路**（`tenant_context.auth_method=='internal'` 且带 `X-DeerFlow-Owner-User-Id` 跳 `authorize()`，只保留 `thread_store.check_access`，TODO 注释等 multi-Org active phase IAM seed 完成后删此分支）→ `authorize()` 异常映射（`_authorize_error_to_http`：`AUTHENTICATION_INVALID`→401 / `ORG_SUSPENDED`·`ORG_DELETING`→403 / `PERMISSION_DENIED`→403 default）→ `policy.evaluated` 观测埋点（allow=INFO / deny=WARNING，observability §3.4 reserved event，不接 AuditEvent）→ `owner_check` 分支照搬 `authz.py:278-313`（`check_access` + `INTERNAL_SYSTEM_ROLE` header owner fallback→404，cross-Org/cross-user 404 的实际承担者，因 `authorize()` 的 `resource_ref` MVP 是 no-op）。
- **范围**：28 个 call site 切换。`threads:delete`→`Permission.RUNTIME_THREAD_WRITE`（ADR §3 delete 归 write，无独立 delete 成员）。计划原列四刀（threads/thread_runs/runs/artifacts），审计发现 uploads（4）/feedback（6）/suggestions（1）也用 Thread 域 `require_permission` + owner_check，与四刀共享同一 `thread_id` 权限语义，全部纳入避免同一 thread_id 的 read/write/delete 一半走 RBAC 一半走 flat-grant 的割裂。
- **测试基础设施 `_router_auth_helpers.py`**：`make_rbac_test_app` 双模式（`bypass_authorize=True` 给业务逻辑测试零 DB 通过，stamp `_deerflow_test_bypass_auth` on `request.state`；真 `sf` 给边界测试走 `authorize()`，rebind `AuthorizeService` 单例）+ `bootstrap_rbac` seed helper 系列（org+builtin roles+user+membership+binding，`test_iam_authorize._bootstrap` 的公共镜像）+ `rbac_sf` fixture（conftest，teardown `reset_authorize_service_for_testing` 避免单例跨测试泄漏）+ `_make_rbac_stub_user`（固定 UUID = `RBAC_DEFAULT_USER_ID` 匹配 seed）。
- **矩阵测试 `test_rbac_runtime_routers.py`** 23 测：§9.1 角色×能力 15 cell（Oracle=`BUILTIN_ROLE_PERMISSIONS`，admin/developer 全允许 runtime 域，viewer 拒 write/create/cancel）+ §9.2 状态映射 4（无 membership / suspended membership / suspended org / 无 binding 全→403）+ trusted-internal-caller 白名单短路（200）+ owner_check→404 + `policy.evaluated` allow（INFO）/deny（WARNING）观测。挂最小 dummy router 隔离 handler 副作用，URL path 用 `Permission.name`（非 `.value`，因 `runtime:thread:read` 的冒号不安全）。
- **测试迁移**：6 个 router 业务测试（threads/artifacts/runs_api/messages_pagination/token_usage/cancel_run_idempotent + skills_custom_router 跨界 uploads）迁移到 `make_rbac_test_app(bypass_authorize=True)`；1 个 owner_check 边界测试（uploads `owner_check_passes=False`）走真路径。`test_stateless_runs_owner_isolation` 不迁移（测 services 层 owner check，不经 router 装饰器）。
- **e2e 适配**：`require_rbac` 生效后 `/register` 用户无 IAM membership 被 403（设计预期：single-Org bootstrap 阶段只有 `/initialize` 创建的首个 admin 有完整 IAM）。两条路径：(a) 后端 e2e（`test_runtime_lifecycle_e2e` / `test_replay_golden` / `test_setup_agent_http_e2e_real_server`）的用户引导从 `/register` 改 `/initialize`（在 app event loop 内 seed IAM，避免 sync TestClient 路径跑 async seed 与 aiosqlite 连接池冲突）；(b) 前端 Playwright e2e（`real-backend-render` / `multi-run-order`）仍用 `/register`（浏览器不能直接调 `/initialize` 的「首个 admin」约束），改为 register 后调新增的 test-only `/api/test-only/seed-admin-iam` 端点（挂在 `seed_runs_router.py`，仅由 `scripts/run_replay_gateway.py` 在 `DEERFLOW_ENABLE_TEST_SEED=1` 时挂载，不进生产）。三个后端 e2e 的 `_reset_process_singletons` 副本补齐 `AuthorizeService._default_service`（PR-031 引入的新单元）+ `deps._cached_local_provider`/`_cached_repo`（admin 计数泄漏源）reset——此前这些 reset 列表未纳入 PR-031/032 引入的进程级单例。
- **旧 stub 保留（PR-032 时点）**：PR-032 不动 `authz.py`（`require_permission`/`_ALL_PERMISSIONS`/`AuthContext`），保留给 Admin/Studio router（PR-033）和它们的测试继续用。**PR-033 已完成此删除**（见 §16.39）—— ADR §14 step 10 的触发条件「PR-033 切完 + 旧 acceptance §15 勾掉」于 PR-033 满足。

### 16.38 PR-032 不包含

**严格不越界**（避免锁死后续 PR + 避免 Admin/Studio 切流越界）：

- **`authz.py` 旧 stub 删除**：`require_permission`/`_ALL_PERMISSIONS`/`AuthContext` 保持原样，**等 PR-033** 切完 Admin/Studio router 后整体替换。PR-032 只新增 `rbac.py`，不改 `authz.py`。
- **Admin/Studio router 切流**：admin.py（`require_admin_user` + admin 域权限）/channels.py/channel_connections.py/mcp.py/memory.py/metrics.py 等继续用 `system_role` 门控或 `require_permission` 旧 stub → **PR-033**。
- **stateless `/api/runs/stream|wait` owner check 迁移**：这两个端点 undecorated（`@router.post` 无 `@require_rbac`），owner check 在 `services.start_run` 内（`services.py:335-357`，因 thread_id 在 body 不在 path，path-based `owner_check` 装饰器无法保护）。PR-032 不动，`test_stateless_runs_owner_isolation` 覆盖现状。
- **ADR §12 invited/removed→404 细化**：`PERMISSION_DENIED` 默认映射 403。single-Org bootstrap 阶段实际不存在 invited/removed 场景（membership 由 `/initialize` 创建为 active），cross-Org→404 已由 `check_access` 承担。multi-Org active phase 启用后 follow-up 细化。
- **IM channel worker 白名单短路移除**：当前 `auth_method=='internal'` + `X-DeerFlow-Owner-User-Id` 跳 `authorize()` 是 temporary shim（单 Org bootstrap 阶段不给 connection owner seed IAM 行）。TODO 注释：等 multi-Org active phase IAM seed 完成后改成全走 `authorize()`、删此分支。
- **普通 `/register` 用户 IAM 引导**：PR-032 让 RBAC 生效后，`/register` 创建的用户无 membership/role binding，调 runtime router 会 403。这是 single-Org bootstrap 阶段的设计预期（只有 `/initialize` 的首个 admin 有完整 IAM）。普通用户的 IAM 引导（自动 seed default org membership + 默认 role）延后到 **PR-034 ServiceAccount** 或独立的 membership 引导 PR——取决于产品设计（自服务 join vs admin 邀请）。当前 e2e 测试用 `/initialize` 绕过。
- **API Key scope 收窄**：`authorize()` 已支持 scope 交集（PR-031），但 runtime router 不接 API Key 路径 → **PR-035**。
- **主动失效**：`require_rbac` 每次请求调 `authorize()`，走 `AuthorizeService` 的 TTL≤60s 缓存。Membership/RoleBinding 变更的主动 invalidate → **PR-037**。

### 16.39 PR-033 Admin / Studio Router 切 RBAC（已交付）

Track C 第四刀。PR-032 切完七个 runtime router 后,Admin 域四个 router
仍走 `deps.require_admin_user`(`system_role == "admin"` 临时门控)。
PR-033 把它们切到 `@require_rbac` + `AuthorizeService.authorize()`,并
整体删除 `authz.py` 旧 stub(ADR §14 step 10)。

- **router 切换(9 个 call site)**:
  - `admin.py` 3 端点(`GET /stats`/`/runs`/`/usage`)→ `Permission.ADMIN_CONSOLE_READ`(read-only Org Console,ADR §4.1 `org:admin` 携带)。
  - `channels.py` 1 端点(`POST /{name}/restart`) + `channel_connections.py` 2 端点(`POST|DELETE /{provider}/runtime-config`) + `mcp.py` 3 端点(`GET|PUT /mcp/config`、`POST /mcp/cache/reset`)→ `Permission.ADMIN_ORG_MANAGE`(system 级运维操作,`org:admin` 独有)。
  - 非 admin 端点(`GET /api/channels/`、`GET /api/channels/providers` 等 read-only)保持无装饰器。
- **`authz.py` 删除**:`require_permission`/`require_auth`/`AuthContext`/`Permissions`/`_ALL_PERMISSIONS`/`_authenticate`/`get_auth_context`/`_make_test_request_stub` 整体移除。`auth_middleware.py` 不再 stamp `request.state.auth`(只 stamp `request.state.user` + `auth_source`,权限检查交给 `@require_rbac` + AuthorizeService)。`deps.require_admin_user` 删除(9 个调用点全部切走)。
- **`system_role == "admin"` 不是放行源**:`authorize()` 内部把 user 写死为 `system_role="user"`(走 cache path),依赖 TenantContext.principal。一个 `system_role="admin"` 但**无** `org:admin` binding 的用户调 admin 端点被 403 —— admin 的真实权限来源是 `/initialize` seed 的 `org:admin` RoleBinding,不是 `system_role` 字段。矩阵测试 `test_rbac_admin_routers.py::TestSystemRoleAdminIsNotAGrant` 锁定此语义。
- **矩阵测试 `test_rbac_admin_routers.py`** 13 测(IAM-206/207/208/209):§9.1 角色×能力 6 cell(Oracle=`BUILTIN_ROLE_PERMISSIONS`,admin 全允许 admin 域,developer/viewer 全拒)+ §9.2 状态映射 4(无 membership / suspended membership / suspended org / 无 binding 全→403,用 `ADMIN_CONSOLE_READ` 作为最 permissive admin 能力)+ `system_role="admin"` 无 binding → 403 / 加 binding → 200(2 测)+ `policy.evaluated` allow(INFO)/deny(WARNING)观测(2 测)。挂最小 dummy router 隔离 handler 副作用。
- **测试迁移**:
  - 5 个 router 业务测试(`test_admin_console_api`/`test_channels_router`/`test_channel_connections_router`/`test_stateless_runs_owner_isolation`/`test_mcp_config_secrets`)迁移到 `make_rbac_test_app(bypass_authorize=True)`。
  - 3 个 `test_403_when_not_admin` 重复 case(admin_console_api 每端点一个)删除 —— 矩阵测试一次性覆盖三个 admin capability × 三个角色的全部 cell,per-endpoint 重复测试是冗余。
  - `test_mcp_config_secrets` 的 2 个直接调用 endpoint 测试(cache reset + update 副作用)改用 `call_unwrapped` 走 `__wrapped__` 跳过装饰器,只测 handler 业务副作用。
  - `test_auth.py` 删除 ~210 行 `AuthContext`/`require_auth`/`require_permission` 直测块(12 个 test),保留 JWT/password/User 模型测试。新装饰器的等价覆盖在 `test_rbac_runtime_routers.py`(runtime 域)+ `test_rbac_admin_routers.py`(admin 域)。
- **`_router_auth_helpers.py` 重构**:删除 `_StubAuthMiddleware`(唯一作用是 stamp `request.state.auth`,无消费方)/`make_authed_test_app`/`_STUB_PERMISSIONS`/`AuthContext` import。统一走 `make_rbac_test_app` 双模式。bypass 模式**不** bind TenantContext —— autouse `_auto_user_context` fixture(或 test-specific `_bound_tenant`)已 bind `org_id="default"`,ContextVar 继承让它透传到 request task;re-bind 会 clobber fixture 的值。
- **ADR §15 验收勾选**:本 PR 勾掉「Router 无手写角色判断」+「旧 flat permission 放行路径已删除或被 Feature Flag 完全关闭」两项。其余 9 项(Viewer/Developer/API Key/membership/P99/最后 admin 保护/OIDC/system-admin 跨 Org/正反向矩阵)需 PR-034/035/036/037/040 等后续 PR。

### 16.40 PR-033 不包含

**严格不越界**:

- **Studio router 切流**:无 `studio:*` 权限的 router 存在今天(Package/Release/Catalog 是 Track E PR-054 系列)。`Permission.STUDIO_*` 已在 PR-030 注册表,等 router 落地时直接挂 `@require_rbac(Permission.STUDIO_*)`。
- **stateless `/api/runs/stream|wait` owner check 迁移**:与 PR-032 相同,这两个端点 undecorated,owner check 在 `services.start_run` 内(`services.py:335-357`)。`test_stateless_runs_owner_isolation` 继续覆盖现状。
- **memory.py / metrics.py 审计**:这两个 router 当前无 admin gate(memory.py 走 internal_auth header 校验、metrics.py 是 `/metrics` public path)。如未来加 admin 门控,直接挂 `@require_rbac`,无需先走 `require_admin_user` 中转。
- **`system_role == "admin"` 短路语义重设**:`authorize()` 内部 user 写死 `system_role="user"`(走 cache path),`system_role` 字段只在 `compute_permissions_for_user` 入口读一次(走 admin 短路 `SYSTEM_PERMISSIONS` —— 只含 `system:*` 前缀,不含 `admin:*`)。本 PR 不改这一行为;未来若要让 `system_role="admin"` 跨 Org 走专用接口(ADR §4.4),需 plumb `system_role` through `TenantContext.principal` —— 留给 system-admin 跨 Org PR。
- **主动失效 / API Key scope / invited-removed → 404 细化**:同 PR-032 §16.38,延后 PR-035/037/multi-Org active phase。

### 16.41 PR-034：ServiceAccount 生命周期 + Authorize 分支（已交付）

PR-034 让 IAM 四表(PR-020B)中的 `service_accounts` 首次**有数据**,让 `role_bindings.principal_type='service_account'` CHECK 约束首次**有调用方**,并完成 ADR §6 `authorize()` 对 `principal_type="service_account"` 的真实分支。这是 PR-020B schema 落地以来的首次"运行时 + 写路径"PR(纯 schema 表此前零数据)。

**Schema 迁移 `0008_service_account_fields`**(expand-only,链自 `0007_builtin_roles`):给 `service_accounts` 加 5 列 ADR §9.1 traceability 字段,全 nullable 不破坏现有行 —— `owner_user_id`(管理责任人,无 FK,沿用 polymorphic 约定)/`purpose`/`system`/`environment`/`expires_at`(到期评审日期,**非**凭证过期)。`status` CHECK 已是 `active`/`disabled`,**不加 `deleted`** —— 删除走硬删 + 同事务清理 bindings/keys(ADR §12)。`test_persistence_bootstrap*.py` 的 HEAD 常量从 `0007_builtin_roles` 同步更新到 `0008_service_account_fields`(3 处)。

**`authorize.py` 三分支重构**:`AuthorizeService.authorize()` 把 PR-031 的 user-only 硬分支(`principal.type != "user"` → `AUTHENTICATION_INVALID`)改成 user / service_account / 其他三分支。新增 `compute_permissions_for_service_account` + `_compute_service_account_permissions`(并行于 user 路径,但 active-principal 维度从 `OrgMembership.status` 换成 `ServiceAccountRow.status` —— SA 无 Membership 概念)。`org_cache_key(org_id, principal_type="service_account", principal_id=sa_id)` 命名空间分离(同 principal_id 在 user / service_account 两个 key 下互不污染,IAM-220 测锁定)。新增 `AuthorizeService.invalidate_principal(org_id, principal_type, principal_id)` 主动失效入口,同时为 PR-037 铺路。

**`rbac.py` 补 `PRINCIPAL_DISABLED` 分支**:此前 disabled SA fall through 到 `PERMISSION_DENIED → 403 "Permission denied"`,状态码巧合正确但响应体错误。本 PR 加 explicit 分支返回 `403 "Principal is disabled"`,与 ADR §12 `principal_disabled` 错误码目录对齐。

**Harness 层 `persistence/iam/repository.py`**(新):纯 DB CRUD(create/get/list/update/set_status/delete_service_account + create/list/delete_role_binding),无 audit / 无 cache / 无 authz(那些是 app 层职责)。`delete_service_account` 在单 `AsyncSession` 内 DELETE role_bindings + delete row,api_keys 走 FK CASCADE —— ADR §12 原子性。Org filter 是强制参数(`ADR §8`)。

**Harness 层 `tenancy/bootstrap.py` 重构**:抽私有 `_ensure_role_binding` polymorphic helper,`ensure_admin_role_binding`(user)+ 新 `ensure_service_account_role_binding`(service*account)都转发,前者签名零变化(向后兼容)。`ensure_admin_role_binding` 仍 emit `admin_role_binding_created` event(保 PR-022 行为),新 helper 不 emit(SA 路径走 router 层的 `service_account*\*` event)。

**Contracts 层 `deerflow/contracts/iam.py`**(新):`ServiceAccountCreateRequest` / `UpdateRequest` / `Response` / `RoleBindingRequest` / `RoleBindingResponse`。`Response` 类用 `from_attributes=True` 直接从 ORM row 投影,避免 API/ORM drift。`UpdateRequest` 故意不含 `status`(lifecycle 走专用 `:disable`/`:enable` endpoint)。

**App 层 `app/gateway/routers/iam.py`**(新,挂 `/api/v1/iam`,10 端点):list/create/get/patch/:disable/:enable/delete + role-bindings 的 list/create/delete。所有读 gate `@require_rbac(Permission.ADMIN_IAM_READ)`,所有写 gate `@require_rbac(Permission.ADMIN_IAM_MANAGE)` —— 两者都只在 `org:admin`(PR-030 registry pin),developer/viewer 全拒。Cross-Org 隔离 404(existence-hiding)。每个写操作 commit 后调 `emit_tenant_event("service_account_*", ...)`(沿用 logger shim,TODO PR-041 真 outbox)+ `get_authorize_service().invalidate_principal(...)`。DELETE 顺序:**先 emit audit(带 sa_id+name)→ 再 repository delete → 再 invalidate**(audit 在删之前保留 ID 痕迹)。

**`_router_auth_helpers.py` 扩展**:`bind_rbac_role` 加 `principal_type`/`principal_id` 参数(默认值向后兼容);新增 `seed_rbac_service_account` + `bootstrap_rbac_service_account`(SA 版的 `bootstrap_rbac`)。

**测试**(共 +69 测,128 测总通过 on PR-034 test set):

- `test_iam_service_account_repository.py`(新,20 测):DB CRUD 全覆盖,含 cross-Org filter、delete 原子清理 bindings、CHECK 拒未知 status/principal_type、role binding polymorphic helper。
- `test_iam_authorize.py` 扩展(+15 测 IAM-220 系列):active/disabled SA、cross-Org hidden as AUTHENTICATION_INVALID、PRINCIPAL_DISABLED raise、expired binding 排除、suspended org、API Key scope 收窄、cache 命名空间分离(user vs SA 同 id 不撞)、`invalidate_principal` 后重算、`authorize()` 入口三分支、`_authorize_error_to_http` PRINCIPAL_DISABLED→403。
- `test_rbac_iam_router.py`(新,12 测 IAM-210/211/212):§9.1 角色×能力 6 cell(admin 全允,dev/viewer 全拒)+ §9.2 状态映射 4 + `policy.evaluated` 观测 2。镜像 `test_rbac_admin_routers.py` 结构。
- `test_iam_router_business.py`(新,12 测 IAM-310~314):create/get/list/patch/disable/enable/delete 完整 lifecycle、cross-Org 404 隔离、409 重名 / 404 缺失 / 400 等错误路径、role binding lifecycle + delete cascade、audit event emit + cache invalidate 顺序断言。
- `test_iam_schema.py` 扩展(+2 测):5 新列 nullable parity、owner_user_id 可重复(无 unique);新增 `test_service_account_fields_round_trip`(0008 ↔ 0007 双向)。

**ADR §15 验收**(详见 ADR 文档勾选):2 项新勾 + 1 项注释,加上 PR-033 已勾的 2 项,共 4/11 项。

**`alembic` 双向验证**:`upgrade head` → `downgrade 0007_builtin_roles` → `upgrade head` round-trip 成功,test_iam_schema 显式覆盖。

### 16.42 PR-034 不包含

**严格不越界**:

- **API Key 全套**:本 PR **不**实现 `AuthMiddleware` 的 `X-Api-Key` header 解析、`ApiKeyRow` 写入 / `key_hash` 恒定时间校验 / mint-rotate-revoke 端点。ServiceAccount 可以创建 / 绑定角色 / 在 service 层被 `authorize()` 验证,但**还不能在生产 HTTP 路径被触达** —— `tenant.py:resolve_principal` 仍只发 `type="user"`,等 PR-035 的 API Key 中间件分支才会发 `type="service_account"`。这与 PR-031「authorize 落地但零 router 调用方」的切分哲学一致:本 PR 的 authorize 分支靠 service-layer 单测(直接构造 `PrincipalRef(type="service_account", ...)`)覆盖,PR-035 落地后端到端可触达。
- **`tenant.py` resolver 分支**:`resolve_principal` / `resolve_tenant_context` 仍 user-only。SA 分支与 API Key header 解析强耦合,归 PR-035。
- **`AuditEvent` 真 outbox**:沿用 `emit_tenant_event` logger shim(同 PR-022 模式),每个 emit 调用标 `TODO(PR-041)`。真 `audit_events` 表 + outbox publisher 是 PR-041。
- **主动失效 SSE re-validation**:`invalidate_principal` 单进程内同步,跨进程协调 + SSE 60s re-validation 是 PR-037。
- **`system:admin` SA**:ADR §4.4 + §8 禁止把 `system:admin` 作为普通 RoleBinding。本 PR 无需额外 guard —— `validate_role_permissions` 写侧禁止 system perms 进 Org 角色(注册表层强制),SA 只能绑 Org 角色,自动满足。是否允许 SA 持 `system:admin` 跨 Org 操作,future PR。
- **rate limiting**(ADR §9.3):`org_id + service_account_id` / `org_id + api_key_id` / `source_ip` 三元限流延后到 PR-035(API Key 落地后才有 rate-limit 维度可挂)+ 平台限流 PR。
- **frontend IAM 管理 UI**:前端目前只有 Org Console(runs/usage/audit),无 principal 管理页。本 PR 后端 API 已就位,前端页面是后续 Track D/E UI PR。

### 16.43 PR-035:API Key 凭证路径(已交付)

PR-035 让 PR-034 的 `authorize()` `service_account` 分支**在生产 HTTP 路径端到端可触达**。交付 ADR-0003 §9.2 Key 全套 MUST:`X-Api-Key` / `Authorization: Bearer` header 解析、`key_hash` 恒定时间校验、mint / list / revoke 端点、明文一次性返回、scope 收窄、过期 / 撤销、审计。

**Schema 零变更**:`api_keys` 表(PR-020B 的 `0004_iam_tables`)已经有 PR-035 需要的全部列(`id`/`org_id`/`service_account_id` FK CASCADE/`key_prefix` String(16) unique/`key_hash` String(255)/`scopes` JSON/`expires_at` NOT NULL/`revoked_at` nullable/`created_at`/`last_used_at`)。无新 migration,无 ORM 变更。

**新模块 `app/gateway/auth/api_key.py`**:版本化 hash 模板,但用 HMAC-SHA256 + pepper(不用 bcrypt —— API key 高频鉴权,bcrypt ~100ms/call 会累积)。明文格式 `dk_live_<prefix8>_<secret43>`(60 chars):`dk_live_` 是 8-char 可读前缀,`<prefix8>` 是 DB `key_prefix` lookup 键(正好 String(16) 列宽),`<secret43>` 是 `secrets.token_urlsafe(32)` = 256 bits 熵。`key_hash = HMAC-SHA256(pepper, plaintext).hex()`(64 chars,String(255) 富余),版本前缀 `$dfakv1$`。校验用 `hmac.compare_digest`(codebase 已用 3 处,PR-035 复用)。

**新 env `AUTH_API_KEY_PEPPER`**(`auth/config.py`):镜像 `AUTH_JWT_SECRET` 模式(env 优先 → 否则 auto-generate + persist 到 `.api_key_pepper`)。**不与 JWT secret 复用**(defense-in-depth:JWT 实现 bug 不应泄露 HMAC pepper)。`AuthConfig.api_key_pepper` 加 default="" 让现有 `AuthConfig(jwt_secret=...)` 测试构造不破坏。

**`AuthMiddleware` API-key 分支**(`auth_middleware.py:88` 之后,`internal_user` 解析之前 —— 外部入口优先):`_strip_bearer` helper 处理 `Authorization: Bearer <key>` 形式;`_resolve_api_key` 走 ADR §12 完整序列(existence via `get_api_key_by_prefix` → hash `verify_api_key` 恒定时间 → expiry → revocation,任一失败 → 401 `authentication_invalid` → SA `disabled` → 403 `principal_disabled`)。成功后 stamp `request.state.user`(SA-backed SimpleNamespace 满足现有 `getattr(user, "id")` 模式) + `AUTH_SOURCE_API_KEY` + 4 个 `request.state.api_key_*` 字段(`api_key_id`/`api_key_org_id`/`api_key_scopes`/`service_account_id`)。`touch_api_key_last_used` 采样式 fire-and-forget(每 key 每 60s 至多 1 次 UPDATE,符合 ADR §11 TTL 颗粒度,swallow exception)。SQLite tzinfo 剥离问题用 `astimezone(UTC)` 兜底。

**`tenant.py` SA 分支**:`resolve_principal(user, request, auth_source)` 加 API_KEY 分支 emit `PrincipalRef(type="service_account", id=sa_id)` 无 `user_id`(PrincipalRef validator 强制)。`resolve_tenant_context` 在 phase 检查之前 short-circuit SA path:org_id 来自 `request.state.api_key_org_id`(Key row 本身编码 org 归属,与 multi_org.phase 无关),跳过 `get_active_membership`(SA 无 Membership)。`_BOOTSTRAP_AUTH_METHOD_MAP` 加 `"api_key": "api_key"`(`AuthMethod` 字面量已 reserved)。`AUTH_SOURCE_API_KEY = "api_key"` 常量加到 `auth_disabled.py` + `tenant.py`。

**`TenantContext.api_key_scopes: frozenset[str] | None = Field(default=None)`**(`contracts/context.py`):trusted carrier 新字段,`None` = universe。frozen pydantic model + frozenset 原生支持。

**`rbac.py` 一行改动**:`authorize(tenant_context, permission, api_key_scopes=tenant_context.api_key_scopes)` —— PR-031 已落地的 hook 终于在生产路径被填充。`policy.evaluated` event 加 `api_key_id=getattr(request.state, "api_key_id", None)` kwarg 用于观测。

**Harness 层 `persistence/iam/repository.py` 加 6 个 helper**:`create_api_key` / `get_api_key` / `get_api_key_by_prefix`(auth-middleware lookup)/ `list_api_keys`(Org-scoped)/ `revoke_api_key`(idempotent,monotonic)/ `touch_api_key_last_used`(采样式)。导出到 `persistence/iam/__init__.py`。

**Contracts 层 `deerflow/contracts/iam.py` 加 3 个 envelope**:`ApiKeyCreateRequest`(scopes 必填非空 pydantic `min_length=1`,router 层 `validate_role_permissions` 拒未知 + system 前缀)/ `ApiKeyResponse`(读 envelope,NO plaintext,NO hash —— field set 本身是 guard)/ `ApiKeyCreateResponse(ApiKeyResponse)`(加 `plaintext_key`,只在 POST 201 返回)。

**App 层 `routers/iam.py` 加 3 个端点**(`POST/GET/DELETE /api/v1/iam/service-accounts/{sa_id}/api-keys[/...]`):mint 返回 plaintext 一次性 + 前缀碰撞重试一次 → 409;list 不带 plaintext/hash;revoke 幂等(204 on already-revoked)+ cross-Org 404(existence-hiding)+ 防御性 `invalidate_principal`(no-op 但符合 ADR §11 字面)。audit payload 严禁带 plaintext/hash(`_FORBIDDEN_PAYLOAD_KEYS` 已含 `"api_key"`/`"key_hash"` 做 defense-in-depth)。

**测试**(+58 测):

- `test_api_key_crypto.py`(新,14):明文格式 / 版本化 hash / 恒定时间 verify / pepper 隔离 / malformed fail-closed / 10k 样本无 prefix 碰撞。
- `test_api_key_repository.py`(新,17):CRUD + Org filter + revoke 幂等 monotonic + touch 采样 + SA delete FK CASCADE 回归锚。
- `test_api_key_auth_middleware.py`(新,13):ADR §12 完整序列 + e2e scope 收窄 + session cookie fallback + `policy.evaluated` 带 `api_key_id`。
- `test_api_key_router_business.py`(新,14):mint/list/revoke lifecycle + plaintext-once + scope 校验 + cross-Org 404 + audit 不含 plaintext + revoke 调 invalidate_principal。
- `test_plaintext_redaction.py`(新,7):`ApiKeyResponse` field set lock / `_FORBIDDEN_PAYLOAD_KEYS` / 源码 grep 断言 router + middleware 不 log plaintext。

**ADR §15 验收**(详见 ADR):「API Key scope 只能收窄 ServiceAccount 权限」从 partial 注释改全勾 + 「API Key 端到端 mint / rotate / revoke」勾选(用 create + revoke 组合实现轮换)。共 6/11 项。

**`alembic` 零变更**:`api_keys` 表 PR-020B 已就位,无双向 round-trip 需要。

### 16.44 PR-035 不包含

**严格不越界**:

- **Rate limiting**(ADR §9.3 三元限流 `org_id + service_account_id` / `org_id + api_key_id` / `source_ip`):PR-035 让限流维度**可挂**(AuthMiddleware 已 stamp `api_key_id` + `service_account_id` + `source_ip` 可从 request 取),但限流器本身是平台限流 follow-up PR。
- **专用 `:rotate` 端点**:用户确认用 create + revoke 组合实现"轮换"(ADR §9.2 ≤24h 重叠期由运维实践保证,审计日志是 `api_key_created` + `api_key_revoked` 事件对,无独立 `_rotated` 事件)。
- **`AuditEvent` 真 outbox**:沿用 `emit_tenant_event` logger shim(PR-041)。
- **主动失效 SSE re-validation**:`invalidate_principal` 单进程内同步,跨进程协调 + SSE 60s re-validation 是 PR-037。PR-035 的 Key create/revoke 路径**理论上无需** invalidate(scope 收窄在 cache boundary 之后,Key 任何变更不影响 SA full pre-scope set),但 revoke 路径仍**防御性**调用(no-op 但符合 ADR §11 字面,future-proof 反重构)。
- **异常使用审计**(ADR §13 `repeated authentication failure / suspicious key use`):需要重复检测状态机,follow-up。PR-035 只在 auth 失败时 emit 普通 401 response。
- **前端 IAM/API Key 管理 UI**:Track D/E UI PR。
- **`description` 字段持久化**:`ApiKeyCreateRequest.description` 仅用于 audit payload,不存 DB(`ApiKeyRow` 无此列)。未来若 UI 需要,加 migration。
- **`Authorization: Bearer` 与未来 JWT Bearer 的歧义**:本 PR 用 `dk_live_` 前缀区分 API key vs JWT(JWT 有 `.` 分隔)。若未来引入 JWT Bearer,需在 `_strip_bearer` 后做格式分支。
