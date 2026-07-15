# DeerNexus MVP 测试策略

> 状态：MVP 强制策略  
> 关联：[90 天 MVP](../roadmap/90-day-mvp.md) · [ADR-0002](../adr/0002-tenant-workspace-keys.md) · [运行时契约](../architecture/runtime-contracts.md) · [数据模型](../architecture/data-model.md) · [API 边界](../architecture/api-boundaries.md) · [安全基线](../security/baseline.md) · [生产 Runbook](../ops/production-runbook.md)

本文把“租户可隔离、权限可验证、Run 可恢复、发布可回滚、操作可审计”转换为可执行测试门禁。跨 Org 隔离、harness 边界、prod ReleaseRef 与关键审计属于阻断级测试，不能用整体覆盖率替代。

---

## 1. 测试目标

1. 证明 OrgA 不能通过任何已知入口观察或操作 OrgB 资源；
2. 证明 TenantContext 在 HTTP、异步任务、Scheduler、IM 和 Worker 中不串租户；
3. 证明 harness 不依赖控制面私有实现；
4. 证明 RBAC、ServiceAccount 和 API Key 遵循默认拒绝；
5. 证明 prod Run 只执行不可变 ReleaseRef，发布与回滚可重现；
6. 证明 AuditEvent、UsageRecord 和幂等写入不会静默丢失或重复；
7. 证明单 / 双副本声明与实际 Run 一致性能力相符；
8. 证明生产 Sandbox、SSRF 与 Secret 控制不能被常见输入绕过；
9. 证明迁移、升级、回滚和备份恢复在约定窗口内可执行；
10. 形成可追踪到 ADR、需求、测试和发布证据的闭环。

---

## 2. 测试原则

- **风险优先**：隔离、授权、Secret、发布和审计优先于 UI 像素与低风险覆盖率。
- **默认负向**：每条成功路径至少配一条未认证、越权、跨 Org 或冲突路径。
- **真实依赖优先**：PostgreSQL、Redis、SSE、迁移与锁语义使用容器或 staging 真实服务，不用纯 Mock 替代。
- **契约单源**：DTO、错误码和 OpenAPI 从文档契约生成 fixture 或做一致性校验。
- **可重复**：测试不依赖固定执行顺序、共享默认 Org 或真实外部账号。
- **失败可诊断**：失败证据包含 request_id、trace_id、org_id、run_id、release digest，但不包含 Secret。
- **不隐藏不稳定**：Flaky 测试必须修复或带 Owner 隔离；阻断级测试不允许长期 quarantine。
- **生产等价**：release gate 使用与生产同类 PostgreSQL、Redis、Sandbox 与配置安全项。

---

## 3. 测试层级

| 层级 | 目标 | 典型范围 | 执行频率 |
| --- | --- | --- | --- |
| Static | 边界、类型、安全模式 | import boundary、lint、type、SAST、Secret scan | 每 PR |
| Unit | 纯业务规则 | DTO、状态机、授权决策、键生成、脱敏 | 每 PR |
| Component | 单服务 + 真实数据层 | Repository、Router、Policy Adapter、Release Service | 每 PR |
| Contract | 生产者 / 消费者一致 | OpenAPI、RunEnvelope、Policy、Audit、Usage | 每 PR |
| Integration | 多组件真实交互 | PG、Redis、SSE、OIDC stub、Sandbox stub / real | 每 PR / Nightly |
| E2E | 用户与管理员关键旅程 | 登录、Run、拒绝、发布、回滚、Console | Nightly / Release |
| Security | 攻击与隔离 | IDOR、SSRF、Webhook、Secret、Sandbox | 每 PR 子集 / Nightly |
| Performance | SLO 与容量 | 并发 Run、SSE、DB、Redis、Sandbox | Nightly / Release |
| Resilience | 故障与恢复 | kill、断网、lease、backup restore | Nightly / Quarterly |

比例不作为硬 KPI。关键风险必须在适合层级被验证，不能为了“单元测试占比”把数据库隔离测试降为 Mock。

---

## 4. 标准测试夹具

所有隔离与授权测试使用明确命名的实体：

```text
OrgA
  WorkspaceA
  UserAAdmin       → org:admin
  UserADeveloper   → org:developer
  UserAViewer      → org:viewer
  ServiceAccountA  → scoped API Key
  Agent demo v1/v2

OrgB
  WorkspaceB
  UserBAdmin       → org:admin
  UserBDeveloper   → org:developer
  UserBViewer      → org:viewer
  ServiceAccountB
  Agent demo v1

System
  SystemAdmin
```

要求：

- OrgA / OrgB 使用不同 UUID，不依赖名称；
- 两个 Org 可存在相同 slug 范围内资源名以暴露错误的全局唯一假设；
- 测试显式传入 TenantContext，不从全局默认 Org 读取；
- fixture factory 默认创建最小资源，不共享可变对象；
- 时间通过可控 Clock；
- UUID、event id、idempotency key 可注入以验证重放；
- Secret 只使用无真实权限的测试值；
- 测试结束清理 ContextVar、数据库、Redis Key、对象和 Sandbox。

---

## 5. 测试 ID 与追踪

格式：

```text
<DOMAIN>-<NUMBER>
```

建议域：

```text
TEN   TenantContext / isolation
IAM   RBAC / API Key
RUN   Run lifecycle / SSE
REL   Agent artifact / release
AUD   Audit / usage
API   API contract
MIG   Migration
SEC   Security / sandbox / SSRF
OPS   Deploy / backup / restore
PERF  Performance
```

PR 描述引用测试 ID；ADR 验收、90 天验收和 Release Evidence 引用同一 ID，避免只写“已测试”。

---

## 6. 多租户隔离矩阵

### 6.1 资源范围

| 资源 | 详情 | 列表 / 搜索 | 写 / 删除 | 统计 / 导出 | 异步 / 缓存 |
| --- | --- | --- | --- | --- | --- |
| Thread | 必测 | 必测 | 必测 | 适用时 | Checkpoint / SSE |
| Run | 必测 | 必测 | cancel / resume | Console | ownership / Stream |
| Checkpoint / Store | 恢复必测 | 不直接暴露 | 写入必测 | 不适用 | namespace |
| Memory | 必测 | 必测 | 必测 | 适用时 | cache |
| Artifact | 元数据 / 下载 | 必测 | 必测 | 导出 | object key / signed URL |
| AgentPackage / Version | 必测 | 必测 | 必测 | Catalog | digest |
| ReleaseChannel | 必测 | 必测 | promote / rollback | 历史 | cache |
| Skill | 必测 | 必测 | 安装 / 禁用 | Catalog | loader |
| MCP / Connector | 元数据 | 必测 | 配置 / 禁用 | 不含 Secret | secret_ref |
| ScheduledTask | 必测 | 必测 | 必测 | 运行历史 | trigger envelope |
| API Key / ServiceAccount | 元数据 | 必测 | 创建 / 撤销 | 使用时间 | auth cache |
| Audit / Usage / Console | 必测 | 查询 / 聚合 | append-only | 导出 | outbox |

### 6.2 每类资源的最低用例

每一行至少验证：

1. OrgA 授权主体成功创建；
2. OrgA 授权主体成功读取；
3. OrgA Viewer 或无相应 permission 的主体写入返回 403；
4. OrgB 使用 OrgA 资源 ID 读取返回 404；
5. OrgB 使用 OrgA 资源 ID 写、删、cancel、resume 返回 404；
6. OrgB 列表、搜索、统计、导出不出现 OrgA 数据；
7. 客户端伪造 Header / Body `org_id=OrgA` 不覆盖 OrgB TenantContext；
8. WorkspaceA 与 OrgB 组合返回 404；
9. Redis / Checkpoint / Object Key 包含正确 Org namespace；
10. system-admin 跨 Org 操作成功且产生 AuditEvent。

最低规模不是“12 个测试”，而是 12 类资源对适用操作的完整矩阵。允许参数化实现，但失败报告必须指出资源和操作。

### 6.3 特殊旁路

必须单独测试：

- 直接按 UUID 读取；
- Cursor 分页下一页；
- 模糊搜索和排序；
- Console 聚合；
- Batch API；
- SSE / Last-Event-ID；
- Signed URL；
- Cache hit；
- Scheduler；
- IM / Webhook；
- 重试与幂等命中；
- 软删和归档资源；
- suspended Org；
- deleting Org；
- system-admin。

跨 Org 泄露测试失败立即阻断合并与发布。

---

## 7. TenantContext 测试

### 7.1 ContextVar

- `TEN-001`：绑定后当前协程可读；
- `TEN-002`：正常退出恢复旧值；
- `TEN-003`：异常退出仍恢复；
- `TEN-004`：并发协程 OrgA / OrgB 不串；
- `TEN-005`：线程池不继承陈旧上下文；
- `TEN-006`：测试用例结束无残留；
- `TEN-007`：缺失上下文抛 `tenant_context_missing`；
- `TEN-008`：不回退默认 Org。
- `TEN-009`：数据库连接复用时清理 tenant session state，OrgA 查询不能继承 OrgB 的 RLS / Session 上下文。

### 7.2 入口

- HTTP Session / OIDC；
- API Key；
- RunEnvelope；
- Scheduler；
- IM ChannelBinding；
- GitHub / Webhook；
- system job fan-out。

每个入口验证成功、无映射、主体禁用、Org suspended、Workspace 不匹配和伪造 Org。

### 7.3 RunEnvelope

- 同库读取允许无 `integrity`；
- 跨信任边界消息缺少或错误签名被拒绝；
- `tenant.org_id` 与 Run / Thread / ReleaseRef 不一致被拒绝；
- 重复 idempotency key 不创建第二 Run；
- 未知 schema version 安全失败；
- Envelope 不包含 Secret。

---

## 8. 架构边界测试

必须建立或继承 `test_harness_boundary`：

- 扫描 `packages/harness/deerflow` import graph；
- 禁止导入 `app.control_plane`、app ORM、Router 和 Service；
- `deerflow.contracts` 禁止导入 app、FastAPI 和 ORM；
- app adapters 可以实现 contracts Protocol；
- 检查动态 import、字符串 import 和 TYPE_CHECKING 旁路；
- 发现违规给出依赖路径。

边界测试为每 PR 阻断项。临时忽略必须修改 ADR-0001，不允许只加 test skip。

---

## 9. RBAC 与身份测试

### 9.1 权限矩阵

以下矩阵是[ADR-0003](../adr/0003-rbac-and-service-accounts.md)的可执行验收映射，不得使用“待定”放行：

| 能力 | Admin | Developer | Viewer | ServiceAccount |
| --- | --- | --- | --- | --- |
| 读取 Thread / Run | 允许 | 允许 | 允许 | 按 scope |
| 创建 Run | 允许 | 允许 | 拒绝 | 按 scope |
| Cancel / Resume | 允许 | 允许 | 拒绝 | 按 scope |
| Console | 允许 | 拒绝 | 拒绝 | 默认拒绝 |
| Membership / Role | 允许 | 拒绝 | 拒绝 | 默认拒绝 |
| Agent Draft | 允许 | 允许 | 只读 | 按 scope |
| dev Promote | 允许 | 允许 | 拒绝 | 按 scope |
| prod Promote / Rollback | 允许 | 拒绝 | 拒绝 | 默认拒绝 |
| Audit 查询 | 允许 | 默认拒绝 | 默认拒绝 | 默认拒绝 |

任何角色权限放宽都需要安全与产品共同签收，并同步更新 ADR-0003、API 边界和对应负向测试。

### 9.2 状态

- invited Membership 不能绑定 TenantContext，资源访问为 404；
- suspended Membership 返回 403 `permission_denied`；removed Membership 不再形成活动 Org 范围，访问返回 404；
- disabled User / ServiceAccount 返回 `principal_disabled`；
- expired / revoked API Key 拒绝；
- 角色删除、Membership 撤销和主体禁用后，相关 Session / 权限缓存在 60 秒内失效；
- 最后一个 org:admin 删除需要受控规则；
- system:org:create 仅 system-admin；
- OIDC group 不在 allowlist 时不自动提权。

### 9.3 API Key

- 明文只返回一次；
- 数据库不存在明文；
- 相同 Key 哈希验证成功；
- 错误 Key 使用恒定时间比较或库安全实现；
- scope 限制生效；
- Org 不能覆盖；
- scopes 必填且非空，且只能收窄 ServiceAccount 权限；
- 轮换重叠期和撤销在 60 秒内生效；
- 创建、轮换、撤销有 AuditEvent；
- Key 不出现在日志、Trace 和错误。

---

## 10. API 契约测试

### 10.1 通用

- 请求 / 响应符合 OpenAPI；
- 所有错误符合统一 envelope 并含 request_id；
- 错误不含堆栈、SQL、内部路径、Policy 全文；
- 未认证 401；
- 跨 Org 404；
- 当前 Org 权限不足 403；
- suspended Org 新 Run / 发布返回 403 + `org_suspended`；
- deleting Org 新 Run、发布和普通写入返回 403 + `org_deleting`；
- prod 未固定 ReleaseRef 的 admit / resume / continue 返回 409 + `release_unpinned`；
- validation 422；
- rate limit 429 + `Retry-After`；
- 破坏性 OpenAPI diff 阻断 PR。

### 10.2 幂等与并发

- 同 Key + 同请求返回原结果；
- 同 Key + 不同请求返回 409 `idempotency_conflict`；
- Run 创建不重复；
- API Key 创建重放不再次返回明文；
- Release promote / rollback 使用 If-Match / row version；
- Release 并发更新只有一个成功，其余 409 `release_conflict`；
- cancel / resume 重试安全。

### 10.3 分页

- Cursor 不可篡改；
- 翻页无重复 / 遗漏；
- 同名跨 Org 资源不串；
- 删除 / 新增并发下行为符合约定；
- 最大 limit 生效；
- 时间范围和过滤字段 allowlist 生效。

### 10.4 Compatibility

- 上游关键 API 客户端路径保持可用；
- 兼容路由仍经过统一 Auth / Tenant / RBAC；
- 不支持的行为返回明确错误；
- 旧路径标记 deprecated；
- compatibility 不能绕过 prod ReleaseRef 和 Audit。

---

## 11. Runtime 契约测试

为以下 DTO 保存 canonical JSON fixture：

- PrincipalRef；
- TenantContext；
- RunEnvelope；
- PolicySnapshotRef；
- EnvelopeIntegrity；
- PolicyRequest / PolicyDecision / PolicyObligation；
- ReleaseRef；
- ApprovalTicket；
- AuditEvent；
- UsageRecord；
- ContractError。

每个 fixture 验证：

- 序列化 / 反序列化；
- 未知可选字段兼容；
- 缺少必填字段失败；
- 未知高风险 obligation 安全拒绝；
- 时间、UUID、digest 格式；
- 不变量和 Org 匹配；
- schema version；
- 敏感字段不被接受或输出。

生产者与消费者契约测试必须在同一 PR 运行。`v1alpha1` 字段改变时 fixture 与文档同 PR 更新。

---

## 12. Policy 与审批预留

### 12.1 Policy

- Run admission 记录 policy_version；
- 普通装载使用 Run snapshot；
- 高风险工具每次实时评估；
- 调用方不能降低 risk_class；
- timeout / unavailable fail-closed；
- deny 不执行工具；
- deny 产生 AuditEvent；
- obligations 参数按 Schema；
- redact / limit 实际执行；
- 未知 obligation 拒绝。

### 12.2 require_approval

- 未启用审批适配器时安全中断；
- 普通 resume 不能绕过 approval_required；
- ApprovalTicket 不等于 ask_clarification；
- resume_token_ref 不暴露可复用明文 Token；
- approved / rejected / expired 状态后续实现时保持幂等；
- MVP 不因缺 UI 把 require_approval 当 allow。

---

## 13. Agent 制品与发布

### 13.1 不可变性

- 相同 Package 可创建 v1 / v2；
- published Version 内容、manifest 和 digest 不可修改；
- 修改内容必须创建新 Version；
- digest 与实际内容匹配；
- 缺失 / 篡改对象拒绝执行；
- revoked Version 不能创建新 Run；
- 历史 Run 保留已撤销 digest 引用；
- `legacy_unpinned=true` 的 Run 在 prod 只能读取、取消和归档，不能 admit、resume 或继续执行。
- 对上述拒绝路径断言 HTTP 409 + `release_unpinned`，不能只检查“执行失败”。

### 13.2 通道

关键 E2E：

```text
发布 demo v1 到 prod
→ 新 Run 固定 v1 digest
→ 晋升 v2
→ 新 Run 固定 v2 digest
→ v1 在途 Run 继续 v1
→ 回滚 prod 到 v1
→ 下一 Run 固定 v1
→ 历史 Run 引用均不变化
```

并验证：

- prod 拒绝 draft / reviewed 但未 published / revoked；
- Workspace 与 Org 匹配；
- CAS 防止并发晋升丢失更新；
- ReleaseEvent 与 AuditEvent 同时产生；
- 回滚理由和主体完整；
- 文件态 Agent 只可作为导入源，不能被 prod 直接读取；
- Catalog 缓存错误不改变执行权威源。

### 13.3 制品安全

- 路径穿越；
- 符号链接逃逸；
- 超大制品；
- digest 冲突 / 错误；
- Manifest Schema；
- 未锁定依赖；
- 危险二进制和脚本扫描；
- 对象存储签名 URL 越权。

---

## 14. Audit 与 Usage

### 14.1 AuditEvent

必须事件：

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

每个事件测试：

- 必填字段；
- Org / Actor / Resource / Outcome；
- request / trace / run 关联；
- event id 和 idempotency；
- payload 白名单；
- 无 Secret、Token、完整 Prompt；
- OrgA 查询不返回 OrgB；
- 普通应用账号不能 UPDATE / DELETE；
- correction 使用新事件；
- 高风险事务与 outbox 一致。

上述 MVP 必报事件不允许豁免。其他关键管理写路径覆盖率必须 100%；确需临时豁免时必须具名、最长 14 天，且不得涉及跨 Org、明文 Secret、未审计 prod 发布或权限变更。

### 14.2 Audit 故障

- Sink 不可用时高风险管理写回滚或 outbox 成功；
- outbox 重试幂等；
- 积压超过阈值告警；
- 恢复后不重复领域操作；
- 磁盘 / 队列满时不静默丢弃。

### 14.3 UsageRecord

- token 来自模型适配器；
- Org / Run / Release digest 正确；
- attempt 区分供应商重试；
- idempotency 防重复；
- cost 缺失不丢 token；
- failed / cancelled 状态正确；
- Console 汇总不跨 Org、不重复计费。

---

## 15. 数据库迁移测试

### 15.1 每个 Alembic Revision

- 空数据库 upgrade；
- 前一受支持版本 upgrade；
- schema 与 ORM 一致；
- downgrade（如声明支持）；
- 不支持 downgrade 时有明确阻断；
- 事务与锁行为；
- 失败重跑；
- 数据校验；
- 应用 N / N+1 兼容窗口。

### 15.2 默认 Org 迁移

按 ADR-0002 验证：

1. 创建默认 Org；
2. 回填 Membership；
3. 回填 Thread / Run；
4. 回填 Checkpoint / Memory / Artifact；
5. 回填 Agent / Skill / MCP；
6. 回填 Scheduler / Channel；
7. 回填 Usage / Audit；
8. 启用租户过滤验证模式；
9. 创建不对外开放的验证 Org；
10. 执行完整隔离矩阵；
11. enforce 非空、外键和复合唯一约束；
12. 开启多组织 Feature；
13. 稳定观察后执行 contract。

校验：

- 迁移前后每表行数；
- orphan 数为 0；
- 无空 `org_id`（系统白名单除外）；
- 无错误跨 Org 复合外键；
- Redis / Checkpoint namespace 正确；
- 重复运行不重复创建；
- contract 后旧应用被阻止。

### 15.3 规模

Staging 使用接近生产的数据量分布，测量：

- DDL lock；
- 回填吞吐；
- 事务日志增长；
- 索引创建时间；
- 应用延迟；
- 回滚点。

只用几十行 fixture 不能作为生产迁移验收。

---

## 16. Run 生命周期与多副本

### 16.1 状态机

- Run 状态和合法转换以[数据模型 §12.3](../architecture/data-model.md#123-run)为权威；
- `clarification_required` 与 `approval_required` 分别测试，不能相互替代；
- `cancelling` 只能进入 `cancelled` 或 `failed`；
- 合法转换成功；
- terminal 不回到 running；
- cancel 幂等；
- resume 只允许可恢复中断；
- retry 不重复创建 Run；
- reconcile 使用 CAS；
- 超时、失败、取消有明确终态；
- Run 始终固定 TenantContext、ReleaseRef、policy_version。

### 16.2 双副本故障注入

按生产 Runbook §12：

- 两实例同时 claim；
- owner 在不同执行点崩溃；
- Redis 延迟、断连、重启；
- PostgreSQL 慢 / 短暂不可用；
- cancel 与 complete 竞争；
- 滚动重启；
- SSE 跨实例重连；
- lease 过期和时钟偏差；
- reconcile 重复；
- Sandbox 外部副作用后崩溃。

出口：

- 只有一个有效 owner；
- terminal 最终一致；
- 可达 Owner 的 cancel propagation ≤60 秒；Owner 失联时按 lease 过期与 reconcile 路径验收；
- 5 分钟内非终态 Run 有明确结果；
- 僵尸 Run <0.1%；
- 不可证明幂等的外部写不自动重放；
- 未通过则配置锁定单副本。

### 16.3 Profile W 物理 Worker

启用前按 ADR-0006 额外验证：

- Run 与 dispatch outbox 原子间隙可恢复；
- 跨信任边界 RunEnvelope 完整性；
- 消息重复、乱序、延迟和 poison message；
- Worker 身份无控制面管理权限；
- Gateway cancel intent 在通知丢失时仍可见；
- Redis 全丢后以 PostgreSQL 为权威 reconcile；
- Worker crash 不自动重放不可证明幂等的外部写；
- Shadow、受控 Org、默认切换和回滚；
- 24 小时目标负载 Soak。

---

## 17. SSE 测试

- 首事件延迟；
- keepalive 不计业务首事件；
- Last-Event-ID 重连；
- 事件顺序和去重；
- terminal event；
- 客户端断开不自动 cancel；
- OrgA Token 不能订阅 OrgB Run；
- 连接期间 Membership / Role / ServiceAccount / API Key 撤销后，SSE 在 60 秒内且发送下一业务事件前关闭；
- Redis 重启后的恢复；
- 滚动升级跨实例恢复；
- 慢客户端背压；
- 单连接和每 Org 连接上限；
- 错误不泄露内部数据。

---

## 18. Sandbox、SSRF 与扩展安全

### 18.1 Sandbox

- 生产配置拒绝 host bash；
- 进程非 root；
- 只读根文件系统；
- CPU、内存、PID、磁盘、时间限制；
- 不挂载 Docker socket、Kubernetes 管理凭证和宿主敏感目录；
- OrgA 写目录不被 OrgB 复用；
- 超时 / OOM 回收；
- 回收失败进入 quarantine；
- Secret 最小注入并在结束后失效；
- 无默认外网；
- 基础镜像固定 digest。

### 18.2 SSRF

测试：

- loopback IPv4 / IPv6；
- RFC1918；
- link-local；
- 云 metadata；
- Kubernetes service / pod CIDR；
- 十进制、八进制、十六进制 IP 表达；
- IPv4-mapped IPv6；
- 用户名 / 密码混淆 URL；
- 重定向到私网；
- DNS rebinding；
- 超长 DNS / Unicode host；
- 非允许端口和协议；
- 超大响应、慢响应和无限重定向。

每次重定向和最终连接 IP 均重新验证。

### 18.3 Webhook / MCP

- 正确 / 错误签名；
- 过期时间戳；
- 重放 event id；
- 未映射外部 Workspace；
- Body 大小限制；
- MCP 返回恶意文本不获得额外权限；
- MCP Secret 不进入日志；
- Tool 参数 Schema 和风险等级不可被调用方覆盖。

---

## 19. E2E 验收旅程

### 19.1 两组织

1. 创建 OrgA / OrgB；
2. 创建 Admin / Developer / Viewer；
3. 各自创建 Thread / Run / Agent；
4. 用 OrgA 资源 ID 从 OrgB 访问；
5. 验证详情、列表、统计、SSE、Artifact 均不可见；
6. 验证拒绝与探测日志。

### 19.2 权限与审计

1. Viewer 创建 Run 被拒；
2. Admin 授予 Developer 权限；
3. Developer 创建 Run；
4. 高风险 Tool 被 Policy deny；
5. Admin 查询 Audit；
6. OrgB Admin 不可见 OrgA Audit。

### 19.3 发布回滚

执行 v1 → v2 → rollback 的完整流程，验证新旧 Run digest、ReleaseEvent、AuditEvent 和 Catalog。

### 19.4 生产运维

1. 执行 doctor；
2. 执行迁移；
3. 创建 Run；
4. 滚动升级或单副本维护；
5. 恢复 SSE / Run；
6. 执行备份恢复；
7. 重新验证 Org 隔离与 ReleaseRef。

---

## 20. 性能测试

### 20.1 SLO 验证

至少测量：

- Gateway request rate / error / P50 / P95 / P99；
- Run create 到 running；
- SSE 首事件；
- Console stats / runs / usage；
- Policy evaluate；
- Release resolve；
- PostgreSQL 查询与连接池；
- Redis Stream lag；
- Sandbox acquire / cold start；
- Audit outbox lag。

### 20.2 负载模型

初始场景：

- 每日 100 / 1000 Run；
- 并发 10 / 50 / 100 Run；
- 多 Org 公平性；
- 100 / 1000 SSE 长连接；
- 长 Thread / 大 Checkpoint；
- 高频短 Tool 与低频长 Sandbox；
- Audit / Usage 持续写入；
- 单 Org 突发但不拖垮其他 Org。

具体承诺以实测为准。未达到 SLO 时记录瓶颈、容量上限和扩容触发点。

### 20.3 Soak

Release Candidate 至少进行 8 小时稳定性测试；进入 HA 声明前至少进行 24 小时：

- 内存 / 连接泄漏；
- Redis Stream 增长；
- Sandbox 回收；
- ContextVar 污染；
- Audit outbox 积压；
- Checkpoint / Artifact 增长；
- 僵尸 Run；
- 成本归因漂移。

---

## 21. 恢复与灾难演练

至少覆盖：

- PostgreSQL 从备份恢复；
- Redis 空实例重建 ownership；
- 对象存储版本恢复；
- Secret 引用验证；
- Gateway 单副本启动；
- Run reconcile；
- Org 隔离、RBAC、Audit、ReleaseRef 回归；
- Audit 归档水位与对象摘要抽样验证；
- prod 门禁启用前确认可执行范围内 `legacy_unpinned` Run 计数为 0；保留记录只能读取、取消和归档；
- 从旧备份恢复后重放[数据治理 §9](../compliance/data-governance.md#9-备份中的删除)的 deletion ledger / tombstone，并证明已删除 Org、主体和内容不可见；
- 记录声明的 RPO（每日备份 24h 或 PITR 15min）、实际数据损失与 RTO。

恢复测试失败是生产发布阻断项，除非已有未到期的具名豁免且不影响安全隔离。

---

## 22. CI 流水线

### 22.1 PR 必跑

```text
format / lint
→ type check
→ import boundary
→ unit
→ component with PostgreSQL
→ TEN-009 connection-pool tenant reuse
→ contract / OpenAPI diff
→ tenant isolation core
→ migration upgrade
→ SAST / full dependency / secret scan
→ build smoke
```

涉及 Redis、SSE、Run ownership、Release、Audit、Sandbox 的 PR 增加对应 Integration Suite。

### 22.2 Nightly

- 完整隔离矩阵；
- PostgreSQL + Redis Integration；
- E2E；
- SSRF / Webhook 安全；
- 双副本故障注入；
- 性能趋势；
- 较长 soak；
- 上游兼容回归。

### 22.3 Release Gate

- 全 PR 套件；
- 完整 E2E；
- 迁移与回滚兼容；
- prod 配置真实 doctor；Fork 前 staging stub 只用于流水线开发，不构成生产验收；
- 镜像扫描、SBOM、签名验证；
- 关键 SLO smoke；
- v1 → v2 → rollback；
- 备份最近成功且在 RPO 内；
- 已知豁免未过期。
- 所有 Release Candidate 至少完成 8 小时 Soak；
- 当生产目标 `replicas>1` 时，§16.2 Profile H 故障注入与 24 小时 HA soak 为阻断项；否则发布配置必须锁定单副本并声明非 HA。
- 当生产目标为 Profile W 时，§16.3、ADR-0006 前置条件、远程 Worker 回滚演练和 24 小时 Soak 为阻断项。

### 22.4 Quarterly / Major Change

- 完整恢复演练；
- Sandbox 逃逸与网络策略复核；
- 上游同步演练；
- 24 小时 HA soak；
- 权限和审计访问复核；
- Threat Model 更新。

---

## 23. 覆盖率与质量门禁

覆盖率是补充指标，不替代关键测试：

- 变更行覆盖率建议 ≥85%；
- TenantContext、授权、Release、Audit、Run 状态机核心包分支覆盖建议 ≥90%；
- 项目整体行覆盖 MVP 目标 ≥80%，上游基线不足时分阶段提高并记录差距；
- 所有 P0 安全与隔离需求必须有明确测试 ID，不因覆盖率达标而豁免；
- Mutation Testing 可用于授权与状态机关键规则，MVP 非硬门槛。

禁止：

- 为提高覆盖率测试实现细节而不测试行为；
- 删除负向断言；
- 用 `skip` 绕过阻断级失败；
- 把真实依赖全部 Mock 后宣称完成集成测试。

---

## 24. Flaky 测试治理

- 同一测试在主分支 20 次内出现 2 次非确定失败即标记 Flaky；
- 立即创建 Owner、原因和修复期限；
- 非阻断低风险测试可以临时 quarantine，默认最长 7 天；
- 隔离、授权、迁移、发布、审计和恢复测试不允许 quarantine；
- 修复需证明重复运行稳定；
- CI 记录 flaky rate 和重试次数；
- 禁止用无限重试把红灯变绿。

---

## 25. 测试数据与隐私

- 默认使用合成数据；
- 生产数据进入测试需批准、脱敏、限时和审计；
- 测试日志不含真实 Prompt、PII、Token 和 Secret；
- 测试 Bucket / DB / Redis 与生产隔离；
- 清理失败产生告警；
- 安全测试 payload 不能攻击外部未授权目标；
- 性能测试外部模型调用使用受控 stub 或明确预算账号。

---

## 26. PR Definition of Done

每个 PR 必须说明：

- 关联 ADR / 需求；
- 新增或变化的 API / DTO / Schema；
- Tenant / Workspace 影响；
- Authn / Authz / Audit 影响；
- 数据迁移与回滚；
- 幂等和并发；
- 日志、指标、告警；
- 测试 ID 与结果；
- 是否需要 Feature Flag；
- 是否影响上游同步。

涉及租户资源的新代码必须回答：“OrgA 如何证明看不到 OrgB？”

---

## 27. 90 天测试出口

- [ ] harness boundary 每 PR 必过
- [ ] TenantContext 并发与清理测试通过
- [ ] `TEN-009` 数据库连接池复用不串 Org
- [ ] 12 类核心资源隔离矩阵通过
- [ ] RBAC 正反向矩阵通过
- [ ] API Key 创建、轮换、撤销和脱敏通过
- [ ] OpenAPI 与 Runtime contracts 契约测试通过
- [ ] 默认 Org migration dry-run 通过
- [ ] v1 → v2 → rollback E2E 通过
- [ ] prod 非 ReleaseRef 执行测试通过
- [ ] prod 拒绝 admit / resume / continue `legacy_unpinned` Run
- [ ] 关键 AuditEvent 覆盖率 100% 或有具名豁免
- [ ] Run cancel / lease / reconcile 符合所声明的单 / 双副本模式
- [ ] SSRF、Sandbox 和 Secret 基线测试通过
- [ ] 备份恢复演练满足 RPO / RTO
- [ ] Release Gate 保存可追踪测试证据

未满足隔离、ReleaseRef、强审计或恢复四类出口之一，不能把 MVP 标记为生产可用。
