# DeerNexus 容量与灾备计划

> 状态：MVP 规划与实测记录模板 v0.1  
> 说明：当前尚未初始化 DeerFlow Fork；本文冻结测量方法、阈值规则和灾备验收，不把未实测数字宣称为容量承诺  
> 关联：[生产 Runbook](production-runbook.md) · [可观测性与 SLO](observability-and-slo.md) · [测试策略](../engineering/testing-strategy.md) · [ADR-0006](../adr/0006-gateway-worker-split.md) · [数据治理](../compliance/data-governance.md)

---

## 1. 目标

1. 用可重复压测而不是经验猜测确定生产上限；
2. 明确 Gateway、Worker、PostgreSQL、Redis、Sandbox 和对象存储的瓶颈；
3. 在达到危险水位前扩容、背压或限流；
4. 防止单 Org 或单类任务耗尽共享资源；
5. 定义单区域故障、数据损坏和依赖中断的恢复策略；
6. 用恢复演练证明 RPO / RTO；
7. 为 Gateway / Worker 物理拆分提供量化证据；
8. 记录容量、成本和可靠性随版本变化的趋势。

---

## 2. 当前声明

### 2.1 未实测前

- 不承诺最大并发 Run、RPS、SSE 连接或 Sandbox 数；
- 不宣称 Profile H；
- 生产默认 Profile S：Gateway `replicas=1`；
- 所有资源上限必须配置为有限值；
- 首次生产容量由 staging 生产等价压测结果决定；
- 上线初期安全运行上限不超过已验证稳定容量的 50%；
- 扩容阈值不超过已验证容量的 70%；
- 达到 85% 视为高风险，停止扩大流量并执行降载。

### 2.2 目标 Profiles

| Profile | 拓扑 | 声明 |
| --- | --- | --- |
| S | Gateway x1，逻辑 Worker 内嵌 | 可恢复单副本，不宣称 HA |
| H | Gateway x2+，共享 PG / Redis，逻辑 Worker 可内嵌 | 通过 lease / cancel / reconcile / rolling 验收后声明 HA |
| W | Gateway 与 Worker 物理拆分 | 仅在 ADR-0006 前置条件和触发证据满足后启用 |

Profile 名称进入发布清单、Dashboard 和 Doctor。

---

## 3. 工作负载模型

### 3.1 Run 类型

至少区分：

| 类型 | 特征 | 主要瓶颈 |
| --- | --- | --- |
| Short Chat | 短 Prompt、少量模型调用、无 Sandbox | Gateway、模型并发、SSE |
| Research | 长 Run、多模型 / Tool、Checkpoint 多 | Redis、PG、模型、存储 |
| Sandbox Build | 高 CPU / 内存 / 磁盘、冷启动 | Provisioner、节点、镜像 |
| Connector Write | 外部副作用、低吞吐高风险 | Policy、凭证、幂等、外部 API |
| Scheduled | 集中触发、无人值守 | Scheduler、Admission、公平队列 |
| IM / Webhook | 突发、签名与重放 | Gateway、绑定解析、幂等 |
| Admin / Console | 聚合查询 | PostgreSQL、索引、缓存 |
| Audit Export | 长查询、对象写 | PostgreSQL、对象存储、背压 |

不能只使用“平均 Run”压测。

### 3.2 租户分布

基线场景：

- 小 Org 多、少量大 Org；
- 1 个突发 Org + 多个稳定 Org；
- 多 Org 同名资源；
- 不同 Agent / Tool 风险和 Sandbox profile；
- 活跃 / 冷 Thread 混合；
- 新 Run 与历史 Console 查询并发；
- Release promote / rollback 与在途 Run 并发。

### 3.3 数据规模

至少覆盖：

```text
Organizations
Users / Memberships
Threads per Org
Runs per day
Average / P95 Run duration
Checkpoint count and bytes per Run
Artifact count and bytes
AgentVersions and ReleaseEvents
AuditEvents per Run / management action
UsageRecords per model attempt
SSE concurrent connections
Scheduled burst size
```

Console 基线沿用可观测性规范：单个合成 Org 至少 100,000 Runs、1,000,000 UsageRecords。

---

## 4. 容量单位

### 4.1 Run Unit

为比较不同工作负载，记录但不把单一分数用于强调度：

```text
model_calls
input_tokens
output_tokens
tool_calls
sandbox_seconds
checkpoint_bytes
artifact_bytes
run_duration
```

容量决策基于各资源维度，不用 token 数替代 CPU、连接或 Sandbox 容量。

### 4.2 并发

区分：

- admitted；
- pending capacity；
- running；
- interrupted / approval / clarification；
- cancelling；
- terminal；
- active SSE；
- active Sandbox；
- active model / Tool / MCP attempts。

只统计 running 会隐藏 pending 和 interrupted 占用。

---

## 5. 组件容量模型

### 5.1 Gateway

测量：

- HTTP RPS、P50 / P95 / P99；
- Run admission CPU / 内存；
- Session / OIDC 验证；
- SSE 连接、事件吞吐、慢客户端；
- Admin / Console 查询；
- 文件上传；
- event loop / thread pool saturation；
- 数据库连接占用；
- 进程 drain 时间。

扩容或拆分信号：

- CPU >70% 持续两个 15 分钟窗口；
- 内存 >70% 已验证上限或持续增长；
- event loop / worker queue 饱和；
- Run admission P95 接近 3 秒 SLO；
- SSE 连接达到已测稳定值 70%；
- 执行负载而非 API 流量成为主要 CPU / 内存来源。

百分比以已验证配置和容器 limit 为分母，不以节点总资源掩盖单 Pod 饱和。

### 5.2 Worker / Runtime

测量：

- claim throughput；
- active lease；
- heartbeat failure；
- queue / dispatch lag；
- Run duration；
- model / Tool / Sandbox 等待；
- terminal convergence；
- crash / restart；
- non-idempotent manual-review 数；
- 每 Worker CPU / 内存；
- drain 时间。

Profile W 触发和前置条件见 ADR-0006。

### 5.3 PostgreSQL

测量：

- CPU、内存、IOPS、存储和 WAL；
- active / idle / waiting connections；
- pool wait；
- transaction duration；
- lock / deadlock；
- query P95 / P99；
- table / index bloat；
- autovacuum lag；
- replica / backup lag；
- Run、Checkpoint、Audit、Usage 日增长；
- Console 查询；
- Migration DDL 和 backfill。

连接预算：

```text
database_max_connections
- admin_and_migration_reserve
- monitoring_reserve
- incident_reserve
= application_connection_budget
```

所有 Gateway / Worker pool 总和不能超过 application budget。生产至少保留 10% 或明确数量的事件响应连接。

### 5.4 Redis

测量：

- memory used / fragmentation；
- key / Stream 数；
- command latency；
- Stream lag；
- consumer backlog；
- AOF rewrite；
- eviction；
- expired lease；
- ownership conflict；
- network bandwidth；
- reconnect storm。

约束：

- ownership / Stream Key 不允许静默 eviction；
- 达到 70% 已测内存进入扩容计划；
- 达到 85% 停止扩大 admission；
- lag P95 >2 秒持续 15 分钟告警；
- Redis 丢失不破坏 PostgreSQL terminal 事实。

### 5.5 Sandbox

测量：

- Provisioner create / acquire P50 / P95；
- active / pending / quarantine；
- image pull；
- CPU / memory / PID / disk；
- timeout / OOM；
- cleanup；
- 节点池利用率；
- egress；
- Secret profile；
- 每 Run 成本。

池容量：

```text
effective_capacity =
  min(cpu_capacity, memory_capacity, pid_capacity, disk_capacity, network_capacity)
  - quarantine
  - maintenance_reserve
```

至少保留 20% 安全余量，除非实测证明其他值更合适。

### 5.6 对象存储

测量：

- bytes / objects；
- request rate；
- error / throttle；
- upload / download P95；
- version count；
- missing digest；
- lifecycle delete；
- audit archive lag；
- signed URL 使用；
- cross-region / egress cost。

已发布 AgentVersion 和未过期 Audit 归档不能被普通生命周期规则删除。

### 5.7 外部模型 / MCP / Tool

记录：

- provider limit；
-并发和排队；
- 429 / 5xx；
- latency；
- retry；
- token / cost；
- region；
- timeout；
- circuit breaker；
- 单 Org 公平性。

外部供应商上限必须低于平台无限重试触发点，避免重试放大。

---

## 6. 初始负载阶梯

Fork 后按以下阶梯建立基线：

### Stage 1：功能基线

- 1 / 5 / 10 并发 Run；
- 100 SSE；
- 2 Org；
- 小数据集；
- 验证指标、Trace、资源清理。

### Stage 2：MVP 目标

- 10 / 50 / 100 并发 Run；
- 100 / 1,000 SSE；
- 100 / 1,000 Run / 日的数据分布；
- 100k Runs / 1m UsageRecords Console 数据；
- Sandbox、Research、Short Chat 混合；
- 单 Org 突发。

### Stage 3：容量探索

- 每次增加 25% 负载；
- 直到首次稳定 SLO 失败、错误显著上升或资源达到 85%；
- 记录拐点；
- 回退到最近稳定档进行 8 小时 Soak；
- Profile H / W 至少 24 小时。

禁止在容量探索时使用真实客户外部副作用。

---

## 7. 压测记录

每次压测保存：

```text
test_id
date
git_commit
image_digests
profile
infrastructure
configuration_digest
database_size
workload_mix
org_distribution
duration
offered_load
accepted_load
SLI results
resource saturation
errors
first_bottleneck
stable_capacity
unsafe_capacity
cost
observations
owner
```

### 7.1 当前结果

当前无代码和生产等价环境，**尚无实测容量结果**。Fork 后不得删除此声明后留空；必须链接测试报告。

| Profile | 稳定并发 Run | 稳定 SSE | Run admission P95 | 首要瓶颈 | 证据 |
| --- | --- | --- | --- | --- | --- |
| S | 待测 | 待测 | 待测 | 待测 | 待填 |
| H | 待测 | 待测 | 待测 | 待测 | 待填 |
| W | 未启用 | 未启用 | 未启用 | ADR-0006 前置未验证 | 不适用 |

---

## 8. 安全运行水位

每个环境记录：

```text
tested_stable_capacity
production_limit
warning_threshold
critical_threshold
hard_reject_threshold
scaling_action
degradation_action
owner
```

初始规则：

- production limit ≤ stable capacity × 50%；
- warning ≤ stable capacity × 70%；
- critical ≤ stable capacity × 85%；
- hard reject 不超过实测安全上限；
- 两个版本持续稳定后，可以基于证据调整；
- 零容忍正确性 / 安全错误不因容量余量而接受。

---

## 9. 背压与降级

### 9.1 Admission

- 每 Org 并发上限；
- 每 Principal 创建速率；
- 全局模型和 Sandbox 上限；
- 超限返回 429 或明确 pending；
- 返回 `Retry-After`；
- 不把 pending 伪报 running；
- 公平队列避免单 Org 饥饿其他 Org。

### 9.2 降级顺序

优先关闭或限制：

1. 非关键后台聚合；
2. 超大 Console 时间范围；
3. 低优先级 scheduled Run；
4. 非必要 Debug / 高成本 Trace；
5. dev / staging Channel 执行；
6. 新低优先级 Run admission。

不得降级：

- Tenant / RBAC；
- prod ReleaseRef；
- Secret / Sandbox 隔离；
- Class A Audit；
- 数据完整性；
- 已承诺的取消和安全遏制。

---

## 10. 成本容量

至少归集：

```text
model tokens and cost
sandbox seconds
worker cpu / memory
postgres storage / io
redis memory
object storage / requests / egress
observability ingestion
audit archive
backup
```

按 Org 运营归因使用 UsageRecord 和受控聚合，不把 `org_id` 作为公共 Metrics 高基数标签。

容量评审同时看单位成本：

- cost / Run；
- cost / 1k tokens；
- cost / Sandbox minute；
- storage growth / 1k Runs；
- observability cost / Run；
- audit archive growth / management operation。

MVP 成本归因不是账单。

---

## 11. 业务影响分析

| 能力 | 最大可接受中断 | 数据损失容忍 | 恢复优先级 |
| --- | --- | --- | --- |
| Auth / Gateway | 4 小时内 | 按已声明 PostgreSQL RPO；运行时不得静默丢失已确认写入 | P1 |
| Runtime 新 Run | 4 小时内 | 已提交 Run 不丢 | P1 |
| 在途 Run / Checkpoint | 4 小时内恢复或明确失败 | 按声明 RPO，外部副作用不能盲重放 | P1 |
| Admin / Studio | 4–8 小时 | IAM / Release 不丢 | P1 / P2 |
| Audit Class A | 不允许静默丢失 | 按已声明 PostgreSQL RPO；运行时不得静默丢失已确认写入 | P1 |
| Usage | 24 小时内恢复摄取 | 按 RPO，可幂等补写 | P2 |
| Console | 24 小时 | 可从事实重建 | P2 |
| Scheduler / IM | 4 小时或受控暂停 | 不重复触发 | P1 / P2 |

最终合同 SLO 可以更严格，届时必须更新架构和成本。

---

## 12. RPO / RTO

### 12.1 MVP 声明

| 数据 / 服务 | RPO | RTO |
| --- | --- | --- |
| PostgreSQL，仅日备 | ≤24 小时 | ≤4 小时 |
| PostgreSQL，启用连续 WAL / PITR | ≤15 分钟 | ≤4 小时 |
| Redis 协调状态 | 不作业务事实 RPO | 从 PG 重建并在 RTO 内 reconcile |
| Agent 制品对象 | 已发布对象不得丢；版本 / 复制按存储能力 | ≤4 小时 |
| Audit 在线 + 归档 | 同 PostgreSQL 保护档：日备 ≤24 小时 / PITR ≤15 分钟 | ≤4 小时恢复写入 / 查询 |
| Secret Store | 按供应商备份与密钥策略 | ≤4 小时或阻止相关能力 |

每个生产环境必须在发布清单声明使用日备档还是 PITR 档，不能只写“支持 PITR”。

“Class A 不静默丢失”是运行时可靠写入约束：业务事务必须与本地 outbox 同事务，不能业务成功后只写普通日志。它不把基础设施灾难 RPO 自动提升为零；需要接近零数据损失的部署必须采用同步复制、PITR 或更严格方案并单独验证。

### 12.2 RTO 起止

```text
start = 事件确认服务不可提供关键能力
end = 单副本安全恢复、关键验证通过并重新开放受控流量
```

完整性能恢复可以晚于 RTO，但不能在未验证隔离、ReleaseRef、Audit 和 Secret 时提前宣布恢复。

---

## 13. 灾难场景

### DR-01 PostgreSQL 不可达

- 停止新 Run 和控制面写；
- 禁止切换 SQLite；
- 评估故障转移或恢复；
- 恢复后校验 Migration、Tenant、Release、Audit；
- 执行 Run reconcile。

### DR-02 PostgreSQL 逻辑损坏

- 冻结写入；
- 保全错误状态和日志；
- 选择损坏前恢复点；
- 在隔离环境恢复并验证；
- 评估从 Audit / ReleaseEvent 重放允许的控制面事实；
- 不自动重放不可证明幂等的外部副作用。

### DR-03 Redis 全丢

- 停止领取新 Run；
- 启动空 Redis；
- 清理旧 ownership 假设；
- 从 PostgreSQL 扫描非终态 Run；
- 重新建立 Stream / lease；
- terminal 状态优先；
- 验证 SSE 重连和 cancel。

### DR-04 对象存储对象缺失

- 停止引用缺失 digest 的新 Run；
- 从版本 / 复制恢复对象；
- 校验 digest；
- 核对 ReleaseChannel 和历史 Run；
- 记录完整性事件；
- 不用文件系统最新版本替代。

### DR-05 Secret Store 不可用

- 新建需要 Secret 的 Run fail-closed 或明确 pending；
- 不从日志、数据库或旧 Pod 抽取明文；
- 恢复 Secret Store 或执行预案轮换；
- 验证引用与访问策略。

### DR-06 区域不可用

MVP 不承诺自动跨区域切换：

- 宣告事件；
- 在备用恢复环境建立网络和 Secret；
- 恢复 PostgreSQL、对象和应用；
- 区域变更需检查数据驻留和供应商配置；
- 按 RTO 恢复单副本受控流量；
- DNS 切换和回切需演练。

### DR-07 错误发布 / Migration

- 停止后续批次；
- 关闭 Feature；
- 判断 Schema 兼容；
- 回滚镜像或前滚修复；
- 不手改 Alembic revision；
- 灾难性数据损坏才从备份恢复；
- Agent 问题使用 Channel rollback。

### DR-08 凭证 / 密钥泄露

- 撤销和轮换；
- 暂停受影响 Connector / Org；
- 保全 Audit 和访问日志；
- 确定数据范围；
- 重新发布工作负载；
- 验证旧凭证无效；
- 必要时通知。

### DR-09 Sandbox / Worker 大规模失陷

- 停止新执行；
- 隔离节点池；
- 撤销工作负载和注入 Secret；
- 保存镜像、Run、网络和 Audit 证据；
- 重建干净节点；
- 按 release digest 和 checkpoint 判断 Run 处理；
- 不自动恢复不确定外部写。

---

## 14. 恢复依赖顺序

```text
Incident Control / Network
→ Secret Store / Workload Identity
→ PostgreSQL
→ Object Store validation
→ Redis empty or trusted state
→ Gateway Profile S
→ Run reconcile
→ Sandbox / Worker
→ Frontend
→ Scheduler / Channel
→ Controlled traffic
→ Full capacity
```

恢复时先 Profile S，不直接恢复到 Profile H / W。

---

## 15. 恢复验证

开放流量前：

- [ ] 应用与 Alembic 版本兼容
- [ ] Org / Membership / RoleBinding 完整
- [ ] OrgA 不可见 OrgB
- [ ] 租户资源无空 `org_id`
- [ ] ReleaseChannel 指向有效 AgentVersion
- [ ] Agent 对象 digest 匹配
- [ ] 新 Run 固定 ReleaseRef
- [ ] legacy unpinned 不执行
- [ ] Audit 强写和查询可用
- [ ] Audit 归档水位 / 摘要抽样一致
- [ ] Secret 引用可解析
- [ ] Redis lease 清理并完成 reconcile
- [ ] 不可证明幂等的外部副作用未自动重放
- [ ] 删除 ledger 已重放
- [ ] 实际数据损失在 RPO 内
- [ ] 恢复时间在 RTO 内

---

## 16. 演练

### 16.1 频率

| 演练 | 频率 |
| --- | --- |
| PostgreSQL 备份恢复 | 每季度；首个生产出口前一次 |
| Redis 全丢 + reconcile | 每季度或 Run 控制重大变更 |
| 对象 digest 恢复 | 每半年 |
| Secret 轮换 / 失效 | 每季度 |
| Profile H 滚动与故障 | 每次声明 HA 前，之后至少季度 |
| 区域恢复桌面演练 | 每半年 |
| Sandbox / Worker 隔离 | 每季度 |
| Break-glass | 每季度 |

### 16.2 记录

```text
exercise_id
scenario
date
participants
environment
backup_point
declared_rpo
declared_rto
actual_data_loss
actual_recovery_time
steps
deviations
security_validation
tenant_validation
release_validation
audit_validation
issues
owners
due_dates
```

演练失败不会通过修改声明数字自动变成成功；必须修复或正式评审目标。

---

## 17. 扩容与架构复审触发

满足任一：

- 资源连续两个窗口超过 warning；
- 预计一个规划周期达到已测稳定容量 70%；
- SLO 预算因容量耗尽快速消耗；
- 单 Org 公平性失败；
- Sandbox /模型供应商限制成为主要排队来源；
- 数据增长使备份或 RTO 超标；
- Console / Audit 查询达到索引与单库上限；
- Gateway 执行负载影响接入；
- Profile H drain / reconcile 不满足；
- 成本 / Run 连续两个版本显著上升。

动作：

1. 确认负载和容量数据；
2. 优先优化泄漏、查询、重试和无界增长；
3. 调整纵向 / 横向资源；
4. 校准限流和公平队列；
5. 评估数据分区、读副本和 Worker 拆分；
6. 重跑基线与 Soak；
7. 更新本文、Runbook、SLO 和发布证据。

---

## 18. Owner 与评审

| 区域 | Owner |
| --- | --- |
| Gateway / SSE | Platform Backend |
| Runtime / Worker | Runtime |
| PostgreSQL | DBA / SRE |
| Redis | SRE / Runtime |
| Sandbox | Platform / Security |
| Model / Tool limits | Runtime / Extension Owner |
| Object / Backup / DR | SRE |
| Capacity test | Performance / Platform |
| Cost | Platform Operations |

容量每月评审；生产发布、重大上游同步和架构变更后复测受影响区域。

---

## 19. 验收

- [ ] Profile S / H / W 声明进入配置、Doctor 和 Dashboard
- [ ] 生产上限来自版本化压测证据
- [ ] 单 Org 公平性和全局背压经过测试
- [ ] PostgreSQL 连接预算和存储增长可计算
- [ ] Redis 无静默 eviction ownership / Stream
- [ ] Sandbox 有有限容量、余量和 quarantine
- [ ] 首要瓶颈和安全稳定容量已记录
- [ ] 初期生产 limit ≤ stable capacity × 50%；经至少两个版本证据后可以上调，但不得超过 stable capacity，且须同步更新 §8 水位和发布记录
- [ ] RPO / RTO 对每个生产环境明确
- [ ] 至少一次完整恢复演练通过
- [ ] 删除 ledger 在旧备份恢复后重放
- [ ] ADR-0006 的拆分触发指标可计算
- [ ] 容量和灾备问题有 Owner 与到期日
