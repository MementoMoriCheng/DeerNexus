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

| 类型 | 约定 |
| --- | --- |
| ID | UUID 的规范小写字符串；外部系统 ID 可为字符串但必须带 provider |
| 时间 | UTC、RFC 3339、微秒可选；持久化使用带时区 timestamp |
| 枚举 | 小写 snake_case；未知值由消费者安全拒绝或忽略，不能静默映射 |
| 金额 | 十进制定点字符串 + ISO 4217 币种，不使用浮点数 |
| Token | 非负整数 |
| Metadata | JSON 对象；只允许显式白名单字段，不放密钥或完整 Prompt |
| Schema 版本 | `schema_version`，初始值 `v1alpha1` |

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

| 动作 | MVP 策略 |
| --- | --- |
| 创建 Run、选择模型、加载普通 Skill | 创建 Run 时评估并记录 `policy_version` |
| 读取 Agent ReleaseRef | 创建 Run 时评估并固定 |
| 高风险/关键工具调用 | 每次调用实时评估 |
| 外部网络、写操作、凭证访问 | 每次调用实时评估 |
| 长 Run 恢复 | 恢复前重评估 Run admission；已完成步骤不重放 |

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

| Code | 语义 | Retryable |
| --- | --- | --- |
| `tenant_context_missing` | 可信租户上下文不存在 | 否 |
| `tenant_mismatch` | 资源与当前 Org/Workspace 不一致 | 否 |
| `authentication_invalid` | 凭证无效、过期或无法映射主体 | 否 |
| `principal_disabled` | 已认证主体被禁用或撤销 | 否 |
| `org_suspended` | Org 已暂停，不允许新 Run 或发布 | 否 |
| `org_deleting` | Org 正在删除，只允许删除流程、受控导出、审计和取消 | 否 |
| `permission_denied` | 已知作用域内权限不足 | 否 |
| `policy_denied` | 策略明确拒绝 | 否 |
| `policy_unavailable` | 策略无法安全评估 | 视调用方退避重试 |
| `approval_required` | 动作需要企业审批 | 否 |
| `release_not_found` | 通道没有可用制品 | 否 |
| `release_not_published` | prod 指向未发布版本 | 否 |
| `release_revoked` | 制品已撤销 | 否 |
| `release_unpinned` | prod Run 缺少不可变 ReleaseRef / digest | 否 |
| `release_tenant_mismatch` | 制品跨 Org | 否 |
| `release_conflict` | ReleaseChannel 的 If-Match / row version 冲突 | 可 |
| `run_conflict` | 幂等键或状态转换冲突 | 可 |
| `idempotency_conflict` | 同幂等键对应不同请求 | 否 |
| `audit_unavailable` | 强审计写路径不可用 | 可 |
| `validation_error` | 请求不符合 Schema 或业务前置条件 | 否 |
| `rate_limited` | 请求超过主体或 Org 限制 | 可 |

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

- [ ] 并发请求、协程、线程池之间不串 TenantContext
- [ ] 请求完成和异常退出均恢复 ContextVar
- [ ] Worker、Scheduler、IM 可从可信信封重建上下文
- [ ] 上下文缺失或 Org 冲突 fail-closed

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

| 模块 | 内容 | 对应章节 |
| --- | --- | --- |
| `versioning.py` | `CURRENT_SCHEMA_VERSION = "v1alpha1"` | §3、§13 |
| `identity.py` | `PrincipalRef`、`PrincipalType` | §4 |
| `context.py` | `TenantContext`、`AuthMethod`（DTO 与不变量） | §5、§5.1 |
| `errors.py` | `ContractError`、`ErrorCode` 注册表、`is_retryable_code` | §12 |
| `__init__.py` | 公开导出与分阶段交付说明 | §2 |

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

| 模块 | 内容 | 对应章节 |
| --- | --- | --- |
| `policy.py` | `ResourceRef`、`PolicyRequest`、`PolicyDecision`、`PolicyObligation`、`PolicyEvaluator` Protocol；`RiskClass` / `Decision` / `ObligationType` 闭集 | §7 |
| `release.py` | `ReleaseRef`、`ReleaseResolver` Protocol；`ReleaseChannel` 闭集 | §8 |
| `runs.py` | `RunEnvelope`、`PolicySnapshotRef`、`EnvelopeIntegrity`；`EnvelopeSource` / `IntegrityAlgorithm` 闭集 | §6 |
| `approval.py` | `ApprovalTicket`（MVP 仅预留中断引用）、`ApprovalStatus` 闭集 | §9 |
| `events.py` | `AuditEvent`、`AuditSink` Protocol、`UsageRecord`、`UsageRecorder` Protocol；`AuditOutcome` / `UsageStatus` 闭集 | §10、§11 |

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

| 符号 | 语义 | 对应章节 |
| --- | --- | --- |
| `_current_tenant` | `ContextVar[TenantContext \| None]`，`default=None`，名为 `deerflow_current_tenant` | §5.2 |
| `bind_tenant_context(context) -> Token` | 绑定当前任务上下文，返回 reset token | §5.2 |
| `reset_tenant_context(token) -> None` | 用 token 恢复先前值（必须 try/finally） | §5.2 |
| `get_tenant_context() -> TenantContext \| None` | 读取或返回 None，**不回退默认 Org** | §5.2、§5.1 |
| `require_tenant_context() -> TenantContext` | 未绑定时 `raise TenantContextError(TENANT_CONTEXT_MISSING)` | §5.2 |
| `TenantContextError(RuntimeError)` | 携带稳定 `ErrorCode`，入口层可据此产出 `ContractError` 信封 | §12 |

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
