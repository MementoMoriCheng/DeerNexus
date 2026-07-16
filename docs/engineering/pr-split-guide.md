# DeerNexus 大改造 PR 拆分指南

> 状态：MVP 工程指南 v0.1  
> 适用范围：DeerFlow Fork 初始化、多租户、RBAC、Audit、Agent Release、Admin UI、运行控制与生产基线  
> 关联：[ADR-0001](../adr/0001-fork-evolution-strategy.md) · [90 天 MVP](../roadmap/90-day-mvp.md) · [实施进度](progress.md) · [测试策略](testing-strategy.md) · [CI/CD](ci-cd.md) · [上游同步](upstream-sync.md)

本文把 90 天大改造拆成可审查、可测试、可回滚的 PR。目标不是追求最少文件，而是让每个 PR 只有一个清晰架构意图，并避免在同一变更中同时引入 Schema、授权、业务切流和 UI。

---

## 1. 原则

1. **契约先于实现**：DTO、错误码、权限和状态机先冻结；
2. **Expand 先于 Enforce**：先兼容 Schema，再回填、验证和收紧；
3. **安全门禁随第一条路径进入**：不等功能完成后补 Tenant / RBAC / Audit；
4. **一个 PR 一个主要风险**：租户迁移、权限切换、发布权威源不混在一起；
5. **可独立回滚**：PR 描述必须说明回滚不会破坏哪些数据；
6. **行为可观察**：新路径同时交付日志、指标或验证证据；
7. **不隐藏依赖**：Stacked PR 显式标注 parent、merge order 和临时兼容；
8. **上游区最小改动**：harness 只加 contracts / hooks，不混入控制面业务；
9. **不做顺手重构**：目录整理、格式化和业务改造分开；
10. **文档与代码同 PR**：实现改变已接受语义时同步 ADR / 规格。

---

## 2. PR 大小

### 2.1 建议

- Reviewer 可在 30–60 分钟理解主要行为；
- 主要意图不超过一个；
- 数据迁移与业务切流拆开；
- 机械生成文件单独标记，不用行数判断复杂度；
- 测试与实现一起提交；
- 大型依赖锁变更不与业务功能混合。

### 2.2 必须拆分

出现以下任一：

- 同时修改 TenantContext、RBAC 和 Release；
- 同时新增表、回填生产数据并 enforce 非空；
- 同时改变 Run 状态机与 Worker 拓扑；
- 同时修改上游大段代码和企业控制面；
- 同时引入新外部依赖、Secret 和公网端点；
- 同时完成 Backend、Frontend、Migration 和部署切流；
- Reviewer 无法描述单一失败回滚路径。

### 2.3 可以合并

- 同一个 DTO 与其序列化 fixture；
- 同一端点的 Router、Service、Repository 和测试，前提是无大迁移；
- 同一 Audit action 的 outbox 接入与测试；
- 小型文档、配置 Schema 和对应 doctor check；
- 纯机械 rename，但不能夹带行为变化。

---

## 3. PR 描述模板

```text
## Why

## Scope
- included
- explicitly excluded

## Architecture
- linked ADR / contract
- dependency direction

## Tenant / Security
- org scope
- authn / authz
- audit
- secret / network / sandbox

## Data
- schema
- migration phase
- compatibility
- backfill / validation

## Runtime
- idempotency
- concurrency
- failure / retry

## Observability
- logs
- metrics
- alerts

## Verification
- test IDs
- commands / evidence

## Rollout
- feature flag
- order
- monitoring

## Rollback

## Follow-ups
```

涉及租户资源时必须回答：

> OrgA 如何证明看不到 OrgB？

涉及外部副作用时必须回答：

> 重试或 Worker 故障后，如何证明不会静默重复？

---

## 4. 依赖标记

Stacked PR 标头：

```text
Stack:
  parent: PR-xxx
  depends_on: [PR-xxx, migration-revision]
  blocks: [PR-yyy]
  merge_order: 3/8
  temporary_compatibility: ...
  cleanup_pr: PR-zzz
```

规则：

- Parent 合并后，子 PR 及时 rebase / merge main；
- 不把尚未合并的子 PR 当作 parent 的测试前提；
- 临时兼容代码必须有清理 PR；
- 每个 PR 合并后 main 仍可构建和部署；
- 跨多个 PR 的 Feature 默认关闭；
- 不使用长生命周期“mega branch”掩盖集成问题。

---

## 5. 推荐交付序列

以下编号表示逻辑顺序，不是预先创建的 GitHub PR 编号。

### Track 0：Fork 与基线

#### PR-001 导入上游基线

包含：

- 固定 DeerFlow tag / commit；
- 保留 License / NOTICE；
- 建立 origin / upstream 说明；
- 记录 `UPSTREAM_BASE`；
- 不做企业业务改造。

验收：

- 上游原生测试可运行；
- Backend / Frontend 可按上游方式启动；
- Commit 与依赖锁可追踪。

回滚：删除尚未发布的导入分支；不做生产迁移。

#### PR-002 基础 CI 与边界测试

包含：

- lint / type / upstream tests；
- `test_harness_boundary`；
- Secret scan；
- 最小构建；
- CODEOWNERS 初版。

不包含：Tenant / RBAC 实现。

#### PR-003 生产配置 Schema 与 Doctor 骨架

包含：

- 配置 Schema；
- PASS / WARN / FAIL 输出；
- PostgreSQL / Redis / OIDC / Sandbox 基础检查接口；
- 不宣称所有检查已实现。

---

## 6. Track A：Contracts 与 TenantContext

#### PR-010 `deerflow.contracts` 基础包

包含：

- PrincipalRef；
- TenantContext；
- ContractError；
- canonical JSON fixture；
- 无 app / ORM / FastAPI 依赖测试。

不包含：真实 OIDC / Membership 查询。

#### PR-011 Policy / Release / Event Contracts

包含：

- RunEnvelope、PolicySnapshotRef、EnvelopeIntegrity；
- PolicyRequest / Decision / Obligation；
- ReleaseRef；
- AuditEvent / UsageRecord；
- Protocol 与 fixtures。

拆成 PR-011A / B 的条件：实现或 Review Owner 不同。

#### PR-012 ContextVar 生命周期

包含：

- bind / get / require / reset；
- 正常、异常、并发协程、线程池、测试清理；
- `TEN-001`～`TEN-009` 中不依赖数据库的部分。

#### PR-013 Gateway Tenant 解析适配器

包含：

- 临时单 Org bootstrap 解析；
- OIDC / Session Principal 映射接口；
- 请求绑定和结构化日志；
- 客户端 org_id 不可信；
- Feature 默认单 Org。

不包含：第二 Org 对外开放。

#### PR-014 异步入口 Tenant 传播

建议按入口拆：

- PR-014A RunEnvelope / 内嵌 Worker；
- PR-014B Scheduler；
- PR-014C IM / Webhook / GitHub。

每个 PR 都要有无映射 fail-closed 测试。

---

## 7. Track B：Schema 与迁移

#### PR-020 Control Plane Schema Expand

包含：

- organizations、workspaces；
- users、external_identities、memberships；
- roles、role_bindings；
- service_accounts、api_keys；
- Alembic expand；
- ORM 与约束测试。

不包含：存量资源 `org_id NOT NULL`。

如果 Review 过大，按 Tenant / IAM 拆 PR-020A / B，但必须保证每个 revision 可升级。

#### PR-021 存量资源 `org_id` Expand

包含：

- Thread / Run；
- Checkpoint / Memory / Artifact；
- Agent / Skill / MCP；
- Scheduler / Channel；
- 可空列和兼容索引。

建议按资源批次拆分，每批都有仓储和迁移测试。

#### PR-022 默认 Org Bootstrap

包含：

- 创建默认 Org；
- 初始 Membership / Admin；
- 安全 bootstrap；
- 幂等；
- Audit outbox 依赖尚未合并时使用明确临时事件接口，不静默丢失。

#### PR-023 Backfill Job

包含：

- 批次、水位、限速、重跑；
- 行数、孤儿、空 org 校验；
- dry-run；
- 不启用 multi-org。

#### PR-024 Repository Org Scope

按资源拆小：

```text
024A Thread / Run / Checkpoint
024B Memory / Artifact
024C Skill / MCP / Connector
024D Scheduler / Channel
024E Console / Usage / Audit
```

每个子 PR 进入受影响资源隔离矩阵。

#### PR-025 Enforce 与 Multi-org Feature

前置：

- 空 `org_id=0`；
- 双 Org 全矩阵绿；
- 新旧应用兼容；
- 生产规模 dry-run。

建议拆：

- PR-025A enforce constraints；
- PR-025B Feature Flag / 验证 Org；
- PR-025C 启用流程与 doctor；
- PR-025D 后续 contract 清理。

不得把 A–D 合成一次不可回滚发布。

---

## 8. Track C：RBAC 与机器身份

#### PR-030 权限注册表与内置角色

包含：

- 权限字符串；
- Admin / Developer / Viewer seed；
- system 权限隔离；
- 角色版本；
- 正反向单元测试。

#### PR-031 统一 Authorize Service

包含：

- Membership / Principal / RoleBinding；
- API Key scope 预留；
- 403 / 404；
- 缓存 key 和 TTL；
- Router 尚不全部切流。

#### PR-032 Runtime Router 切 RBAC

按 Thread / Run / Artifact 拆分。每个端点：

- permission；
- Tenant scope；
- 负向测试；
- compatibility 路由；
- 观测。

#### PR-033 Admin / Studio Router 切 RBAC

可以按 Admin 与 Studio 两个 PR：

- Admin IAM / Console / Audit；
- Studio Package / Release / Catalog。

#### PR-034 ServiceAccount

包含生命周期、RoleBinding 和限流主体，不包含 API Key 明文创建。

#### PR-035 API Key

包含：

- 生成、prefix、hash；
- 单次明文响应；
- scope 交集；
- 过期、轮换、撤销；
- Secret / 日志测试；
- Audit。

#### PR-036 OIDC Group Mapping

包含 allowlist、additive、dry-run、最后管理员保护和一个 IdP E2E。

#### PR-037 撤权与 SSE

包含主动失效、TTL≤60 秒、SSE 重验证和 P99 撤权证据。

---

## 9. Track D：Audit

#### PR-040 Audit Schema 与数据库权限

包含：

- audit_events、audit_outbox；
- append-only Role；
- action registry 基础；
- event / payload Schema；
- Migration。

#### PR-041 Audit Sink / Outbox Worker

包含 claim、retry、dead letter、idempotency、指标和告警接口。

#### PR-042 Class A IAM / Org

建议分：

- Membership / RoleBinding；
- ServiceAccount / API Key；
- Org status / system-admin。

每个业务事务与 outbox 同事务。

#### PR-043 Catalog / Connector / Release Audit

在对应领域服务就绪后接入，不在 Audit PR 内实现完整领域功能。

#### PR-044 Runtime Security Audit

包含 login、Policy deny、approval required、tenant mismatch、Sandbox violation。

#### PR-045 Audit Query / Archive

建议拆：

- Org 查询 / 分页；
- system-admin 查询；
- Archive Job / Manifest / 摘要；
- Retention / legal hold。

---

## 10. Track E：Agent 制品与 Release

#### PR-050 AgentPackage / AgentVersion Schema

包含状态机、digest、对象引用、不可变约束，不含 Channel。

#### PR-051 文件态导入

包含路径 / Manifest / digest / Secret 剥离和幂等导入。

#### PR-052 制品存储与对账

包含 inline threshold、对象上传、摘要、inventory、missing 告警。

#### PR-053 ReleaseChannel / ReleaseEvent

包含 Schema、`NULLS NOT DISTINCT`、row_version 和领域服务。

#### PR-054 Release Resolve

包含 `ReleaseResolver` Adapter、prod 门禁、Run pin 和跨 Org 测试。

#### PR-055 Promote / Rollback API

包含 CAS、If-Match、幂等、权限、Audit outbox。

#### PR-056 Legacy Run 门禁

包含：

- 标记 `legacy_unpinned`；
- prod 读取 / 取消 / 归档；
- 409 `release_unpinned`；
- 禁止 admit / resume / continue。

#### PR-057 Studio / Admin 最小 UI

后端 API 稳定后再做；不与 Schema / Release 原子性合并。

---

## 11. Track F：Console、观测与生产

#### PR-060 Org Console API

建议拆 stats / runs / usage，并使用生产规模查询测试。

#### PR-061 Admin Console UI

只做 Runs、Usage、Failure / Audit 入口；不扩审批、市场和 KB。

#### PR-062 结构化日志与 Trace

关联 ID、禁止字段、Run span、release digest、policy version。

#### PR-063 Metrics / Dashboard / Alerts

Gateway、Run、Redis、PG、Sandbox、Audit、Backup；链接 Runbook。

#### PR-064 Doctor 完整检查

把骨架接入真实配置与依赖，生产 FAIL 条件进入发布门禁。

#### PR-065 Backup / Restore Automation

备份 Job、恢复脚本、校验、证据；不与数据库业务迁移混合。

#### PR-066 CI/CD Release Gate

真实命令、Workflow、SBOM、扫描、环境保护和发布证据。

---

## 12. Track G：Run 控制与 Worker

#### PR-070 Run 状态 CAS

先冻结 terminal、cancel、resume、reconcile 语义。

#### PR-071 Ownership / Lease

包含 Redis Key、lease token、heartbeat 和原子 claim。

#### PR-072 Reconciler

包含过期 owner、非终态 Run、指标和人工处理。

#### PR-073 SSE 跨副本恢复

包含 StreamBridge、Last-Event-ID、撤权和慢客户端。

#### PR-074 Profile H 门禁

Doctor、部署配置、故障注入和 24 小时 Soak；失败保持 Profile S。

#### PR-075 Dispatcher / Executor Protocol

只做同进程接口化，不立即远程 Worker。

#### PR-076+ 物理 Worker

只有 ADR-0006 触发和前置条件满足后创建独立计划：

- Dispatch outbox / queue；
- Worker identity；
- remote claim；
- cancel；
- observability；
- shadow；
- controlled rollout；
- cleanup。

---

## 13. 并行工作流

可以并行：

- Contracts 与 CI 骨架；
- Security / Runbook / observability 配置映射；
- Admin UI 设计与稳定 API Mock；
- Audit 基础设施与 Agent Schema；
- Test fixtures 与对应实现。

不能无协调并行：

- TenantContext 字段与多个入口各自实现；
- 同一表的多个 Alembic revision；
- 权限命名与 Router 手写判断；
- Run 状态机与 Reconciler；
- ReleaseChannel Schema 与 Promote API；
- Audit action 命名与各模块自由创建事件。

共享契约指定单一 Owner，其他 Track 使用 fixture 和适配器。

---

## 14. Migration PR 规则

每个迁移 PR 标明：

```text
phase: expand | backfill | enforce | contract
compatible_app_versions
estimated_rows
lock_risk
expected_duration
batch_size
throttle
validation_queries
retry_behavior
downgrade_supported
rollback_or_forward_fix
backup_requirement
```

禁止：

- 在同一 revision 大表加非空列并全量回填；
- 依赖应用启动自动执行长迁移；
- 用 ORM 默认值掩盖存量空值；
- 未验证索引锁就进入生产；
- contract 后仍允许旧应用连接；
- 失败后手改 revision。

---

## 15. Feature Flag 规则

每个 Flag：

```text
name
owner
default
scope
dependencies
enable_criteria
rollback_behavior
observability
expires_at
cleanup_pr
```

关键 Flag：

- multi-org；
- RBAC authoritative；
- audit enforcement；
- prod ReleaseRef enforcement；
- Profile H；
- remote Worker。

Flag 不能让安全不变量变为客户端可选。启用顺序进入 Runbook。

---

## 16. 测试拆分

实现 PR 同时交付：

- Unit / Component；
- 受影响 Contract fixture；
- 正常、未认证、权限不足、跨 Org；
- 幂等 / 并发；
- 日志 / Audit；
- Migration；
- Rollback 或 Feature disable。

Nightly / Release 的长测试可以在后续专用 PR 接入，但实现 PR 必须：

- 提供 test marker / fixture；
- 在 PR 描述列出后续 Gate；
- 不在长测试尚未存在时启用生产 Feature。

---

## 17. Review 分工

| 改动 | 必需 Review |
| --- | --- |
| harness / contracts | Runtime Owner |
| Tenant / IAM | Control Plane + Security |
| Migration | Backend + DBA |
| Release / Artifact | Studio + Runtime + Security |
| Audit | Control Plane + Security |
| Sandbox / Network / Secret | Platform Security |
| Run ownership / Worker | Runtime + SRE |
| CI / Image / SBOM | Platform Engineering |
| Frontend Admin | Frontend + API Owner |
| Data retention / deletion | Data Owner + Security / Privacy |

Review 不是简单人数门槛；对应 Owner 必须实际检查其风险领域。

---

## 18. 合并前检查

- [ ] 单一意图清楚
- [ ] 范围和非范围明确
- [ ] ADR / 契约已引用
- [ ] 上游区域改动最小
- [ ] Org / Workspace 语义明确
- [ ] Authn / Authz / Audit 明确
- [ ] Secret / Network / Sandbox 影响明确
- [ ] Schema 阶段与兼容窗口明确
- [ ] 幂等、并发、失败语义明确
- [ ] 日志、指标和告警明确
- [ ] 测试 ID 与证据完整
- [ ] Feature 默认安全
- [ ] Rollout / Rollback 可执行
- [ ] 临时代码有 cleanup PR
- [ ] 文档与代码同 PR 更新

---

## 19. 反模式

- “实现多租户”单个 PR；
- 大量格式化掩盖安全逻辑；
- Schema、Backfill、Feature ON 同时合并；
- 在 Router 中临时写角色名；
- 先全局按 ID 查，再应用层比较 Org；
- 用 TODO 代替 Audit；
- 用当前磁盘 Agent 让 Release 测试先过；
- 为减少 PR 数量复用不兼容 DTO；
- Boundary 测试失败后加 skip；
- 上游同步 PR 混入产品功能；
- UI 隐藏按钮代替后端权限；
- 未记录依赖的 Stacked PR；
- 合并后 main 只有配合其他未合并 PR 才能运行。

---

## 20. 里程碑完成定义

### Phase A

- PR-001～014 的适用项完成；
- Schema Expand 和默认 Org 路径可开始；
- Boundary、Contracts、TenantContext 进入 CI；
- 生产配置和 Doctor 有真实映射。

### Phase B

- Tenant Schema、Backfill、Repository Scope；
- RBAC、ServiceAccount、API Key；
- Audit Class A / B；
- Org Console API；
- 双 Org 已实现资源矩阵全绿。

### Phase C

- AgentPackage / Version / Release；
- prod ReleaseRef；
- Console UI；
- v1 → v2 → rollback；
- 备份恢复、发布证据和最终隔离矩阵。

---

## 21. Fork 后校准

导入上游代码后，必须把本文逻辑 PR 映射为：

```text
actual paths
actual packages
actual migration revisions
actual test markers
actual workflow jobs
actual CODEOWNERS
actual feature flags
actual upstream conflicts
```

映射可以调整 PR 数量，但不能破坏“契约先行、Expand/Enforce 分离、安全门禁随路径进入”的原则。
