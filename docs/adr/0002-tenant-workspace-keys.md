# ADR-0002：Tenant / Workspace 主键与资源归属

- **状态**：Accepted
- **日期**：2026-07-15
- **决策者**：DeerNexus 项目组
- **关联**：[ADR-0001](0001-fork-evolution-strategy.md) · [目标架构](../architecture/target-architecture.md) · [运行时契约](../architecture/runtime-contracts.md) · [数据模型](../architecture/data-model.md)

---

## 1. 背景

DeerFlow 现有隔离以 `user_id` 为主。DeerNexus 要升级为企业多租户平台，必须先冻结组织、Workspace、用户、外部渠道与运行资源的主键语义，否则后续为 thread、run、checkpoint、agent、skill、MCP、调度任务和审计补 `org_id` 时会反复迁移。

需要解决：

1. 哪个实体是硬隔离与默认计费边界；
2. Workspace 在 MVP 中是否参与授权；
3. 哪些资源必须带 `org_id`，哪些可以是系统全局资源；
4. HTTP、Worker、Scheduler、IM 等入口如何获得统一租户上下文；
5. 存量 DeerFlow 数据如何迁入默认组织。

---

## 2. 决策

### 2.1 Organization 是硬隔离边界

`Organization`（简称 Org，也称 Tenant）是 DeerNexus 的最高租户隔离单元：

- 所有租户拥有的资源必须有非空 `org_id`；
- 授权、查询、缓存、审计、用量和发布均以 `org_id` 为首要范围；
- 不允许通过客户端请求体直接覆盖认证过程解析出的 `org_id`；
- 跨 Org 访问默认不可见，资源读取返回 `404`；已进入目标 Org 权限上下文但缺少操作权限时返回 `403`；
- `slug`、组织名称和外部 IdP 标识均不是授权主键。

### 2.2 主键格式

- `organizations.id`、`workspaces.id` 及新增控制面实体主键使用 PostgreSQL `uuid`；
- 初始实现使用无业务语义的 UUIDv4，由应用或数据库生成；
- 代码不得依赖 UUID 的时间顺序；未来切换 UUIDv7 不改变对外契约；
- 外部 API 把 UUID 当作不透明字符串；
- 可读名称使用单独的 `slug`，唯一性范围由数据模型明确。

### 2.3 Workspace 在 MVP 中是可选资源分组

MVP 中 `Workspace` 是 Org 内可选的项目命名空间：

- `workspace_id` 可以为空，空值表示 Org 默认范围；
- Workspace 可用于资源筛选、发布通道绑定和未来配额归集；
- **MVP 不交付 Workspace 级角色继承或独立授权**，权限仍由 Org 级 RoleBinding 决定；
- 任何 Workspace 必须属于且只能属于一个 Org；
- 同时携带 `org_id` 与 `workspace_id` 时，必须验证二者从属关系；
- 后续启用 Workspace 级授权须新增 ADR，不得静默改变现有角色语义。

### 2.4 外部 `workspace_id` 必须改名存储

Slack、飞书、Teams 等外部平台的 `workspace_id` 不得写入平台 `workspaces.id`：

- 外部字段保存为 `external_workspace_id`；
- 同时记录 `provider`、`external_tenant_id`、`external_channel_id`；
- 外部渠道绑定表负责把外部标识映射到平台 `org_id` 和可选 `workspace_id`；
- 未建立映射的外部事件 fail-closed，不得落入默认组织执行。

---

## 3. TenantContext

认证或可信任务装载完成后，入口必须构造不可变 `TenantContext`。**字段和序列化格式以[运行时契约 §5](../architecture/runtime-contracts.md#5-tenantcontext)为唯一来源**；本 ADR 只冻结租户语义，不维护第二份 DTO 定义。

约束：

1. `org_id` 对租户请求必填；
2. `principal_id` 是审计归因主体，不能用展示名称替代；
3. 只有 `principal_type=user` 时 `user_id` 才有意义；
4. `system` 主体只能执行显式列入系统权限清单的任务；
5. ContextVar 必须在请求或任务结束时恢复，防止线程池、协程和测试之间污染；
6. 异步任务不得只依赖进程内 ContextVar，必须把必要字段序列化到可信任务信封。

字段级定义、传播和错误语义见[运行时契约](../architecture/runtime-contracts.md)。

---

## 4. 资源归属矩阵

| 资源 | `org_id` | `workspace_id` | 归属规则 |
| --- | --- | --- | --- |
| Organization | 主键自身 | 不适用 | 顶级隔离实体 |
| Workspace | 必填 | 主键自身 | 必须属于一个 Org |
| Membership / RoleBinding | 必填 | MVP 不使用 | Org 级授权 |
| Thread / ThreadMeta | 必填 | 可选 | 创建时固定，不允许跨 Org 移动 |
| Run | 必填 | 可选 | 从 Thread 或创建请求的 TenantContext 继承 |
| Checkpoint / Store | 必填或编码进命名空间 | 可选 | 必须与 Run/Thread 同 Org |
| Memory | 必填 | 可选 | 用户记忆仍受 Org 隔离，不等同企业 KB |
| Artifact / Workspace 文件 | 必填 | 可选 | 存储路径和对象键包含 Org 命名空间 |
| AgentPackage / AgentVersion | 必填 | 可选 | 版本属于 Org；系统内置包走显式白名单 |
| ReleaseChannel / ReleaseRef | 必填 | 可选 | 通道只能引用同 Org 制品 |
| Skill / MCP / Connector | 必填 | 可选 | 安装、凭证和配置按 Org 隔离 |
| ScheduledTask | 必填 | 可选 | 触发 Run 时恢复保存的租户上下文 |
| ChannelBinding | 必填 | 可选 | 外部标识映射到平台 Org/Workspace |
| API Key / ServiceAccount | 必填 | MVP 不使用 | 密钥不能跨 Org 授权 |
| UsageRecord | 必填 | 可选 | 从 Run 和模型调用继承 |
| AuditEvent | 必填；系统事件例外 | 可选 | 租户事件不可为空；系统事件使用独立作用域 |

### 4.1 系统全局资源例外

只有下列资源可以 `org_id IS NULL`：

- 平台内置且只读的系统角色模板；
- 平台内置模型或工具元数据；
- 无租户归属的平台运维事件。

例外必须：

1. 在数据模型的系统资源白名单中登记；
2. 不能包含租户凭证、用户内容或运行数据；
3. 由独立系统权限控制；
4. 不能因 `org_id IS NULL` 被普通租户查询自动命中。

禁止使用“空 `org_id` 表示默认组织”。

---

## 5. 上下文解析点

| 入口 | `org_id` 来源 | 要求 |
| --- | --- | --- |
| Gateway HTTP / SSE | Session、OIDC claims 与 Membership 解析结果 | 路由执行前完成；请求体仅能选择已授权 Org |
| API Key | Key 关联的 ServiceAccount | Key 自身固定 Org，不接受覆盖 |
| 内嵌 Run 执行 / Worker | 持久化 RunEnvelope | 同库读取时验证记录来源；跨信任边界队列传输时验证信封完整性并重建 TenantContext |
| Scheduler | ScheduledTask 记录 | 触发前验证任务与主体仍有效 |
| IM Channel | ChannelBinding | 未绑定或冲突时拒绝 |
| GitHub / Webhook Agent | 安装或 Webhook Binding | 校验签名后解析 Org |
| 后台维护任务 | 明确的 system scope 或逐 Org fan-out | 禁止隐式“扫描全部租户” |

每个入口都必须产生 `request_id` 或 `job_id`，并把 `org_id`、`principal_id`、`run_id`（如有）写入结构化日志与审计事件。

---

## 6. 查询、缓存与存储规则

### 6.1 PostgreSQL

- 租户资源仓储方法必须显式接收 `org_id`；
- 通过主键读取租户资源时仍必须附加 `org_id` 条件；
- 唯一约束默认以 `org_id` 为前缀，例如 `UNIQUE(org_id, slug)`；
- 高频查询索引以 `org_id` 为首列，具体见数据模型；
- 是否启用 PostgreSQL RLS 作为纵深防御由实现 spike 决定，但应用层过滤不得因 RLS 存在而省略；
- 跨 Org 管理查询只能位于独立 system-admin 仓储接口，并产生审计事件。

### 6.2 Redis

租户键统一使用：

```text
dn:{environment}:org:{org_id}:{resource_type}:{resource_id}
```

Run ownership 与 StreamBridge 键还必须包含 `run_id`。禁止仅用 `user_id`、`thread_id` 或外部 channel id 作为全局键。

### 6.3 Checkpoint / Store

- checkpoint namespace 必须包含 `org_id`；
- `thread_id` 在 API 中即使全局唯一，存储查询仍按 `org_id + thread_id`；
- checkpoint 恢复时必须校验 Run、Thread 与当前 TenantContext 的 Org 一致；
- 发现归属冲突时拒绝恢复并产生高优先级安全审计事件。

### 6.4 对象与文件存储

对象键使用：

```text
org/{org_id}/workspace/{workspace_id-or-_default}/{resource_type}/{resource_id}/...
```

`workspace_id IS NULL` 时路径段固定为字面量 `_default`。数据库保存对象内容摘要、大小、媒体类型和存储键。签名 URL 必须短期有效且在生成前完成授权。

---

## 7. 生命周期规则

- 租户资源创建后不得直接改变 `org_id`；
- 需要跨 Org 迁移时执行“导出 → 安全检查 → 导入”，不得数据库直接更新外键；
- Workspace 内资源可以在同 Org 下移动，但必须审计并校验引用关系；
- Org 删除采用异步受控流程：冻结 → 导出/保留检查 → 软删 → 延迟物理清理；
- 法律保全、审计保留和备份清理优先于普通删除请求；
- Slug 可以变更，UUID 不变。

---

## 8. 存量迁移

### 8.1 Bootstrap

1. 创建部署指定的默认 Organization；
2. 为存量用户创建 Membership；
3. 为租户资源新增可空 `org_id`（expand）；
4. 按资源依赖顺序回填默认 Org；
5. 校验孤儿记录、计数、外键和 checkpoint 归属；
6. 应用切换为强制写入 `org_id`；
7. 在租户过滤验证模式下创建不对外开放的第二 Org，执行完整隔离矩阵；
8. 将租户资源 `org_id` 改为非空并增加复合约束（enforce）；
9. 开启多组织功能开关；
10. 稳定观察后删除旧的仅 `user_id` 路径（contract）。

### 8.2 回填顺序

建议顺序：

```text
organizations / memberships
→ threads / runs
→ checkpoints / store / artifacts / memory
→ agents / skills / MCP / connectors
→ scheduled tasks / channel bindings
→ usage / audit
```

迁移必须可重入，使用批次和进度表；不得在未完成备份与 dry-run 时直接对生产全表回填。

### 8.3 回滚

- contract 前允许关闭多组织开关并回到单 Org 路径；
- contract 后不保证自动降级，必须从备份恢复或执行专用降级迁移；
- 已创建第二个 Org 后禁止回到不带 Org 过滤的旧版本。

---

## 9. 安全与测试要求

隔离回归至少覆盖：

`thread`、`run`、`checkpoint`、`memory`、`artifact`、`agent`、`skill`、`MCP`、`scheduled_task`、`api_key`、`console`、`audit_event`。

每类资源至少验证：

1. OrgA 正常创建、读取、更新、删除；
2. OrgB 按 OrgA 的资源 ID 读取不可见；
3. OrgB 不能通过列表、搜索、统计或导出旁路看到 OrgA；
4. 缓存、checkpoint、SSE、日志和审计不发生跨 Org 混淆；
5. system-admin 跨 Org 操作具备独立权限并留下审计证据。

跨 Org 泄露为阻断级缺陷，目标为零，不设错误预算。

---

## 10. 架构后果

### 正向

- 资源归属和查询范围在写代码前冻结；
- Workspace 可渐进启用，不阻塞 Org 级隔离；
- 外部平台标识不会污染平台主键；
- Runtime、控制面、缓存和存储使用同一租户语义。

### 代价

- 所有仓储、任务信封和缓存键都需显式携带 `org_id`；
- 存量数据需要分阶段迁移；
- system-admin 查询必须使用独立接口，不能复用普通租户仓储。

---

## 11. 验收

- [ ] `TenantContext` 字段在运行时契约中冻结
- [ ] 数据模型中的租户表和资源表符合本 ADR
- [ ] Gateway、Run、Scheduler、IM、API Key 解析点均有测试
- [ ] Redis、Checkpoint、对象存储命名空间包含 `org_id`
- [ ] 双 Org 隔离回归套件进入 CI
- [ ] 默认 Org 迁移完成一次生产规模 dry-run
- [ ] harness 不依赖控制面私有租户实现

---

## 12. 关联与后续决策

- [ADR-0003](0003-rbac-and-service-accounts.md)：RBAC、服务账号；MVP 不启用 Workspace 级授权
- [ADR-0004](0004-agent-artifacts-and-release.md)：Agent 制品不可变发布与通道
- [ADR-0005](0005-audit-event.md)：AuditEvent Schema、防篡改与保留
- ADR-0006：Gateway / Worker 物理拆分条件
