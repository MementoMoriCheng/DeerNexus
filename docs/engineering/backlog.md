# 待办 / 未交付项清单

本文件汇总 DeerNexus 项目当前的待办、阻塞、未开始与 follow-up 项,按类别组织。
每项标注:**类别** / **阻塞条件** / **所属 ADR/Track** / **详细位置**(progress.md 行号 / ADR § / runtime-contracts §)。

> 本文件是 `progress.md`(PR 落地账本)与各 ADR §15 验收清单的**补充索引**,不替代它们。
> 已交付 PR 的「不在范围」段中点名的 follow-up,如已被独立 PR 吸收或不再相关,应在本文件更新时标注。

最后更新:2026-08-01(PR-075 账本回填后)。

---

## 1. PR 级别:阻塞 / 未开始

### PR-014B — Scheduler 入口 Tenant 传播

| 字段 | 值 |
|------|-----|
| **状态** | 阻塞 |
| **阻塞条件** | scheduler 模块 greenfield(尚未存在) |
| **影响** | 不阻塞 Track B / Track C / Track D / Track E。Track A 出口已达成(PR-014A + PR-014C 交付)。 |
| **位置** | `progress.md` Track A 段 |

### PR-025D — Contract 清理

| 字段 | 值 |
|------|-----|
| **状态** | 未开始 |
| **内容** | 移除 `user_id` 隔离分支 + 清理临时兼容索引(`ix_runs_org_status` / `ix_runs_org_thread_created`)+ org-scope 现有全局唯一改为 Org 作用域 UNIQUE |
| **前置条件** | ci-cd §10.2 Contract 至少晚一个稳定窗口(待生产稳定运行后再清理兼容代码) |
| **位置** | `progress.md` Track B 段 |

### PR-076+ — 物理 Worker 拆分

| 字段 | 值 |
|------|-----|
| **状态** | 阻塞 → ADR-0006 §2.2 触发条件 |
| **范围** | dispatch outbox / queue;worker identity;remote claim;observability;shadow(影子验证);controlled rollout(受控租户/任务类型);cleanup(临时双写路由移除) |
| **前置条件** | ADR-0006 §2.2 触发指标达成(Profile H 运行中执行故障多次扩大为整个 Gateway 故障) + §3 全部前置条件满足 + §11 物理拆分前验收通过 |
| **MVP 关系** | **不在 90 天 MVP scope**(pr-split-guide §12:「只有 ADR-0006 触发和前置条件满足后创建独立计划」) |
| **位置** | `progress.md` Track G 段;`pr-split-guide.md` §12;ADR-0006 §8 Phase 1-3 |

---

## 2. ADR 验收未完成项

### ADR-0004(Agent 制品与发布)— 6/16 未勾

| # | 验收项 | 依赖 / 说明 |
|---|--------|-------------|
| 1 | v1 → v2 → rollback 新 Run digest 正确 | 端到端 E2E 测试(依赖 PR-056 Run-pin 已交付;需 start_run → promote v2 → rollback → 新 Run digest 验证 E2E) |
| 2 | 在途 Run 不随 Channel 变化 | Run-pin 持久化 ReleaseRef 后,在途 Run 不受后续 promote/rollback 影响(需 E2E 验证) |
| 3 | revoked 不能创建新 Run | resolver prod 门禁(release_revoked code)+ Run-pin start_run 调 resolver(需 E2E 验证 revoked version 拒绝) |
| 4 | prod 不读取文件系统草稿 | prod channel 只读 ReleaseRef(已由 PR-054 resolver 保证);需 E2E 验证 prod run 不触文件系统草稿路径 |
| 5 | ReleaseEvent 与 AuditEvent 同事务 / outbox 一致 | Class A 同事务 outbox enqueue 已交付(PR-053);需端到端一致性验证(同事务 commit 或都不 commit) |
| 6 | 对象存储与数据库引用对账 | 依赖真实 S3/MinIO 后端(follow-up);inline backend 同库即匹配 |

> 当前: **10/16 已勾**。剩余 6 项多为端到端验证(依赖 Run-pin E2E 测试)或真实 S3 后端。

### ADR-0005(审计)— 6/12 未勾

| # | 验收项 | 依赖 / 说明 |
|---|--------|-------------|
| 1 | system-admin 查询再次审计 | system-admin 审计查询端点(需独立 API + 审计) |
| 2 | correction 不修改原事件 | 审计 correction 机制(append-only correction,不修改原事件) |
| 3 | 每日归档、摘要和回读验证 | 对象存储归档 Job(依赖 object_storage config / S3 后端) |
| 4 | 365 天保留策略存在且可监控 | 保留策略 + 监控(依赖归档 Job + retention 配置) |
| 5 | 备份恢复后归档水位与摘要抽样一致 | 归档 Job + backup 恢复一致性验证 |
| 6 | Outbox backlog 和 dead letter 告警可触发 | outbox backlog / dead letter Prometheus 告警规则(依赖 ops/observability 配置) |

> 当前: **6/12 已勾**。剩余 6 项依赖归档 Job(S3 后端)/ 保留策略 / ops 告警配置。

### ADR-0006(Gateway/Worker 拆分)— 15/16 未勾

ADR-0006 §11 大部分验收项属于**物理拆分前/后**(PR-076+),不在 90 天 MVP scope。MVP 阶段已交付的相关项:

| 验收项 | 状态 |
|--------|------|
| Profile S / H 声明和生产配置一致 | ✅ 已勾(PR-074) |
| Router 不直接调用 Agent 内部对象 | 待验证(PR-075 Dispatcher/Executor 已接口化) |
| 内嵌执行使用 RunEnvelope 与稳定 contracts | 待验证(PR-075 ExecRequest 轻量封装;完整 RunEnvelope 留 PR-076+) |
| 24h soak 通过 + 生产等价故障注入 | 门禁机制已就位(PR-074);真实执行留 release pipeline |
| 其余(拆分触发指标 / §3 前置 / Security Review / 拆分灰度回滚 / 物理 split 后 SLO…) | PR-076+,out of MVP |

> 当前: **1/16 已勾**(PR-074)。Track G MVP 链(070-075, 077)已全部交付;§11 物理拆分验收留 PR-076+。

---

## 3. Doctor DEFERRED_LIVE_CHECKS(基础设施阻塞)

这些 doctor 探针今天仍为 FAIL 占位(`production.py:DEFERRED_LIVE_CHECKS`),因为代码路径尚未构建:

| 探针 | 阻塞于 | 说明 |
|------|--------|------|
| `oidc.jwks_validation` | Track C(PR-036) | 真 OIDC JWKS URI 连线验证(今天 OIDC config 已支持但 JWKS 实时拉取验证未实现) |
| `sandbox.provisioner_create` | Track E | sandbox provisioner 远程创建验证(provisioner_url 已 config 但远程创建路径未实现) |
| `secret_store.access` | Secret Store provider | 真 Secret Store provider(Vault/GCP SM)访问验证(今天 `references_only=True` 模式) |
| `object_storage.security` | object-storage backend | 真实 S3/MinIO 对象存储后端验证(今天 inline backend) |

---

## 4. 已交付 PR 的 follow-up(「不在范围」点名项)

这些是已交付 PR 在其「不在范围」段明确点名的 follow-up,尚未被独立 PR 吸收:

### Track E(Release)
- **真实 S3/MinIO 对象存储后端**:inline backend 之上(follow-up;解锁 ADR-0004 §15 第 6 项 + ADR-0005 归档 Job)
- **catalog_entries 写入投影**:import/promote 同事务投影进 catalog(follow-up;表 + reader 已就位 PR-054)
- **release_idempotency_records GC/TTL**:replay 记录清理(已由独立 commit `a9db2b5` 交付 GC sweep;TTL 列未加)
- **weak ETag + 多值 If-Match**:RFC 7232 完整 ETag 语义(follow-up;今天支持强 ETag 单引号整数)

### Track G(Runs HA)
- **Redis cancel notify listener 端**:worker 订阅 `deerflow:run:cancel:{run_id}` 即时 set abort_event,延迟 ≤1s(PR-077 只交付 publish 端 + PG 轮询 ≤10s 兜底;listener 是 §5.4 bullet 2 软优化)
- **9 态 rename**:`interrupted`→`cancelled` + 加 `cancelling`/`clarification_required`/`approval_required` 瞬态(PR-070 §16.65 显式 deferred;状态字符串硬编码在 SQL 聚合 + metric 标签 + Admin Console 过滤器)
- **resume 跨进程传播**:ADR §3.4 提及 resume;独立控制路径
- **不可中断外部副作用人工处理**:ADR-0006 §5.4 bullet 6;abort_event 只控制 astream 循环,外部副作用语义边界独立设计

### Track D(Audit)
- **4 个无代码路径的 Class B 审计事件**:`policy.approval.required`(无生产者)/ `sandbox.security_violation.detected`(无 SecurityViolationError 类)/ `runtime.cross_org_mismatch.detected`(tenant.py RuntimeError 点)/ `runtime.run.reconcile_manual`(reconcile 路径)—— 需各自先建代码路径
- **Class B fail-closed(队列满阻塞)**:ADR-0005 §7.2「所有持久化路径均不可写时 fail-closed」;需队列水位/背压状态机,独立 PR
- **真 OIDC code-flow 的 auth.login**:今天只本地密码 login 的 auth.login 审计

---

## 5. 测试 / 环境已知问题

- **Windows 环境测试失败**(非 PR 回归):`test_mcp_file_migration` / `test_mcp_session_pool` / `test_run_manager::test_list_by_thread` / `test_sandbox_search_tools` / `test_skill_permissions` / `test_tool_output_budget_middleware` / `test_wechat_channel` —— Linux CI 全绿,Windows 本地因路径/编码/时序差异失败。
- **flaky `test_channel_repository::test_events_recorded_in_order`**:events 创建过快时 `created_at` 时间戳相同,`order_by(created_at.desc())` 非确定(约 1/5 概率失败)。修复:加二级排序键(如 `id`)或 `created_at` 精度提升。

---

## 维护约定

- **新增 follow-up**:交付 PR 时在「不在范围」段点名后,在本文件对应类别补一行。
- **吸收 follow-up**:当某 follow-up 被独立 PR 交付时,在本文件标注「✅ 已交付(PR #XX)」并移至底部「已解决」归档段。
- **ADR §15 勾选**:ADR 文件中的 `[ ]` → `[x]` 勾选同步反映到本文件第 2 节。
- **本文件不替代 `progress.md`**:PR 落地状态以 `progress.md` 为准;本文件是「尚未交付项」的索引。
