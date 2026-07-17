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

### 16.7 PR-013：Gateway Tenant 解析适配器（已交付）

落地模块（`backend/app/gateway/`）：

| 模块 | 内容 | 对应章节 |
| --- | --- | --- |
| `config.py` | `DEFAULT_BOOTSTRAP_ORG_ID` 常量 + `GatewayConfig.default_org_id`（env `DEER_FLOW_DEFAULT_ORG_ID`） | §5.2、ADR-0001 §6 |
| `tenant.py` | `TenantResolutionMiddleware`、`resolve_principal`、`resolve_tenant_context`（单 Org bootstrap）、auth-source→`AuthMethod` 映射 | §5.2、api-boundaries §6.1 |
| `app.py` | 中间件注册（tenant 在 auth 之前 add，因 `BaseHTTPMiddleware` 反序执行） | §5.2 |

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

| 模块 | 内容 | 对应章节 |
| --- | --- | --- |
| `app/gateway/tenant_rebuild.py` | `rebuild_tenant_context(envelope)`、`bind_tenant_from_envelope(envelope)` | §5.2 rule 4 |
| `runtime/runs/worker.py` | `RunContext.tenant` 字段（可选，向后兼容）；`run_agent` 入口防御性 rebind | §5.2 rule 3/4 |
| `app/gateway/deps.py` | `get_run_context` 从 contextvar 填充 `tenant` | §5.2 |

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

| 模块 | 内容 | 对应章节 |
| --- | --- | --- |
| `app/gateway/tenant.py` | `resolve_channel_tenant_context(owner_user_id, request_id)`（无 `Request` 解析器，镜像 HTTP 路径的 `resolve_tenant_context`）；`channel_tenant_scope(owner_user_id, request_id)` contextmanager（bind/reset，owner 缺失时 no-op） | §5.2 rule 2/3/6 |
| `app/channels/manager.py` | `ChannelManager._handle_message` 在 `_apply_effective_owner` 后、现有 try/except 外层用 `channel_tenant_scope` 包裹分发 | §5.2 rule 3 |

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

| 模块 | 内容 | 对应章节 |
| --- | --- | --- |
| `contracts/context.py` | `AUTO_ORG` sentinel + `_OrgIdSentinel` + `resolve_org_id(value, *, method_name)`（三态：AUTO_ORG 读 bound tenant，缺失即 `RuntimeError` fail-closed；显式 `str` 覆盖；显式 `None` 绕过） | §5.2、§11.2 |
| `persistence/thread_meta/{base,sql,memory}.py` | `ThreadMetaStore` 全方法加 `org_id` kw；SQL/memory 实现写入盖戳、读取/变更按 `org_id` 过滤（与 `user_id` 并存做纵深防御）；`check_access` 跨 Org 行恒 deny（即便 `user_id is None` 的 permissive 模式） | §7.1 |
| `persistence/run/sql.py` + `runtime/runs/store/{base,memory}.py` + `runtime/runs/manager.py` | `RunStore.put` 仅 insert 盖戳 `org_id`（不可变，update 分支不覆盖）；`get`/`list_by_thread`/`delete` 加 `org_id` 谓词；`RunRecord.org_id` 字段经 `_store_put_payload` 显式线程化，保证 `RunManager` 重试仍 tenant-scoped | §7.2 |
| `runtime/events/store/db.py` | `DbRunEventStore.put`/`put_batch` 软读 tenant 盖戳 `org_id`（Worker PR-014A 防御性 rebind 后通常有 tenant）；`list_messages`/`list_events`/`list_messages_by_run`/`count_messages`/`delete_by_thread`/`delete_by_run` 加 `org_id` 谓词 | §10（run_events） |
| `persistence/feedback/sql.py` | `FeedbackRepository.create`/`upsert`（仅 insert 盖戳，org_id 不可变）；`get`/`list_by_run`/`list_by_thread`/`delete`/`delete_by_run`/`list_by_thread_grouped` 加 `org_id` 谓词 | feedback |
| `app/gateway/services.py` `start_run` | 从 `run_ctx.tenant.org_id` 解析并显式传入 `create_or_reject(org_id=...)` 与 `thread_store.create(org_id=...)`；旧行修复路径补 `org_id=None` | §5.2 rule 3 |
| `app/gateway/deps.py` 启动恢复、`app/gateway/routers/threads.py` 旧行修复 | 显式 `org_id=None`（system-admin 等价扫描/修复路径） | §11.2 |

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
