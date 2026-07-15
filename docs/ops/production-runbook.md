# DeerNexus 生产部署与恢复 Runbook

> 状态：MVP v0.1  
> 目标：可部署、可检查、可升级、可回滚、可恢复  
> 关联：[目标架构](../architecture/target-architecture.md) · [MVP 数据模型](../architecture/data-model.md) · [API 边界](../architecture/api-boundaries.md) · [安全基线](../security/baseline.md) · [90 天 MVP](../roadmap/90-day-mvp.md)

当前仓库尚未初始化 DeerFlow Fork，因此本文先冻结生产运行语义和验收步骤，不虚构具体 CLI、Helm Key 或环境变量名。Fork 后必须在本文“实现映射”中补充真实命令、配置路径、Dashboard 和负责人。

---

## 1. 生产目标与边界

### 1.1 MVP 服务目标

| SLI | MVP 建议 SLO | 窗口 |
| --- | --- | --- |
| Gateway 可用性 | 99.5% | 30 天 |
| Run 创建成功率 | 99%（不含明确策略拒绝） | 7 天 |
| Run 创建到 running / 明确失败 P95 | < 3 秒 | 7 天 |
| SSE 首个业务事件 P95 | < 5 秒 | 7 天 |
| Console 常用查询 P95 | < 500 毫秒（数据规模见[可观测性 §6.5](observability-and-slo.md#65-console)） | 7 天 |
| Redis Stream lag P95 | < 2 秒 | 7 天 |
| 跨 Org 数据泄露 | 0 | 持续 |
| prod 非 ReleaseRef 执行 | 0 | 持续 |
| 关键审计写路径覆盖 | 100% 或具名豁免 | 每次发布 |
| 备份成功率 | 100% | 30 天 |

### 1.2 恢复目标

- MVP RTO：不超过 4 小时；
- RPO 按已启用能力确定并进入发布清单：

| PostgreSQL 保护能力 | 声明与验收 RPO |
| --- | --- |
| 每日加密完整备份，无 PITR | ≤24 小时 |
| 连续 WAL / 托管 PITR 已启用 | ≤15 分钟，并持续检查 WAL 连续性 |

- Redis 不是已完成业务事实的唯一权威源；Redis 丢失后从 PostgreSQL 重建 ownership / reconcile；
- 跨区域多活不是 MVP 目标；
- 每季度至少一次恢复演练，首个 90 天验收前至少完成一次。

---

## 2. 组件与版本矩阵

Fork 初始化后，生产变更必须更新下表，不允许使用未固定的 `latest`。

| 组件 | MVP 基线 | 固定方式 | 权威数据 |
| --- | --- | --- | --- |
| DeerNexus Backend | Fork commit 待填 | Git commit + 镜像 digest | 应用 |
| Frontend | 同发布版本 | 镜像 / 静态制品 digest | 应用 |
| Python | 按上游支持矩阵待填 | 镜像 digest | 运行时 |
| PostgreSQL | 15+ | 托管版本或镜像 digest | **业务主存储** |
| Redis | 7+ | 托管版本或镜像 digest | 流与协调 |
| Sandbox Runtime | AIO / BoxLite / K8s Provisioner | 镜像 digest | 执行环境 |
| OIDC Provider | 企业 IdP | issuer + client 配置版本 | 身份源 |
| Object Store | S3 兼容或等价 | Bucket + policy 版本 | 大制品 / Artifact / 归档 |
| Secret Store | KMS / Vault / Secret Manager | Secret version | Secret |

最低要求：

- PostgreSQL 15+，以支持数据模型选定的 `UNIQUE NULLS NOT DISTINCT`；
- Redis 7+ 并启用 TLS / ACL，不允许以低版本或未认证实例进入生产；
- 所有生产镜像固定 digest；
- 记录 DeerFlow upstream tag / commit 与企业补丁清单；
- 数据库、应用和迁移版本必须在发布清单中成套记录。

---

## 3. 部署拓扑

### 3.1 Profile S：单副本 MVP

```text
Load Balancer / Reverse Proxy
  ├── Frontend
  └── Gateway x1
        ├── PostgreSQL
        ├── Redis
        ├── Object Store
        ├── Secret Store
        └── Sandbox Provisioner → Isolated Sandboxes
```

适用条件：

- multi-worker consistency spike 尚未通过；
- 部署配置明确锁定 `replicas=1`；
- Runbook 和发布说明明确“不宣称 HA”；
- PostgreSQL、对象存储和备份仍使用生产配置；
- Gateway 故障允许在 RTO 内恢复。

### 3.2 Profile H：双副本条件拓扑

```text
Load Balancer
  └── Gateway x2+
        ├── PostgreSQL
        ├── Redis StreamBridge / Run Ownership
        └── Sandbox Provisioner
```

启用条件：

1. Run ownership lease / heartbeat / reconcile 已实现并测试；
2. cancel、completion、timeout 竞争有原子状态转换；
3. SSE 能跨副本恢复或通过稳定 StreamBridge 路由；
4. 滚动重启无不可恢复僵尸 Run；
5. Redis 短暂中断的失败语义明确；
6. 通过 §12 的双副本验收。

未满足任一项时保持单副本，不能仅增加 Replica 数量后宣称高可用。

### 3.3 Worker 语义

MVP 中 Worker 是逻辑执行角色，可以内嵌 Gateway。只有满足后续 ADR-0006 的容量、故障域或独立发布条件后才物理拆出 `app/worker`。

### 3.4 Profile W：物理 Worker

Profile W 表示 Gateway 与 Worker 物理拆分，启用条件、至少一次投递、灰度和回滚见 [ADR-0006](../adr/0006-gateway-worker-split.md)。Profile W 仍需单独声明 Gateway 接入层采用 Profile S 或 H；“Worker 已拆分”不自动等于 Gateway 高可用。

---

## 4. 环境与配置

### 4.1 环境

至少分离：

- development：允许本地依赖与开发态 Agent；
- staging：与生产同拓扑类别，用于迁移、隔离、发布和恢复演练；
- production：只允许已审核配置、已发布镜像和 ReleaseRef。

环境必须使用不同：

- OIDC client；
- 数据库、Redis、Bucket；
- Secret 与工作负载身份；
- API Key；
- Agent Release Channel 绑定；
- 网络策略。

禁止 production 读取 development / staging Secret 或数据。

### 4.2 配置分类

| 分类 | 示例 | 存储 |
| --- | --- | --- |
| 非敏感运行配置 | Feature Flag、超时、上限、日志级别 | 版本化配置 |
| 部署配置 | Replica、资源、网络、健康检查 | IaC / Helm / 等价 |
| Secret | DB、OIDC、模型、MCP、签名密钥 | Secret Store |
| 租户配置 | Org 设置、Connector、Policy | PostgreSQL + Secret 引用 |
| 发布引用 | 镜像 digest、Agent digest、迁移版本 | 发布清单 |

生产配置变更必须：

- 经 PR 或等价审查；
- 有变更记录、Owner 和回滚方式；
- 敏感值不进入版本库；
- 避免控制台手工修改产生漂移；
- 由配置校验或 doctor 检测不安全组合。

---

## 5. Preflight / Doctor

Fork 后实现 `doctor` 或等价检查器。检查结果分：

- `PASS`：满足生产要求；
- `WARN`：允许启动，但必须登记已知限制；
- `FAIL`：阻止生产启动。

### 5.1 必检项

| 检查 | 失败级别 |
| --- | --- |
| PostgreSQL 可达、版本 ≥15、迁移版本兼容 | FAIL |
| Redis 可达；双副本时 Stream / ownership 能力可用 | FAIL |
| OIDC issuer / audience / JWKS 配置有效 | FAIL |
| 生产未启用 host bash | FAIL |
| Sandbox Provisioner 可创建隔离实例 | FAIL |
| Secret 来源为受控 Secret Store | FAIL |
| 对象存储私有、可读写并启用加密 | FAIL |
| `org_id` 强制写入与多租户 Feature 状态匹配 | FAIL |
| prod Agent 只解析已发布 ReleaseRef | FAIL |
| Audit sink / outbox 可用 | FAIL |
| 数据库连接池、Run 上限、Sandbox 上限非无限 | FAIL |
| 日志级别非 DEBUG，脱敏规则启用 | FAIL |
| TLS / CORS / CSRF 生产配置有效 | FAIL |
| 关键接口限流启用，429 包含 `Retry-After` | FAIL |
| 备份 / WAL 最近成功时间在声明 RPO 内 | FAIL |
| `replicas=1` 但未明确声明 Profile S | FAIL |
| `replicas≥2` 但未声明 Profile H，或未通过 §12 验收 | FAIL |
| 声明 Profile W 但 ADR-0006 前置、24 小时 Soak 或回滚演练未通过 | FAIL |
| 双副本一致性测试未通过但 replicas>1 | FAIL |
| 单副本部署存在已登记 HA 豁免 | WARN |

### 5.2 租户迁移状态判定

| 阶段 | 新写入带 `org_id` | 存量空值 | 验证 Org | Multi-org Feature | Doctor |
| --- | --- | --- | --- | --- | --- |
| Expand / Backfill | 必须 | 迁移清单内允许 | 无 | OFF | 迁移模式 PASS；不可作为多租户生产出口 |
| Enforce 验证 | 必须 | 0 | 不对外开放 | OFF | PASS；仅验证主体可访问第二 Org |
| Multi-org Active | 必须 | 0 | 可转正式或删除 | ON | PASS |
| Feature ON 但仍有空 `org_id` | 不确定 | >0 | 任意 | ON | FAIL |
| 租户过滤关闭但存在多 Org | 任意 | 任意 | 有 | 任意 | FAIL |

Doctor 必须读取明确迁移阶段，不得只根据 Feature Flag 猜测状态。

### 5.3 Doctor 输出

禁止输出 Secret。输出至少包含：

```text
check_id
status
component
message
remediation
config_source（不含值）
timestamp
```

发布流水线保存 doctor 结果作为部署证据。

---

## 6. 首次部署

### 6.1 准备

- [ ] 固定应用、前端、Sandbox 镜像 digest
- [ ] 创建 PostgreSQL、Redis、对象存储和 Secret Store
- [ ] 创建最小权限数据库角色
- [ ] 配置 OIDC client 与回调地址
- [ ] 配置 TLS、DNS、反向代理与网络策略
- [ ] 创建备份策略和告警
- [ ] 确认单副本或双副本模式
- [ ] 准备默认 Org、初始 system-admin 的安全 bootstrap 方式

### 6.2 推荐顺序

1. 创建数据库与应用角色；
2. 创建对象存储 Bucket 和生命周期；
3. 创建 Redis 与访问策略；
4. 写入 Secret Store；
5. 运行数据库迁移 preflight；
6. 执行 Alembic upgrade；
7. 创建默认 Organization 与初始管理员；
8. 部署 Gateway 单副本；
9. 执行 doctor；
10. 执行身份、双 Org 隔离、Run、Sandbox、Audit smoke test；
11. 部署 Frontend；
12. 如已满足双副本条件，再扩为 2+ 副本并执行滚动验收；
13. 开启外部流量；
14. 记录发布清单。

### 6.3 Bootstrap 安全

- 初始 system-admin 通过一次性受控命令或部署 Job 创建；
- 一次性凭证使用后立即撤销；
- 不提供公开的“首个注册用户自动成为管理员”路径；
- 默认 Org ID、创建者和时间进入审计；
- Bootstrap Job 不能长期保留生产管理权限。

---

## 7. 健康检查

### 7.1 Liveness

只证明进程可响应：

- 不执行数据库重查询；
- 不返回内部配置；
- 失败触发进程重启；
- 可以在受限内网匿名。

### 7.2 Readiness

证明实例可接收请求，至少检查：

- 应用初始化完成；
- 数据库可用且迁移版本兼容；
- 必需配置已加载；
- Redis 可用；双副本模式额外检查 ownership / StreamBridge；
- 强审计写路径能够在本地数据库事务或 outbox 中持久接收事件；外部归档短时延迟通过告警和背压处理，不因单纯归档延迟摘除全部 Gateway；
- 实例未处于 draining。

OIDC 或外部模型短暂不可用不一定使整个 Gateway unready，但对应新登录或模型调用必须明确失败并告警。

### 7.3 深度健康

需运维身份，检查：

- 创建和清理最小数据库事务；
- Redis Stream / lease；
- 对象存储小对象写读删；
- Sandbox acquire / execute / release；
- Audit outbox 投递；
- OIDC discovery / JWKS；
- 时间同步。

深度健康不作为高频 Kubernetes probe，避免放大依赖故障。

---

## 8. 数据库迁移

### 8.1 规则

- 使用 Alembic；
- 采用 expand → backfill → enforce → contract；
- 迁移与应用版本兼容范围写入发布清单；
- 大表回填使用批次、水位和限速；
- DDL 锁风险在 staging 用生产规模样本验证；
- 迁移前确认备份在 RPO 内；
- contract 迁移与旧应用不兼容时，先确保无法回滚到旧应用。

### 8.2 发布步骤

1. 迁移 preflight：版本、空间、锁、备份、预计时长；
2. 部署兼容旧/新 Schema 的应用版本；
3. 执行 expand；
4. 异步 backfill，监控延迟与错误；
5. 执行单 Org 数据完整性校验，确认回填完成；此时禁止向真实用户开放第二 Org；
6. 启用租户过滤验证模式，创建不对外开放的验证 Org；
7. 执行完整双 Org 隔离矩阵；
8. enforce 非空、外键和复合唯一约束；
9. 开启多组织 Feature Flag；
10. 观察至少一个约定窗口；
11. contract 清理旧列和旧路径。

### 8.3 禁止

- 在未备份时直接修改生产大表；
- 同一发布同时做不可回滚迁移和大规模功能切换而无阶段开关；
- 失败后手工改迁移版本表假装成功；
- 多组织开启前仍存在未归属资源；
- contract 后回滚到不理解 `org_id` 的旧版本。

---

## 9. 备份

### 9.1 PostgreSQL

最低策略：

- 每日完整备份；
- 托管服务支持时启用连续 WAL / PITR；
- 保留至少 7 个每日恢复点，生产默认建议 30 天；
- 备份加密；
- 备份与主数据库权限和故障域分离；
- 每日监控备份完成、大小、耗时和最近可恢复时间；
- 每季度执行恢复演练。

数据库备份保留不是审计保留的替代品。AuditEvent 必须按[安全基线 §11](../security/baseline.md#11-审计安全)保留至少 90 天热数据，并通过独立归档作业满足默认 365 天总保留。

必须覆盖：

- Tenant / IAM；
- Thread / Run；
- AgentPackage / Release；
- Audit / Usage；
- Connector 非敏感配置和 Secret 引用；
- Alembic 版本。

### 9.2 Redis

Redis 保存流和协调状态，不是已完成业务事实唯一来源：

- 双副本模式建议启用 AOF `everysec` 或托管等价持久化；
- 监控内存、eviction、AOF / snapshot 错误；
- 禁止对 ownership / Stream 键使用会导致静默淘汰的策略；
- Redis 恢复后以 PostgreSQL Run 状态执行 reconcile；
- 不从旧 Redis 备份覆盖更新的 PostgreSQL terminal 状态。

### 9.3 对象存储

- 启用服务端加密；
- 启用版本控制；
- 制品 Bucket 与临时 Artifact 生命周期分离；
- 已发布 digest 在引用存在期间不得被生命周期规则删除；
- Audit 归档建议启用 Object Lock / 保留策略；
- 保存对象 inventory 或定期核对数据库引用与对象存在性。

### 9.4 Secret

- 备份 Secret metadata 和恢复流程；
- 根密钥按 Secret Store 厂商方案恢复；
- 不把 Secret 明文复制进普通数据库备份；
- 恢复演练验证引用可解析，但避免在日志展示 Secret。

---

## 10. 恢复流程

### 10.1 宣告与冻结

1. 建立事件记录并指定 Incident Commander；
2. 冻结写流量或切维护模式；
3. 记录最后已知数据库时间、应用版本和迁移版本；
4. 保护当前日志、Audit、镜像和故障数据；
5. 选择符合 RPO 的恢复点；
6. 通知相关 Owner 和安全负责人。

### 10.2 恢复顺序

```text
基础网络 / Secret Store
→ PostgreSQL
→ 对象存储引用验证
→ Redis 空实例或可信恢复点
→ Gateway 单副本
→ reconcile Run ownership / terminal state
→ Sandbox Provisioner
→ Frontend
→ 外部流量
```

### 10.3 PostgreSQL 验证

- Alembic 版本与应用兼容；
- Organization、Membership、RoleBinding 计数合理；
- 租户资源 `org_id` 非空；
- AgentVersion digest 与对象存在；
- ReleaseChannel 指向有效 Version；
- Run / Thread / Checkpoint 关系完整；
- AuditEvent 时间连续性和最近事件；
- UsageRecord 无异常重复；
- 外键、唯一约束和索引有效。

### 10.4 Redis 与 Run reconcile

恢复后：

1. 不信任旧 lease；
2. 清理过期 ownership；
3. 从 PostgreSQL 扫描 `pending` / `running` / `clarification_required` / `approval_required` / `cancelling`；
4. 按 Run 状态和 checkpoint 判断可恢复、失败或取消；
5. 使用 CAS 更新状态；
6. 记录 reconcile 事件和指标；
7. 不重复执行已确认完成的外部副作用。

不能证明幂等的外部写工具不得自动重放，必须进入人工处理或安全失败。

### 10.5 恢复验收

- [ ] system-admin 和普通 Org 用户可登录
- [ ] OrgA 无法访问 OrgB 数据
- [ ] 新 Run 使用正确 ReleaseRef
- [ ] 历史 Run 仍指向原 digest
- [ ] SSE、cancel、Sandbox 可用
- [ ] Audit 写入和查询可用
- [ ] Audit 归档水位与对象摘要抽样一致
- [ ] 可执行范围内 `legacy_unpinned` Run 计数为 0；保留记录仅可读取、取消和归档
- [ ] 已按[数据治理 §9](../compliance/data-governance.md#9-备份中的删除)重放 deletion ledger / tombstone，已删除 Org、主体和内容不会因旧备份恢复而重新可见
- [ ] Connector Secret 引用可解析
- [ ] 备份恢复点与实际数据损失在 RPO 内
- [ ] 服务恢复时间在 RTO 内

---

## 11. 滚动升级与回滚

### 11.1 升级顺序

1. 发布候选在 staging 通过完整门禁；
2. 确认备份、迁移兼容和回滚镜像；
3. 执行兼容性迁移；
4. 将一个实例标记 draining；
5. 停止接收新 Run，允许流式连接按超时迁移；
6. 部署新镜像并等待 readiness；
7. 执行 smoke test；
8. 逐实例继续；
9. 观察 SLO、Run 状态、Redis lag、Audit 和 Sandbox；
10. 完成发布记录。

### 11.2 单副本

- 需要维护窗口或短暂不可用；
- 部署前等待可安全结束的 Run，无法等待时持久化 checkpoint；
- 明确客户端重连行为；
- 回滚前确认数据库 Schema 仍兼容。

### 11.3 双副本

- `maxUnavailable=1` 或等价策略；
- 至少一个 ready 实例持续服务；
- draining 实例不再领取新 lease；
- ownership 转移必须等旧 lease 失效或显式释放；
- SSE 通过 Redis / Last-Event-ID 恢复；
- 滚动期间持续运行测试 Run 检测僵尸和重复。

### 11.4 应用回滚

允许条件：

- Schema 与旧版本兼容；
- 未执行不可逆 contract；
- Feature Flag 可关闭新路径；
- Agent Release 回滚与应用镜像回滚分开处理；
- 不覆盖已产生的 Audit 和 ReleaseEvent。

如 Schema 不兼容，优先 forward fix；不得强行运行旧二进制连接新 Schema。

---

## 12. Run 控制与双副本验收

### 12.1 初始参数

Fork 后按上游实现校准。MVP spike 可从以下值开始：

| 参数 | 初始值 |
| --- | --- |
| Ownership lease TTL | 60 秒 |
| Heartbeat interval | 15 秒 |
| Reconcile interval | 30 秒 |
| Worker claim retry jitter | 1–5 秒 |
| Graceful drain | 不小于 lease TTL，且受部署超时约束 |
| Cancel propagation SLO | ≤60 秒 |
| Terminal convergence SLO | ≤300 秒 |

原则：

- heartbeat ≤ TTL / 3；
- reconcile ≤ TTL / 2；
- 所有状态更新时间使用服务器 UTC；
- ownership Key 含 environment、org_id、run_id；
- 领取和续租使用原子操作；
- terminal 状态写入 PostgreSQL 后不可被旧 owner 覆盖。

### 12.2 必测场景

- 两实例同时 claim 同一 Run；
- owner 进程在工具调用前、调用中、状态提交前崩溃；
- Redis 短暂断连和完全重启；
- PostgreSQL 慢查询与事务超时；
- cancel 与 completion 同时发生；
- 滚动重启；
- SSE 客户端跨实例重连；
- checkpoint 存在 / 缺失 / 损坏；
- reconcile 重复运行；
- Sandbox 已执行外部副作用但状态未提交。

### 12.3 出口标准

- 同一 Run 同时只有一个有效 owner；
- terminal 状态最终一致且不回退；
- cancel 请求幂等；
- 可达 Owner 的 cancel 在 60 秒内传播；Owner 失联时按 lease 过期与 reconcile 路径处理；
- 滚动重启后 5 分钟内所有非终态 Run 被继续、明确失败或进入人工处理；
- 僵尸 Run 率小于 0.1%；
- 不可证明幂等的外部副作用不自动重复；
- 测试失败则生产配置锁定单副本并登记豁免。

---

## 13. 容量与背压

### 13.1 必须配置的上限

- 每 Org 并发 Run；
- 每 Principal Run 创建速率；
- 全局并发模型调用；
- Sandbox Pool 最大实例；
- 单 Sandbox CPU、内存、PID、磁盘、时间；
- SSE 每实例连接数；
- Tool / MCP 响应大小；
- Thread / Prompt / Artifact 大小；
- PostgreSQL 连接池；
- Redis 最大内存与禁止淘汰的 Key 类；
- Audit outbox 最大积压和告警阈值。

禁止使用“无限”作为生产默认值。

### 13.2 背压

- 达到 Org 并发上限：返回 429 或排队，并给出可观测状态；
- 全局资源紧张：优先公平排队，不能让单 Org 占满；
- Sandbox Pool 耗尽：Run 保持明确 pending，不伪报 running；
- Audit outbox 超阈值：高风险写操作 fail-closed；
- Redis lag 过高：停止领取新 Run，保护已有 Run；
- PostgreSQL 连接耗尽：readiness 降级并告警，禁止无限重试风暴。

### 13.3 压测基线

Fork 后至少记录：

- 100 / 1000 Run 每日负载；
- 并发 10 / 50 / 100 Run；
- SSE 长连接数；
- 平均 / P95 模型调用与 Sandbox 冷启动；
- Audit、Usage、Checkpoint 日增长；
- PostgreSQL CPU、IO、连接和慢查询；
- Redis 内存、Stream lag、命令延迟；
- 单 Run 成本和单 Org 归因。

压测与灾备证据以[容量与灾备 §7](capacity-and-dr.md#7-压测记录)为唯一记录源；本 Runbook 只引用 `test_id` 和当前安全上限，不重复维护另一套容量数字。未经测试的估算不能宣称为容量承诺。

---

## 14. 监控与告警

### 14.1 必要指标

Gateway：

- request rate、5xx、4xx、latency；
- OIDC 登录成功 / 失败；
- Run create / cancel / resume；
- SSE connection、reconnect、first-event latency。

Runtime：

- Run 状态数与时长；
- lease acquire / conflict / expiry；
- heartbeat failure；
- reconcile backlog / outcome；
- tool / model / MCP latency 与错误；
- Sandbox acquire、timeout、OOM、quarantine。

数据：

- PostgreSQL 连接、事务、锁、复制 / 备份、磁盘；
- Redis memory、eviction、latency、Stream lag、AOF；
- Object Store error、missing digest；
- Audit outbox backlog / failure；
- Usage ingestion lag。

安全：

- 跨 Org 探测；
- Policy deny / approval required；
- API Key 异常；
- SSRF 阻断；
- Sandbox 逃逸迹象；
- 审计不可用。

### 14.2 最小告警

| 告警 | 级别 / 条件建议 |
| --- | --- |
| Gateway 5xx >1% 持续 5 分钟 | P2 早期预警 |
| Gateway 5xx >5% 持续 5 分钟或核心路径不可用 | P1 |
| PostgreSQL 不可达 | P1 |
| Redis 不可达且双副本启用 | P1 |
| Audit 强写路径不可用 | P1 |
| 跨 Org 泄露证据 | P1，立即遏制 |
| Sandbox 逃逸迹象 | P1 |
| Run create P95 >5 秒持续 15 分钟 | P2 |
| Console 默认查询 P95 >500ms 持续 15 分钟 | P2 |
| Redis Stream lag P95 >2s 持续 15 分钟 | P2 |
| Reconcile backlog >100 或超过 5 分钟 | P2 |
| Sandbox Pool 耗尽 >10 分钟 | P2 |
| Audit dead letter >0 | P2 |
| Audit 归档摘要校验失败 | P1 |
| 备份超过 RPO 未成功 | P1 |
| 证书 / Secret 即将到期 | P2，提前 14 / 7 / 1 天 |

---

## 15. 常见故障手册

### 15.1 PostgreSQL 不可用

1. 停止新 Run 和控制面写入；
2. 保持无害健康页，避免无限重试；
3. 检查托管服务、网络、凭证、连接池；
4. 不切换到本地 SQLite 或非生产存储；
5. 按数据库故障转移或 §10 恢复；
6. 恢复后校验迁移、Run、Release 与 Audit。

### 15.2 Redis 不可用

单副本：

- readiness 立即失败并停止创建新 Run；
- 不再启动依赖 StreamBridge / Redis 的 SSE、Scheduler 或恢复操作；
- 保留 PostgreSQL 中的 Run 状态，不把失败伪报为完成；
- Redis 恢复后按 §10.4 清理旧状态并 reconcile；
- 只有 Fork 盘点证明某只读端点完全不依赖 Redis 时，才可在明确 allowlist 内继续提供。

双副本：

- 停止领取新 Run；
- 保留 PostgreSQL 状态；
- 恢复 Redis 后清理旧 lease 并 reconcile；
- 检查是否发生重复工具副作用。

### 15.3 IdP 不可用

- 新登录 fail-closed；
- 未过期 Session 按既定策略继续；
- 不启用公开的临时管理员密码；
- Break-glass 账号必须预先建立、最小权限、强审计和定期演练。

### 15.4 Sandbox Pool 耗尽

- 新 Run 保持 pending；
- 检查泄漏、超时、配额和 Provisioner；
- 回收终态 Run 的实例；
- quarantine 异常实例；
- 必要时对低优先级 Org 限流，不跨 Org 抢占有状态 Sandbox。

### 15.5 Audit 不可用

- 高风险 Admin / Studio 写操作 fail-closed；
- 运行期低风险事件进入本地受控缓冲或 outbox；
- 监控积压并限制磁盘；
- 恢复后按 event_id 幂等投递；
- 不允许静默丢弃或关闭审计绕过。

### 15.6 Release 错误

- 使用 ReleaseChannel rollback，不修改已发布 AgentVersion；
- 回滚只影响新 Run；
- 在途 Run 继续固定原 digest；
- 发布与回滚均产生 ReleaseEvent 和 AuditEvent；
- 若 digest 对象缺失，停止新 Run 并视为完整性事件。

---

## 16. 发布与恢复证据

每次生产发布保存：

```text
release_id
backend / frontend / sandbox image digest
DeerFlow upstream commit
database migration revision
feature flags
profile（S / H / W；W 同时声明 Gateway S / H）
doctor result
test report
security scan / SBOM reference
deployer and approver
start / finish time
rollback target
known waivers
```

每次恢复演练保存：

```text
exercise_id
backup point
declared RPO / RTO
actual data loss / recovery time
restore steps and deviations
tenant isolation validation
ReleaseRef / Audit validation
issues, owner, due date
```

---

## 17. Fork 后实现映射

代码导入后立即补充，并最迟在 Day 75 前完成 staging 验证；生产验收不得使用占位命令或模拟 doctor 替代：

| 项目 | 实现映射 / 待填 |
| --- | --- |
| Backend 启动命令 | 路径与参数 |
| Frontend 构建 / 启动 | 路径与参数 |
| Alembic 配置与 head | 路径 / revision |
| Doctor 命令 | `make doctor-production`；结构化证据：`cd backend && uv run python ../scripts/doctor.py --profile production --json`；存在任一 `FAIL` 时退出 1，否则退出 0 |
| 生产声明 Schema | `config.yaml:production`，类型定义见 `backend/packages/harness/deerflow/config/production_config.py` |
| Helm / Compose / IaC | 路径 |
| Gateway health endpoints | 路径 |
| Redis Stream / ownership Key | 实际格式 |
| Run lease 参数 | 配置名与默认值 |
| Sandbox Provisioner 配置 | `config.yaml:sandbox.provisioner_url`、`sandbox.replicas` 与 `production.limits.max_sandbox_replicas` |
| Backup Job | 名称与计划 |
| Dashboard / Alerts | 链接 |
| On-call / Owner | 团队与升级方式 |

PR-003 的 PostgreSQL、Redis、OIDC、Sandbox、Backup、部署证据、Secret Store 与 Gateway 安全真实探针会明确返回 `FAIL`，直到 PR-064 接入真实依赖；静态声明通过不能作为生产准入证据。在其余映射补齐并经过 staging 验证前，本文仍是运行语义基线，不应被描述为已验证的逐命令操作手册。

---

## 18. 90 天生产出口

- [ ] 组件版本与镜像 digest 固定
- [ ] doctor 对生产配置全 PASS 或有有效 WARN 豁免
- [ ] PostgreSQL、Redis、OIDC、Sandbox、对象存储连通
- [ ] `allow_host_bash=false`
- [ ] 单 / 双副本模式与声明一致
- [ ] 数据库迁移和默认 Org bootstrap 可重复
- [ ] 双 Org 隔离 smoke + CI 套件通过
- [ ] prod 只运行已发布 ReleaseRef
- [ ] prod 拒绝 admit / resume / continue `legacy_unpinned` Run
- [ ] Audit 强写路径与 outbox 可用
- [ ] 备份在 RPO 内，完成一次恢复演练
- [ ] 滚动升级或单副本维护流程演练完成
- [ ] P1 / P2 告警可触发并到达责任人
- [ ] 真实命令、配置、Dashboard 和 Owner 已补入 §17
