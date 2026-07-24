# ADR-0005：AuditEvent、写入可靠性与保留

- **状态**：Accepted
- **日期**：2026-07-15
- **决策者**：DeerNexus 项目组
- **关联**：[运行时契约](../architecture/runtime-contracts.md) · [数据模型](../architecture/data-model.md) · [API 边界](../architecture/api-boundaries.md) · [安全基线](../security/baseline.md) · [ADR-0003](0003-rbac-and-service-accounts.md) · [ADR-0004](0004-agent-artifacts-and-release.md)

---

## 1. 背景

RunEvent、日志和数据库历史不能单独承担企业审计：

- RunEvent 面向运行调试，可能随实现变化；
- 日志可能采样、过期或缺少稳定 Schema；
- 业务表只保存当前状态；
- 高风险操作需要证明谁在何时对哪个 Org 的资源做了什么以及结果如何。

DeerNexus 需要稳定 AuditEvent、可靠写入、防普通应用改写、按 Org 查询和可验证归档。

---

## 2. 决策

- AuditEvent 是独立的 append-only 合规证据，不等同 RunEvent；
- 使用版本化 Schema；
- 高风险控制面写与 Audit outbox 同事务；
- 运行时安全事件先持久化到可靠队列 / outbox，再异步写审计表；
- 普通应用账号无 UPDATE / DELETE；
- 租户事件强制 `org_id`；
- 查询默认按 Org；
- 更正通过追加 correction event；
- 热数据至少 90 天，总保留默认至少 365 天；
- 定期导出到权限隔离的对象存储并生成摘要；
- 不宣称可抵御数据库、KMS、对象存储最高权限管理员联合篡改。

---

## 3. Schema

字段以运行时契约为基础：

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

持久化额外记录：

```text
ingested_at
producer
producer_version
partition_key?
archive_batch_id?
```

### 3.1 不变量

- event_id 全局唯一；
- idempotency_key 在生产者约定范围内稳定；
- 租户事件 `org_id NOT NULL`；
- actor.id 是稳定主体 ID，不用 display_name 代替；
- occurred_at 是事件发生时间，ingested_at 是接收时间；
- outcome 必须表达业务结果；
- payload 符合 action 对应 Schema；
- 不记录 Secret、Token、完整 Prompt / Response 或无界文件正文。

---

## 4. Action 命名

格式：

```text
<domain>.<resource>.<verb>
```

要求：

- 小写；
- verb 使用过去式或稳定结果词；
- 不把 UI 页面名作为 action；
- 语义改变时新增 action / schema version，不复用旧名；
- action 注册表由本文维护。

---

## 5. MVP 事件目录

### 5.1 阻断验收最小集

与运行时契约和路线图一致：

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

这组事件不允许豁免。

`release.agent.published` 只表示 ReleaseChannel promote / CAS 成功，不代替 Version 状态进入 published 时的 `catalog.agent_version.published`。

### 5.2 IAM 与 Organization

```text
org.organization.created
org.organization.status_changed
org.workspace.created
org.workspace.archived

iam.membership.invited
iam.membership.activated
iam.membership.suspended
iam.membership.removed
iam.role_binding.created
iam.role_binding.deleted
iam.service_account.created
iam.service_account.enabled
iam.service_account.disabled
iam.service_account.deleted
iam.api_key.created
iam.api_key.rotated
iam.api_key.revoked
iam.oidc_mapping.changed
iam.last_admin.protected
```

API Key 事件只记录 key id、prefix、scope、到期时间和 outcome，不记录完整 Key 或 hash。

### 5.3 Catalog、Connector 与 Release

```text
catalog.agent_package.created
catalog.agent_version.created
catalog.agent_version.reviewed
catalog.agent_version.published
catalog.agent_version.revoked
catalog.skill.changed
catalog.mcp.changed
connector.configuration.changed
connector.secret_reference.changed
release.agent.published
release.agent.rolled_back
```

不在 payload 中记录 Connector Secret 值。

### 5.4 Policy 与 Runtime

```text
policy.tool.denied
policy.approval.required
runtime.run.cancelled_by_actor
runtime.run.reconcile_manual
runtime.cross_org_mismatch.detected
sandbox.security_violation.detected
```

普通 Run 创建、模型 token 和每个低风险工具调用不默认写合规审计，分别进入 Run journal、UsageRecord 和可观测系统；若租户 Policy 要求，可提升为 AuditEvent。

### 5.5 System Admin

```text
system.cross_org.read
system.cross_org.write
system.cross_org.export
system.break_glass.enabled
system.break_glass.disabled
```

system-admin 跨 Org 操作必须有目标 Org、reason、ticket / incident ref（如有）和 outcome。

---

## 6. Payload

每个 action 有独立 JSON Schema。公共字段不重复放入 payload。

允许示例：

```text
changed_fields[]
before_digest
after_digest
role_id
permission_version
release_from_version
release_to_version
policy_rule_id
tool_name
risk_class
external_provider
incident_ref
```

禁止：

```text
authorization_header
cookie
api_key
key_hash
oauth_token
connector_password
full_prompt
full_model_response
full_file_content
signed_url_query
database_dsn
```

需要证明内容时保存 digest、大小、分类和受控对象引用，不复制正文。

---

## 7. 写入等级

### 7.1 Class A：强审计控制面事务

包括：

- Membership / RoleBinding；
- ServiceAccount / API Key；
- Org 状态；
- Connector Secret 引用；
- Agent publish / revoke；
- prod promote / rollback；
- system-admin 跨 Org 写 / 导出。

要求：

- 业务变更与 `audit_outbox` 同事务；
- outbox 写失败则业务回滚；
- 投递按 event_id 幂等；
- 不允许“业务成功但只写日志”。

### 7.2 Class B：运行时安全事件

包括：

- 登录成功 `auth.login`；
- Policy deny；
- require_approval；
- Tenant mismatch；
- Sandbox 安全违规；
- 手工 reconcile。

要求：

- 在返回或结束相关动作前进入可靠本地 outbox / 队列；
- 远端 Audit Store 暂不可用时，只要本地可靠队列写入成功，合法登录可以继续并告警；所有持久化路径均不可写时 fail-closed，不能只写普通日志；
- 投递失败重试并告警；
- 不重复执行原业务动作；
- 队列满时 fail-closed 或受控背压，不能静默丢弃。

### 7.3 Class C：可选访问审计

包括：

- 敏感 Artifact 读取；
- Audit 导出；
- 高敏感 Connector 读取；
- 租户配置的 data access。

是否启用由数据分类和 Policy 决定。system-admin 跨 Org 读取始终审计。

---

## 8. Outbox

最小字段：

```text
id
event_id
org_id
payload
status: pending | processing | published | dead_letter
attempts
available_at
created_at
published_at?
last_error?
owner_token?
```

行为：

- pending 按 available_at 领取；
- 领取使用原子 claim；
- 失败指数退避并有最大间隔；
- 达到重试阈值进入 dead_letter 并 P1 / P2 告警；
- 恢复后按 event_id 幂等；
- published 记录保留到足以完成对账；
- 不在 last_error 保存 Secret；
- Reconciler 可以重新释放过期 processing；
- Outbox backlog 超阈值时 Class A 写入 fail-closed。

---

## 9. 幂等、顺序与时间

### 9.1 幂等

- event_id 由生产者在业务事务内生成；
- 重试复用同一 event_id；
- Audit Store 对 event_id 唯一；
- idempotency_key 支持领域级重复检测；
- correction 使用新 event_id 并引用被更正事件。

### 9.2 顺序

- 不承诺跨 Org 或跨资源全局顺序；
- 单资源历史按 occurred_at + event_id 稳定排序；
- ReleaseEvent / RoleBinding 等领域 row_version 写入 payload 可证明先后；
- arrived late 的事件保留原 occurred_at；
- 查询同时显示 ingested_at 以识别延迟。

### 9.3 时间

- 服务与数据库使用 UTC；
- 时间同步漂移监控；
- occurred_at 不由普通客户端提交；
- 外部事件时间只放 payload，平台 occurred_at 由受信 Adapter 确认；
- 严重时钟漂移产生运维告警。

---

## 10. 存储与防改写

### 10.1 PostgreSQL

- `audit_events` append-only；
- 普通 Runtime / Control Plane Role 只有 INSERT 和授权 SELECT；
- 无 UPDATE / DELETE；
- 更正追加新事件；
- 使用按时间分区时，分区管理由专用运维 Role；
- DDL、分区删除和保留 Job 受变更管理；
- 查询索引包含 Org、时间、Action、Resource。

### 10.2 归档

至少每日：

1. 选择闭合时间窗口；
2. 按稳定顺序导出；
3. 生成记录数、first / last event、文件 digest；
4. 写独立对象存储；
5. 启用 Versioning；
6. 平台支持时启用 Object Lock；
7. 写 archive manifest；
8. 更新 archive_batch_id / 水位；
9. 抽样回读验证；
10. 监控延迟和失败。

归档对象键：

```text
audit/org/{org_id-or-system}/date/{yyyy-mm-dd}/batch/{batch_id}.jsonl
```

Archive Manifest：

```text
batch_id
schema_versions[]
org_id?
window_start
window_end
event_count
first_event_id
last_event_id
content_digest
created_at
storage_version
```

### 10.3 能力边界

MVP 的“防篡改”表示：

- 普通应用账号不可改写；
- 归档与在线库权限隔离；
- 摘要和版本支持发现非授权变化；
- 删除和保留经过专用流程。

不表示：

- 抵御同时控制数据库 Owner、KMS 和对象存储最高权限的联合管理员；
- 已具备外部公证或不可否认签名。

---

## 11. 保留与删除

默认：

| 层 | 保留 |
| --- | --- |
| PostgreSQL 热数据 | ≥90 天 |
| 对象存储归档总保留 | ≥365 天 |
| Outbox published 记录 | ≥7 天或完成对账后 |
| Dead letter | 问题关闭后 ≥30 天 |

规则：

- 法规、合同和 legal hold 从严；
- Org 删除不自动删除未到期 Audit；
- 保留 Job 只能删除已成功归档、通过摘要校验且不在 legal hold 的分区；
- 删除 Job 由专用 Role 执行并再次审计；
- 删除前生成计划、范围和 event count；
- system 事件按平台保留策略。

ADR-0005 冻结默认值；更改必须评估成本、法规和恢复影响。

---

## 12. 查询与导出

### 12.1 Org 查询

需要：

```text
admin:audit:read
```

支持过滤：

```text
time range
action
actor
resource type / id
outcome
run_id
request_id
```

要求：

- 强制 Org；
- Cursor 分页；
- 默认 24 小时，在线查询最大 90 天；
- 导出走异步 Job；
- 查询响应不返回被脱敏字段；
- Audit 查询和导出本身按风险审计。

### 12.2 System 查询

- 仅 system-admin 专用接口；
- reason 必填；
- 目标 Org 明确；
- 查询 / 导出产生 system 跨 Org AuditEvent；
- 不复用普通 Org Repository 的隐式“all org”开关。

### 12.3 导出

- 异步生成；
- 短期下载 URL；
- 文件加密；
- 包含 Schema、时间范围、Org 和 digest；
- 下载授权再次验证；
- 过期自动删除临时导出；
- 不包含 Secret。

---

## 13. Correction

错误事件不能 UPDATE。追加：

```text
audit.event.corrected
```

Payload：

```text
original_event_id
correction_reason
corrected_fields
approved_by?
```

Correction：

- 不改变原事件；
- 只能修正允许字段；
- actor / org / action 等核心身份错误需要安全评审；
- 查询 UI 同时展示原事件和 correction；
- correction 自身不可再次原地修改。

---

## 14. 可观测性

指标：

```text
audit_outbox_pending
audit_outbox_oldest_age_seconds
audit_publish_success_total
audit_publish_failure_total
audit_dead_letter_total
audit_ingest_lag_seconds
audit_archive_lag_seconds
audit_archive_verify_failure_total
audit_query_duration_seconds
audit_export_jobs
```

告警：

- Class A outbox 不可写：P1；
- oldest pending >5 分钟：P2，持续扩大可升级；
- dead letter >0：P2；
- 归档超过 24 小时无成功：P2；
- 摘要校验失败：P1；
- 普通应用角色获得 UPDATE / DELETE：P1；
- 跨 Org 查询证据：P1。

指标不以 org_id 作为无界公共标签；按 Org 诊断使用日志 / Trace 和受控查询。

---

## 15. 测试

- [~] 最小 9 类事件全部产生（PR-044：`auth.login`（成功+失败）+ `policy.tool.denied`（RBAC + 工具护栏双维度）落地，加 PR-042 的 `iam.role_binding.created/deleted`，共 5/9；仍缺 `catalog.skill.changed` / `catalog.mcp.changed` / `release.agent.published` / `release.agent.rolled_back`（Track E）+ `policy.approval.required`（无生产者））
- [~] IAM、API Key、Connector、Release 关键写路径覆盖（PR-042：IAM 全覆盖 —— ServiceAccount 生命周期 / RoleBinding / API Key / OrgMembership / OIDC group mapping 共 14 个写路径全部同事务 enqueue；action 归一化为 `<domain>.<resource>.<verb>` 注册表。Connector / Release 写路径待 Track E，其代码路径尚不存在）
- [x] Class A outbox 失败时业务不提交（PR-042：IAM router 14 个 Class A 写路径重构为 `async with sf() as session:` → 业务写（传 session，不 commit）→ `enqueue_audit_outbox_in_session`（不 commit）→ `session.commit()` 原子提交；outbox enqueue 失败（如 `IntegrityError`）abort 共享事务，业务写一并回滚。`test_audit_class_a.py::TestClassAAtomicity::test_outbox_failure_rolls_back_business_write` 用重复 `event_id` 强制碰撞证明 SA 行不落地。Connector/Release 写路径待 Track E）
- [~] Class B 队列失败时不静默丢失（PR-044：3 个有代码路径的 Class B 事件走 best-effort enqueue —— `auth.login` / `policy.tool.denied`（RBAC + 工具护栏）；`emit_class_b_audit` + `emit_tenant_event(outcome=)` 复用 OutboxAuditSink durable pending 行。ADR §7.2「所有持久化路径均不可写时 fail-closed」真 fail-closed（队列满/全不可写阻塞 login）作后续 hardening 独立 PR，需队列水位/背压状态机）
- [x] event_id / idempotency 重放不重复（PR-040 + PR-041：`audit_events.event_id` PK + `audit_outbox.uq_audit_outbox_event_id` 双层；worker 重复 publish 命中 `IntegrityError` → mark published 不重复；`test_audit_outbox.py::test_drain_idempotent_on_duplicate_event_id` + `test_duplicate_event_id_collides` 锁定）
- [x] OrgA 查询不返回 OrgB（PR-040 存储层 + PR-045 HTTP 端点：`list_audit_events` / `count_by_org` 强制 `org_id` 过滤，org 隔离在存储层锁定；`GET /audit/events` 经 `_require_org_id` + 仓储硬过滤，HTTP 层 org 隔离验证 `test_audit_query_endpoint.py::TestOrgIsolation`）
- [ ] system-admin 查询再次审计
- [x] 普通应用 Role 无 UPDATE / DELETE（PR-040：`BEFORE UPDATE OR DELETE` 触发器（SQLite `RAISE(ABORT)` + Postgres 触发函数）+ in-app INSERT-only repository 双层；`TestAppendOnlyTrigger` 证明 UPDATE/DELETE 在 migrated DB 被拒。注：当前 harness 单连接单 owner，GRANT/REVOKE 多 Role 特权隔离即 §16 step 2 归 ops runbook；触发器使该保证对所有连接成立，不等同特权隔离）
- [x] Payload Secret 扫描通过（PR-040：repository 复用 `contracts.events._scrub_payload` 在写入前剥离 §6 禁键，`test_scrub_strips_forbidden_payload_keys` 用 `model_construct` 绕过 DTO 校验证明二次擦除仍生效）
- [ ] correction 不修改原事件
- [x] 90 天热数据查询可用（PR-045：`GET /api/v1/admin/audit/events` 在线查询端点，7 个 §12.1 过滤器 + `(occurred_at,event_id)` cursor 分页 + 默认 24h 窗 + 在线 90 天窗门控 + `admin:audit:read` 门控；`test_audit_query_endpoint.py` 12 测覆盖）
- [ ] 每日归档、摘要和回读验证
- [ ] 365 天保留策略存在且可监控
- [ ] 备份恢复后归档水位与摘要抽样一致
- [ ] Outbox backlog 和 dead letter 告警可触发

---

## 16. 迁移

1. 创建 audit_events 和 audit_outbox；
2. 建立独立数据库权限；
3. 实现最小 9 类事件和 Class A 全部 action（含 Membership、ServiceAccount、API Key、Org、Connector 与 Release）；
4. 对 Class A 写路径接入事务 outbox；
5. 对运行安全事件接入可靠投递；
6. 建立查询 API；
7. 建立归档 Job 和 Manifest；
8. 回归 Secret 脱敏；
9. 开启关键路径强制门禁；
10. 历史 RunEvent 不批量伪装为 AuditEvent；如需迁移，标记 source=legacy_import。

---

## 17. 后果

### 正向

- 操作证据与运行调试解耦；
- 高风险事务不会“业务成功但审计丢失”；
- Org 查询、保留和归档可验证；
- 后续合规导出有稳定基础。

### 代价

- 写路径增加 outbox 和监控；
- 审计存储与归档产生持续成本；
- 事件 Schema 和脱敏需要治理。

---

## 18. 非目标

- 外部公证 / 区块链审计；
- 首期 SIEM 全量双向集成；
- 所有数据读取都写 AuditEvent；
- 用 AuditEvent 替代 Run journal、日志或 Trace；
- 自动完成所有行业合规认证。
