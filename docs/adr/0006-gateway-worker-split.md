# ADR-0006：Gateway / Worker 物理拆分条件

- **状态**：Accepted
- **日期**：2026-07-15
- **决策者**：DeerNexus 项目组
- **关联**：[ADR-0001](0001-fork-evolution-strategy.md) · [运行时契约](../architecture/runtime-contracts.md) · [目标架构](../architecture/target-architecture.md) · [生产 Runbook](../ops/production-runbook.md) · [容量与灾备](../ops/capacity-and-dr.md)

---

## 1. 背景

DeerFlow 当前 Gateway 可以内嵌执行 Agent Run。DeerNexus 目标架构保留独立 Worker 的演进方向，但 90 天 MVP 明确不把物理拆分作为验收项。

过早拆分会引入队列、租约、重复投递、取消传播、SSE 路由、Secret 下发和独立部署复杂度；完全不定义拆分边界，又会让 Gateway 在规模增长后同时承担接入、控制面和高成本执行，形成故障域与扩缩容耦合。

本 ADR 决定：

1. MVP 中 Worker 的逻辑职责；
2. 何时必须启动物理拆分；
3. 拆分前必须具备哪些运行时语义；
4. 拆分后 Gateway、Worker、PostgreSQL 和 Redis 的权威关系；
5. 如何灰度和回滚。

---

## 2. 决策

### 2.1 MVP 默认不物理拆分

90 天 MVP 默认使用：

```text
Gateway Process
  ├── Runtime / Admin / Studio API
  ├── Run admission
  ├── Logical execution dispatcher
  └── In-process logical worker
```

约束：

- 即使同进程，也必须通过 `RunEnvelope`、`ReleaseRef`、`PolicySnapshotRef` 和稳定 Run 状态机表达执行边界；
- Gateway Router 不直接调用 Agent 内部对象，必须进入 Runtime Application Service；
- 内嵌 Worker 与未来远程 Worker 使用同一 contracts；
- Profile S 明确 `replicas=1`，不宣称 HA；
- 满足 Profile H 一致性门槛后，Gateway 可以多副本但仍内嵌执行，不等同独立 Worker。

Profile 命名统一为：

- **Profile S**：Gateway 单副本，逻辑 Worker 内嵌；
- **Profile H**：Gateway 多副本，逻辑 Worker可内嵌，已通过 lease / cancel / reconcile / rolling 验收；
- **Profile W**：Gateway 与 Worker 物理拆分；仍需声明 Gateway 自身采用 S 或 H 接入形态。

### 2.2 采用“证据触发”而非日期触发

物理拆分不以“进入第二季度”或代码目录完成为依据。满足任一硬触发条件，或连续两个观测窗口满足两项普通触发条件时，必须启动 ADR 实施复审和拆分计划。

#### 硬触发

1. **安全边界**：执行环境需要与 Gateway 身份、网络或 Secret 权限形成独立工作负载边界；
2. **故障隔离**：Agent / Tool / Sandbox 故障能够反复拖垮 Gateway 接入可用性，且资源限制无法充分隔离；
3. **合规要求**：特定数据或客户要求执行工作负载进入独立节点池、账号或网络域；
4. **独立伸缩**：执行需求必须按 GPU、CPU、内存或 Sandbox 容量独立伸缩，Gateway 副本无法合理承载；
5. **独立发布**：Runtime 执行代码与 Gateway 的发布频率、风险窗口或回滚需求长期冲突。

#### 普通触发

1. Run 执行 CPU / 内存持续挤压 Gateway，导致 Gateway 或 Run admission SLO 连续两个窗口超标；
2. 增加 Gateway 副本主要是为了承载执行，而非 API 流量；
3. 单个部署单元的 drain 时间持续超过允许发布窗口；
4. Sandbox acquire / 模型并发和 API 接入需要不同背压策略；
5. Profile H 故障演练中，执行故障多次扩大为整个 Gateway 故障；
6. 容量模型显示未来一个规划周期内内嵌模式将超过已测上限的 70%；
7. Gateway 与执行模块的 Owner、发布或值班责任已稳定分离。

阈值和观测窗口由[容量与灾备](../ops/capacity-and-dr.md)记录，以真实压测和生产指标为准，不在缺少 Fork 与基线时虚构固定 RPS。

---

## 3. 拆分前置条件

以下条件全部满足前，不得把远程 Worker 用于生产：

### 3.1 数据与状态

- PostgreSQL 是 Run、Thread、ReleaseRef 和 terminal 状态的权威源；
- Redis 只承担 Stream、lease、ownership 和协调，不是已完成业务事实唯一来源；
- Run 状态转换使用 CAS / row version；
- terminal 状态不可回退；
- Checkpoint 包含 Org namespace，并可验证 Run / Thread / Org 一致；
- `legacy_unpinned` Run 不进入远程执行。

### 3.2 投递

- `RunEnvelope` Schema 有生产者 / 消费者契约测试；
- 跨信任边界投递必须验证 `EnvelopeIntegrity`；
- 投递语义明确为 **至少一次**；
- 不宣称端到端 exactly-once；
- 创建幂等使用 `org_id + principal_id + endpoint + idempotency_key` 的 API 作用域，并由数据库 `UNIQUE(org_id, idempotency_key)` 或等价约束兜底；
- Worker 消费按 `run_id` 检查已存在 Run、有效 owner 和 terminal 状态，禁止二次执行，而不是用 `run_id + idempotency_key` 重新定义创建幂等；
- Worker claim、heartbeat、lease expiry、reconcile 已实现；
- Dead letter / poison message 有人工处理与告警。

### 3.3 副作用

- Tool / MCP 声明幂等性和风险等级；
- 不可证明幂等的外部写操作在 Worker 故障后不自动重放；
- 模型调用和 UsageRecord 使用 attempt 区分重试；
- Sandbox 创建与回收可幂等；
- 审计和用量投递按 event id 幂等。

### 3.4 控制

- cancel / resume 能跨进程传播；
- Worker 执行前重新验证 Principal、Org 状态和 Run 可执行状态；
- 高风险 Tool 每次实时 Policy 评估；
- Worker 只能获取当前 Run 所需最小 Secret；
- Worker 不拥有 Organization、RoleBinding、API Key 等控制面管理权限。

### 3.5 可观测

- Gateway 与 Worker Trace 可通过 request_id / trace_id / run_id 串联；
- 具备 queue lag、claim、lease、heartbeat、reconcile、terminal convergence 指标；
- Worker 版本、RunEnvelope 版本和 release digest 可查询；
- 告警与 Runbook 已演练；
- 完成至少 24 小时目标负载 Soak。

---

## 4. 拆分后的职责

### 4.1 Gateway

- 认证、TenantContext、RBAC；
- Runtime / Admin / Studio API；
- Run admission、Policy admission；
- 解析并固定 ReleaseRef；
- 创建 Run 与 RunEnvelope；
- SSE 接入和授权；
- cancel / resume 命令受理；
- Console / Audit 查询；
- Worker 不可用时的背压与明确失败。

Gateway 不执行高成本 Agent 图和 Sandbox 工作负载。

### 4.2 Worker

- 领取已提交 Run；
- 验证 RunEnvelope、Org、ReleaseRef 和可执行状态；
- 执行 Agent 图、模型、Tool / MCP、Sandbox；
- 续租、Checkpoint、Usage 和运行事件；
- 响应 cancel；
- 通过 CAS 写状态；
- 释放 Sandbox 和临时凭证。

Worker 不创建 Organization、Role、ServiceAccount、API Key、Agent 发布或 Policy 定义。

### 4.3 PostgreSQL

权威保存：

- Run / Thread；
- ReleaseRef / policy_version；
- Checkpoint / Store（或其权威元数据）；
- terminal 状态；
- Audit outbox；
- UsageRecord；
- Worker 可恢复所需的业务事实。

### 4.4 Redis / Queue

保存：

- 待领取信号或 Stream；
- ownership lease / heartbeat；
- SSE StreamBridge；
- cancel 通知；
- 短期协调状态。

Redis 丢失后必须能根据 PostgreSQL 执行 reconcile。不得用旧 Redis 状态覆盖更新的 PostgreSQL terminal 状态。

---

## 5. 投递与一致性

### 5.1 Run 创建

```text
authenticate / tenant / RBAC
→ resolve ReleaseRef
→ policy admission
→ transaction: persist Run + outbox/dispatch record
→ publish dispatch signal
→ Worker claim
```

数据库事务成功但 publish 失败时，由 dispatcher 重试；publish 成功但 Worker 未处理时，由 Stream / lease 恢复。不得先投递、后创建 Run。

### 5.2 Worker Claim

Claim 必须原子完成并记录：

```text
run_id
worker_id
lease_token
lease_expires_at
worker_version
claimed_at
```

续租只能由当前 `lease_token` 完成。旧 Owner 在 lease 失效后不得覆盖新 Owner 或 terminal 状态。

### 5.3 完成

```text
execute
→ persist checkpoint / outcome
→ CAS terminal state
→ append run event / audit / usage
→ release lease
```

Lease 释放失败不能把已提交 terminal Run 重新执行。Reconciler 先读 PostgreSQL terminal 状态。

### 5.4 Cancel

- Gateway 持久化 cancel intent；
- 发送 Redis / Queue 通知作为加速；
- Worker 每个安全点检查 cancel；
- 通知丢失时 Worker 仍从持久化状态看到 intent；
- cancel 与 completion 竞争由 CAS 决定；
- 不可中断外部副作用必须进入明确结果或人工处理。

---

## 6. 安全边界

- Gateway 与 Worker 使用不同工作负载身份；
- Worker 数据库角色只具备执行所需最小表权限；
- Worker 无 IAM / Organization / API Key 管理写权限；
- Internal API 不对公网开放；
- 跨主机使用 mTLS、短期工作负载身份或等价机制；
- RunEnvelope 通过消息队列时验证完整性；
- Queue 按环境隔离，消息带 Org 但不带 Secret；
- Worker 网络默认不能访问控制面管理接口；
- Worker Secret 按 Run / Connector 最小注入并短期有效；
- Worker 日志遵循与 Gateway 相同的脱敏和租户隔离要求。

---

## 7. 容量与调度

拆分不是无限扩容：

- Gateway 和 Worker 分别配置并发、连接池和资源上限；
- 每 Org 并发与公平调度在 dispatch / claim 层执行；
- Worker Pool 可按隔离等级、模型 / GPU、Sandbox 类型和网络策略分组；
- 调度条件来自受控元数据，不接受用户任意指定节点；
- Worker 过载时 Run 保持明确 pending，不伪报 running；
- Queue lag 超阈值时 Gateway admission 背压；
- 单 Org 不能占满全局 Worker；
- 扩缩容不得在 lease TTL 内频繁抖动。

---

## 8. 灰度步骤

### Phase 0：同进程接口化

- 提取 Dispatcher / Executor Protocol；
- 内嵌执行仍走 RunEnvelope；
- 建立 contracts 和状态机测试；
- 不改变部署拓扑。

### Phase 1：影子验证

- 远程 Worker 只消费合成或 staging Run；
- 对比状态、事件、Usage、Audit 与在途恢复；
- 不复制生产外部副作用。

### Phase 2：受控租户 / 任务类型

- 通过服务端 Feature Flag 选择一小组内部 Org；
- 只选择幂等、低风险任务；
- Gateway 保留内嵌回退路径，但同一 Run 只能选择一个 Executor；
- 持续比较 SLO 和一致性。

### Phase 3：生产默认

- 扩大任务类型和租户；
- 停止新 Run 进入内嵌执行；
- 保留只读历史兼容和紧急回滚窗口；
- 完成稳定窗口后删除双写 / 双路由临时代码。

---

## 9. 回滚

回滚原则：

- 回滚只影响新 Run 的 Executor 选择；
- 已被远程 Worker claim 的 Run不迁回内嵌 Worker，除非 lease 失效并通过 reconcile；
- 关闭远程 dispatch 后先 drain Worker；
- 保留 PostgreSQL Run、Checkpoint、ReleaseRef 和 Audit；
- 不清空 Redis 后强行重放；
- 远程 Worker 故障期间不可证明幂等的外部副作用进入人工处理；
- 回滚必须记录受影响 Run、原因、版本和结果。

如拆分引入了不兼容 Schema，必须按 expand / contract 兼容窗口回滚，不得运行旧 Gateway 连接不兼容数据库。

---

## 10. 备选方案

### A. MVP 立即拆分（拒绝）

优点：部署边界早期清晰。  
缺点：在租户、ReleaseRef、审计和一致性契约尚未实现时同时引入分布式复杂度，显著增加 90 天失败风险。

### B. 永久内嵌（拒绝）

优点：实现简单。  
缺点：无法满足独立安全边界、伸缩、故障隔离和专用执行资源需求。

### C. 证据触发、契约先行（采纳）

保留 MVP 交付速度，同时确保未来拆分不重写执行语义。

---

## 11. 验收

### MVP 同进程阶段

- [ ] Router 不直接调用 Agent 内部对象
- [ ] 内嵌执行使用 RunEnvelope 与稳定 contracts
- [ ] Profile S / H 声明和生产配置一致
- [ ] 拆分触发指标可从容量和观测文档追踪

### 物理拆分前

- [ ] §3 全部前置条件满足
- [ ] 至少一次生产等价故障注入
- [ ] 24 小时目标负载 Soak 通过
- [ ] Security Review 覆盖身份、网络、Secret 和 Queue
- [ ] 数据库与消息投递事务间隙可恢复
- [ ] 至少一次拆分灰度回滚演练

### 物理拆分后

- [ ] Gateway SLO 不受执行高负载拖累
- [ ] 只有一个有效 Worker Owner
- [ ] cancel 与 terminal convergence 满足 Runbook SLO
- [ ] Redis 丢失后可由 PostgreSQL reconcile
- [ ] 跨 Org、ReleaseRef、Audit 和 Usage 测试全绿
- [ ] Worker 无控制面管理权限

---

## 12. 后果

### 正向

- 避免为了目录完整而过早分布式化；
- 拆分时复用已验证的运行时契约；
- 触发条件可度量、可复审；
- Gateway 与 Worker 的安全、故障和伸缩责任明确。

### 代价

- 同进程阶段也需维护 Dispatcher / Executor 抽象；
- 至少一次投递要求所有副作用显式处理幂等；
- 物理拆分前需要完成较强的一致性、观测和恢复门槛。

---

## 13. 非目标

- 在本 ADR 中选择具体消息队列产品；
- 承诺端到端 exactly-once；
- 以 Worker 拆分替代 Sandbox 隔离；
- 在 90 天 MVP 强制实现独立 Worker；
- 将控制面管理逻辑下放 Worker；
- 自动多区域调度。
