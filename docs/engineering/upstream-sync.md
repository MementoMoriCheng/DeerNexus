# DeerNexus / DeerFlow 上游同步规范

> 状态：MVP Fork 治理规范  
> 关联：[ADR-0001](../adr/0001-fork-evolution-strategy.md) · [CI/CD](ci-cd.md) · [测试策略](testing-strategy.md) · [安全基线](../security/baseline.md)

本文定义 DeerNexus Fork 初始化后的上游基线、同步分支、补丁分类、冲突处理、测试、许可证和分叉度量。目标是在保留 DeerFlow Runtime Kernel 价值的同时，避免企业控制面改动把 harness 演变为不可同步的私有重写。

---

## 1. 远程与基线

初始化后：

```text
origin    → DeerNexus repository
upstream  → official DeerFlow repository（read-only）
```

必须记录：

```text
upstream_repository
upstream_tag
upstream_commit
synced_at
sync_pr
deernexus_commit
```

建议存入：

```text
UPSTREAM_BASE
docs/engineering/upstream-history.md
```

`UPSTREAM_BASE` 是机器可读基线，不使用“最近 main”作为生产依赖。

---

## 2. 分支

```text
main
upstream-sync/<yyyy-mm-dd>-<short-sha>
security/<cve-or-advisory>
```

规则：

- upstream-sync 从 DeerNexus main 创建；
- 把目标 upstream commit 合入该分支；
- 不在 main 上直接解决冲突；
- 不对已共享的 main / sync 分支 force push；
- 同步 PR 单独提交，不混入无关产品功能；
- 必要适配作为同步 PR 内清晰独立 commit；
- Sync PR 合并后更新 UPSTREAM_BASE。

---

## 3. 同步节奏

| 类型 | 节奏 / SLA |
| --- | --- |
| 常规上游检查 | 每周 |
| 常规同步 PR | 至少每两周或每个上游稳定版本 |
| Critical，已利用或外网可达 | 24 小时内缓解，72 小时内修复或隔离 |
| Critical，未确认利用且非外网可达 | 72 小时内修复或隔离 |
| High 安全修复 | 7 天内 |
| 重大上游 Runtime 版本 | 先兼容评估，再建立专用 Sync PR |

安全 SLA 的起算点、严重度与豁免以[安全基线 §13.3](../security/baseline.md#133-修复-sla)为准。如果连续 30 天未同步，必须记录原因、风险和下一日期。

---

## 4. 上游变更分类

### A. 安全修复

- 身份、依赖、Sandbox、Tool / MCP、SSRF；
- 优先最高；
- 可以 Cherry-pick 最小补丁临时缓解；
- 后续仍需与正式上游版本重新对齐；
- 不能跳过目标漏洞、隔离和边界测试。

### B. Runtime 缺陷

- RunManager、Middleware、StreamBridge、Checkpoint、Sandbox；
- 优先合入；
- 企业适配通过 contracts / adapter 保持；
- 不复制第二套执行栈。

### C. 上游新能力

- 评估产品价值、依赖、迁移和风险；
- 默认不自动打开 Feature；
- 先在 dev / staging；
- 需要企业租户语义时通过适配层接入。

### D. 界面 / API 变化

- 检查 Compatibility API；
- 更新 OpenAPI 和前端；
- 不让兼容路由绕过 TenantContext / RBAC；
- 破坏性变化有迁移期。

### E. 与企业控制面冲突

- 优先在 `app/` 或 adapter 解决；
- harness 只保留最小契约钩子；
- 需要长期改 harness 时登记私有补丁；
- 重复冲突触发架构复审。

---

## 5. 私有补丁分类

每个仅 DeerNexus 存在的 harness 补丁登记：

```text
patch_id
path
owner
reason
linked_adr
introduced_commit
upstream_issue_or_pr?
expected_lifetime
conflict_history
removal_condition
```

类型：

| 类型 | 处理 |
| --- | --- |
| Contract Hook | 可保留，但保持最小 |
| 企业业务规则 | 移出 harness |
| 上游缺陷临时修复 | 尽量贡献上游并跟踪移除 |
| 安全加固 | 评估贡献上游 |
| Compatibility Adapter | 优先放 app / adapter |
| 重构 / 命名偏好 | 避免，除非上游接受 |

禁止仅为了 DeerNexus 目录美观大规模移动上游 Runtime 文件。

---

## 6. 同步流程

### 6.1 准备

1. 确认工作区干净；
2. 获取 upstream tags / commits；
3. 阅读目标范围 Release Notes / Diff；
4. 检查安全公告和依赖变化；
5. 建立 upstream-sync 分支；
6. 记录旧 / 新基线；
7. 生成变更分类清单。

### 6.2 合入

1. 合并目标 upstream commit；
2. 不使用丢失上游历史的复制粘贴；
3. 按模块解决冲突；
4. 保留上游行为，企业差异通过 adapter；
5. 对每个冲突记录选择理由；
6. 更新依赖锁、Migration、OpenAPI、配置；
7. 更新 UPSTREAM_BASE。

### 6.3 验证

```text
upstream native tests
→ lint / type
→ harness boundary
→ runtime contracts
→ tenant isolation core
→ PostgreSQL / Redis integration
→ Run / SSE / cancel / resume
→ release / audit
→ sandbox / SSRF
→ frontend build
→ E2E
```

如影响 Profile H：

- lease；
- heartbeat；
- reconcile；
- rolling restart；
- SSE reconnect；
- 24 小时 Soak（生产 HA 发布前）。

### 6.4 合并

- Sync PR 由 Runtime Owner + Control Plane / Security（按影响）Review；
- CI 全绿；
- 发布说明列出用户影响；
- 合并后 staging；
- 更新分叉指标；
- 删除临时兼容补丁；
- 记录下一同步目标。

---

## 7. 冲突处理原则

优先顺序：

1. 安全和租户隔离；
2. runtime-contracts；
3. 上游 Runtime 行为与测试；
4. DeerNexus 企业 Adapter；
5. UI / 命名偏好。

典型冲突：

### 7.1 Harness 依赖

如果上游改变入口：

- 更新 contracts adapter；
- 不让 harness 导入 app.control_plane；
- 更新 boundary test。

### 7.2 Run 状态

- 映射到数据模型语义状态；
- 不静默改变 terminal、cancel、approval / clarification；
- Migration 和在途 Run 兼容。

### 7.3 Checkpoint / Store

- 保留 Org namespace / org_id；
- 验证旧数据可读；
- 不允许上游全局 thread_id 查询绕过 Org。

### 7.4 Auth / Router

- 上游新 Router 进入统一认证、TenantContext、RBAC；
- Compatibility 明确；
- 默认拒绝未分类端点。

### 7.5 File Agent / Skill

- 保留开发态能力；
- prod 仍只执行 ReleaseRef；
- 不恢复磁盘最新版本为生产权威。

---

## 8. 不能自动接受的上游变化

- 恢复生产 host bash 默认；
- 放宽 Auth / CORS / CSRF；
- 新增无 TenantContext 的资源查询；
- 把外部 workspace_id 当平台主键；
- 使 published 制品可修改；
- 跳过 Policy / Audit；
- 把 Redis 变为唯一 Run 事实源；
- 删除 Org namespace；
- 使用明文 Secret；
- 引入 incompatible Migration 无阶段兼容；
- 改变 Run 状态机而无迁移。

发现时停止同步，建立架构 / 安全决策。

---

## 9. 上游贡献

优先贡献：

- 通用 Runtime bug fix；
- Security hardening；
- Contracts 无业务语义的扩展点；
- Sandbox 改进；
- 测试与文档；
- 无 DeerNexus 品牌 / 企业私有模型的适配抽象。

不直接贡献：

- 客户专有逻辑；
- 内部 URL、Secret、环境配置；
- 未经授权的企业控制面代码；
- 与上游方向不符的大规模重写。

上游 PR 合并后：

- 在下一 Sync 移除本地重复补丁；
- 运行完整回归；
- 更新 patch registry。

---

## 10. 安全应急

### 10.1 流程

1. 确认受影响版本；
2. 评估可利用性和暴露面；
3. 先采取配置 / 网络 / Feature 缓解；
4. 合入上游补丁或最小 Cherry-pick；
5. 运行目标漏洞、隔离、boundary、Runtime 回归；
6. 发布；
7. 记录证据；
8. 后续与正式 upstream 基线归并。

### 10.2 禁止

- 因时间紧跳过双 Org 隔离；
- 关闭 Audit；
- 使用 force push 重写生产历史；
- 只升级依赖不验证上游 API；
- 永久保留无跟踪的临时补丁。

---

## 11. 许可证与归属

- 保留 DeerFlow MIT License；
- 保留上游版权与 NOTICE；
- 新增第三方依赖更新许可证清单；
- 上游文件修改不删除原版权；
- 发布源码 / 镜像附带要求的许可证；
- SBOM 包含直接和传递依赖；
- 不把上游商标归属表述为 DeerNexus 自有。

许可证检查进入 Sync PR。

---

## 12. 分叉度量

每次 Sync：

```text
days_since_last_sync
upstream_commits_behind
sync_lead_time
conflict_files
conflict_resolution_hours
harness_private_patch_count
harness_private_added_deleted_lines
private_patch_oldest_age
upstream_security_patch_age
upstream_prs_open / merged
```

`upstream_security_patch_age` 从“可利用漏洞确认或供应商修复可用”起，计算到生产缓解生效的日历时间；同时记录最终修复 / 隔离时间，并按安全基线 §13.3 的 Critical / High 档位判断是否超期。

季度复审触发条件（满足任一）：

- 连续两个同步周期冲突处理超过 3 个工程日；
- harness 私有补丁超过 2,000 行或占 harness 变更量 10%；
- 同一模块连续三次产生语义冲突；
- 安全补丁多次超过 SLA；
- 为企业能力维护第二套 Run / Middleware / Sandbox 栈；
- 上游升级被阻塞超过 60 天。

触发后评估：

1. 私有规则移出 harness；
2. 增强 Adapter / Protocol；
3. 向上游贡献扩展点；
4. 降低同步频率但加强安全 backport；
5. 按 ADR-0001 重新评估独立控制面 / 远程 Runtime 方案。

阈值用于触发评审，不自动决定架构分拆。

---

## 13. Sync PR 模板

```text
Old upstream:
New upstream:
Upstream release notes:
Security advisories:

Change categories:
- security
- runtime
- API / UI
- dependency
- migration

Conflicts:
- file
- upstream intent
- DeerNexus constraint
- resolution

Private patches:
- retained
- removed
- added

Tests:
- upstream
- boundary
- isolation
- runtime
- release / audit
- sandbox
- E2E

Migration:
Compatibility:
Rollback:
License / SBOM:
Known risks:
```

---

## 14. 回滚

Sync 发布失败：

- 回滚到之前 DeerNexus 镜像 digest；
- 确认数据库 Migration 兼容；
- 关闭新增 Feature；
- 不重写 Git 历史；
- 保留失败 Sync 分支和证据；
- 修复后新 PR；
- Critical 安全补丁不能通过回滚恢复漏洞暴露，需替代缓解。

Agent Channel 不因平台 Sync 自动变化。

---

## 15. Owner

| 事项 | Owner |
| --- | --- |
| 基线与 Sync PR | Runtime Maintainer |
| 企业 Adapter | Control Plane |
| Migration | Backend / DBA |
| Sandbox / Security | Platform Security |
| Frontend Compatibility | Frontend |
| License / SBOM | Platform Engineering |
| 发布与回滚 | SRE |

具体人员写入 CODEOWNERS。

---

## 16. 验收

- [ ] origin / upstream 远程与权限正确
- [ ] UPSTREAM_BASE 记录 tag / commit
- [ ] 至少完成一次 Sync 演练
- [ ] Sync PR 不混入无关功能
- [ ] 上游原生测试与 DeerNexus 关键门禁通过
- [ ] harness boundary 通过
- [ ] 新 Router / Store / Runtime 路径具有 Org 语义
- [ ] 私有补丁有登记和 Owner
- [ ] Critical / High 上游安全补丁满足安全基线 §13.3 的缓解与修复 / 隔离时限
- [ ] License / NOTICE / SBOM 更新
- [ ] 分叉指标可生成
- [ ] 回滚到旧镜像经过验证

---

## 17. Fork 初始化待填

1. 官方 upstream URL；
2. 初始 tag / commit；
3. Branch / Merge 策略；
4. UPSTREAM_BASE 文件格式；
5. Patch Registry 路径；
6. CI Workflow；
7. CODEOWNERS；
8. Release Notes 来源；
9. Security Advisory 订阅；
10. 第一次同步演练日期。
