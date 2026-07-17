# DeerNexus 实施进度

> 状态：单一权威进度表（Single Source of Truth）
> 更新规则：每个 PR 合并或状态变更时，本表同步更新一行；状态以 `origin/main` 实际落地为准，分支合并不视为已交付
> 关联：[PR 拆分指南](pr-split-guide.md)（交付序列定义）· [90 天 MVP](../roadmap/90-day-mvp.md)（阶段验收）· [运行时契约 §16](../architecture/runtime-contracts.md#16-实现状态)（Contracts Track 细节）

本表把 [PR 拆分指南](pr-split-guide.md) 的逻辑 PR 编号映射为真实 GitHub PR、当前状态和验收证据。逻辑编号是交付计划，GitHub PR 号是实际合并记录——两者通过本表对应。

---

## 1. 状态定义

| 状态 | 含义 |
| --- | --- |
| `已交付` | 已合并到 `main`，CI 绿，可部署 |
| `已合并未落地` | 已合并，但目标分支非 `main`（如 stacked PR），需后续 rebase / merge 才进入 `main` |
| `开启中` | PR 已创建，等待 review 或 CI |
| `进行中` | 正在开发，尚无 PR |
| `阻塞` | 有明确前置依赖未满足 |
| `未开始` | 计划内，尚未启动 |

> **重要**：只有 `已交付` 计入 `90-day-mvp.md` 的阶段出口验收。`已合并未落地` 表示代码写完但 `main` 尚不可用。

---

## 2. Track 0：Fork 与基线

| 逻辑 PR | 主题 | GitHub PR | 状态 | 验收证据 / 测试 ID | 落地 commit |
| --- | --- | --- | --- | --- | --- |
| PR-001 | 导入上游基线 DeerFlow v2.0.0 | #1 | 已交付 | `UPSTREAM_BASE` 固定；上游测试可运行 | `5bdb87e7` |
| PR-002 | 基础 CI 与边界测试 | #2 | 已交付 | `test_harness_boundary.py`；lint/type/secret scan 进 CI | `26b1f29c` |
| PR-003 | 生产配置 Schema 与 Doctor 骨架 | #3 | 已交付 | `app/doctor/production.py`；PASS/WARN/FAIL 输出；`test_production_doctor.py` | `201017c8` |

---

## 3. Track A：Contracts 与 TenantContext

| 逻辑 PR | 主题 | GitHub PR | 状态 | 验收证据 / 测试 ID | 落地 commit |
| --- | --- | --- | --- | --- | --- |
| PR-010 | `deerflow.contracts` 基础包（PrincipalRef / TenantContext / ContractError） | #4 | 已交付 | `CONTRACT-010-*`（76 测试）；boundary allow-list | `7fd9467e` |
| PR-011 | Policy / Release / Event 契约 + Protocols | #7 | 已交付 | `CONTRACT-011-*`（114 测试）；gitleaks 清洁 | `1cd85c16` |
| PR-012 | ContextVar 生命周期（bind/get/require/reset）+ `TEN-001`~`TEN-008`（`TEN-009` DB 相关，待 CI 阶段） | #9 | 已交付 | `TEN-001`~`TEN-008`（20 测试）；boundary allow-list 加 `contextvars` | `7a634239` |
| PR-013 | Gateway Tenant 解析适配器（单 Org bootstrap） | #11 | 已交付 | `TEN-入口`（12 测试，TM-001）；`TenantResolutionMiddleware`；fail-closed 503 | `b8108a10` |
| PR-014A | Worker RunEnvelope 重建 + Tenant 绑定（内嵌 Worker） | #13 | 已交付 | `TEN-入口 Worker`（8 测试）；`tenant_rebuild.py`；`run_agent` 防御性 rebind（§5.2 rule 3/4） | `e1d77242` |
| PR-014B | Scheduler 入口 Tenant 传播 | — | 阻塞 → scheduler 模块（greenfield，尚未存在） | — | — |
| PR-014C | Channel / IM dispatch Tenant 直接绑定 | #15 | 已交付 | `TEN-入口 Channel`（10 测试）；`resolve_channel_tenant_context` + `channel_tenant_scope`；`_handle_message` 分发包 scope（§5.2 rule 3/6） | `0def292f` |

**Track A 出口**：PR-010～013 + PR-014A + PR-014C 已交付 —— **Track A 出口达成，Schema Expand（Track B）解锁**。PR-014B（Scheduler）因 scheduler 模块 greenfield 延后，不阻塞 Track B。

---

## 4. Track B：Schema 与迁移

| 逻辑 PR | 主题 | GitHub PR | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| PR-020A | 控制面租户表（organizations / workspaces / external_identities / org_memberships） | #17 | 已交付 | `test_tenant_schema`（14 测试）；`0003_tenant_tables` revision；`safe_create_table`/`safe_create_index` helper | `6d66b028` |
| PR-020B | 控制面 IAM 表（roles / role_bindings / service_accounts / api_keys） | #19 | 已交付 | `test_iam_schema`（14 测试）；`0004_iam_tables` revision；system-template NULL-org CHECK；多态 principal；hash-only api_keys | `70ff2b42` |
| PR-021 | 存量资源 `org_id` Expand | #21 | 已交付 | `test_resource_org_schema`（17 测试）；`0005_resource_org_id` revision；4 表可空 `org_id`（FK RESTRICT）+ 5 兼容索引；`safe_drop_index` helper | `621a09b8` |
| PR-022 | 默认 Org Bootstrap | #23 | 已交付 | `test_default_org_bootstrap`（12 测试）；`deerflow/tenancy/` 子包（4 幂等 helper + 临时 audit event 接口）；两阶段：lifespan 建默认 Org + 系统模板 `org:admin` Role，`/initialize` 建 admin OrgMembership + RoleBinding | `55754503` |
| PR-023 | Backfill Job | #25 | 已交付 | `test_backfill_default_org`（9 测试）；`deerflow/tenancy/backfill.py`（隐式水位 PK 子查询分批 + 校验门）+ `scripts/backfill_default_org.py` CLI（`--dry-run`/`--batch-size`/`--throttle-ms`）；按依赖序回填 4 表 NULL org_id→默认 Org | `a227344d` |
| PR-024 | Repository Org Scope（4 张 Run-lifecycle 资源表） | #27 | 已交付 | `test_org_isolation`（10 测试，`TEN-隔离`）；`contracts/context.py` `resolve_org_id`+`AUTO_ORG` 三态 sentinel；`ThreadMeta`/`Run`/`RunEvent`/`Feedback` 仓储写入盖戳 `org_id`、读取/删除硬过滤（与 `user_id` 并存纵深防御）；`check_access` 跨 Org 恒 deny；`RunRecord.org_id` 经 manager 重试线程化；DI/启动恢复/旧行修复补 `org_id=None`；conftest 绑默认 Org tenant + seed org 行；详见 runtime-contracts §16.13 | `316ed2ab` |
| PR-025A | Enforce `org_id NOT NULL` + `threads_meta` 复合唯一 | #29 | 已交付 | 迁移 `0006_enforce_org_not_null`（链自 0005）：4 表 `org_id` `nullable→False`（batch-alter 保命名 FK RESTRICT）+ `threads_meta` `UNIQUE(org_id, thread_id)`；新增 helper `safe_set_column_nullable`/`safe_create_unique_constraint`/`safe_drop_unique_constraint`；4 ORM 模型同步 `nullable=False`；`test_resource_org_schema` NOT NULL 矩阵 + 复合唯一 + 0006↔0005 round-trip；附带修复 `MemoryRunStore.list_by_thread` 同毫秒 tie-sort（阻塞 CI 的预存 bug）；详见 runtime-contracts §16.15 | `e8b8b37a` |
| PR-025B | Multi-org Feature Flag + 不对外开放验证 Org | #31 | 已交付 | 从零引入 Feature Flag 机制：`tenancy.multi_org.phase` 三态（`disabled`/`validation`/`active`，默认 `disabled`）+ `deerflow/tenancy/feature_flags.py`（ci-cd §11 八字段 frozen registry，`MULTI_ORG_FLAG` 列 Track B 四前置与三启用准则，`current_multi_org_phase()` 读实时 config）；`ensure_validation_org` 幂等 bootstrap（惰性——不建 Membership/RoleBinding）；Gateway lifespan `_ensure_validation_org` 仅 `phase=validation` 种子；`config.example.yaml` 增 `tenancy:` 块，`config_version` 15→16（安全叠加）；请求路径解析器在 B 中**保持单 Org**（可回滚：`phase=disabled`=今天行为）；doctor 阶段探测→C，解析器 Membership 切换→C+，翻 active=操作者 CD 门禁；详见 runtime-contracts §16.17 | `58622d07` |
| PR-025C | Doctor 租户迁移阶段探测 | — | 待合并 | 把 production doctor 的 `tenant.migration_state` 占位换成首个 live-DB check：`app/doctor/tenant_probe.py` 建临时只读 engine（不跑 alembic、不复用全局 engine），count 4 表 NULL `org_id` + `organizations` 行数，读 `current_multi_org_phase()`，按 runbook §5.2 表判 PASS/WARN/FAIL（disabled+多Org / validation+残留NULL / active+残留NULL → FAIL；active+仅1Org → WARN）；DB 连接失败容错为 FAIL 不中断；新增 config-only `check_feature_flag_expiry`（≤30 天 WARN / 过期 FAIL，兑现 ci-cd §11）；`run_production_checks` 加 `extra_checks` 参数保持同步，`_run_production_doctor` 改 async；**不改 `tenant.py`**（doctor 是纯只读观测层）；详见 runtime-contracts §16.19 | — |
| PR-025C+ | 解析器切 Membership-based org 解析 | — | 未开始（PR-025C 已交付 doctor 探测） | 新建 membership-read helper（仓库无此代码）、改 `resolve_tenant_context`/`resolve_channel_tenant_context`、定义多 membership 选择策略、处理可信内部/auth-disabled 主体回退、负向测试、观测；门禁在 `phase` 翻 `active` 前（ci-cd §10.3） |
| PR-025D | Contract 清理 | — | 未开始 | 移除 `user_id` 隔离分支 + 清理临时兼容索引 + org-scope 现有全局唯一（ci-cd §10.2 Contract 至少晚一个稳定窗口） |

---

## 5. Track C：RBAC 与机器身份

| 逻辑 PR | 主题 | GitHub PR | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| PR-030 | 权限注册表与内置角色 | — | 未开始（Track A 已解锁） | 正反向单测 |
| PR-031 | 统一 Authorize Service | — | 阻塞 → PR-030 | 403/404；缓存 |
| PR-032 | Runtime Router 切 RBAC | — | 阻塞 → PR-031 | 按 Thread/Run/Artifact |
| PR-033 | Admin / Studio Router 切 RBAC | — | 阻塞 → PR-031 | — |
| PR-034 | ServiceAccount | — | 阻塞 → PR-030 | 生命周期 |
| PR-035 | API Key | — | 阻塞 → PR-034 | 明文只返回一次 |
| PR-036 | OIDC Group Mapping | — | 阻塞 → PR-035 | dry-run |
| PR-037 | 撤权与 SSE | — | 阻塞 → PR-036 | TTL≤60s |

---

## 6. Track D：Audit

| 逻辑 PR | 主题 | GitHub PR | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| PR-040 | Audit Schema 与数据库权限 | — | 阻塞 → Track B | append-only Role |
| PR-041 | Audit Sink / Outbox Worker | — | 阻塞 → PR-040 | 幂等 / dead letter |
| PR-042 | Class A IAM / Org Audit | — | 阻塞 → PR-041 / Track C | 同事务 |
| PR-043 | Catalog / Connector / Release Audit | — | 阻塞 → PR-041 / Track E | — |
| PR-044 | Runtime Security Audit | — | 阻塞 → PR-041 | login / deny / sandbox |
| PR-045 | Audit Query / Archive | — | 阻塞 → PR-042 | Org 查询 / 保留 |

---

## 7. Track E：Agent 制品与 Release

| 逻辑 PR | 主题 | GitHub PR | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| PR-050 | AgentPackage / AgentVersion Schema | — | 阻塞 → Track B | 不可变 digest |
| PR-051 | 文件态导入 | — | 阻塞 → PR-050 | 幂等 |
| PR-052 | 制品存储与对账 | — | 阻塞 → PR-050 | inventory |
| PR-053 | ReleaseChannel / ReleaseEvent | — | 阻塞 → PR-050 | CAS / `NULLS NOT DISTINCT` |
| PR-054 | Release Resolve | — | 阻塞 → PR-053 | prod 门禁 |
| PR-055 | Promote / Rollback API | — | 阻塞 → PR-054 | If-Match |
| PR-056 | Legacy Run 门禁 | — | 阻塞 → PR-054 | 409 `release_unpinned` |
| PR-057 | Studio / Admin 最小 UI | — | 阻塞 → PR-055 | API 稳定后 |

---

## 8. Track F：Console、观测与生产

| 逻辑 PR | 主题 | GitHub PR | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| PR-060 | Org Console API | — | 阻塞 → Track B/C | stats / runs / usage |
| PR-061 | Admin Console UI | — | 阻塞 → PR-060 | 不扩审批/市场/KB |
| PR-062 | 结构化日志与 Trace | — | 未开始 | 可与 Track A 并行 |
| PR-063 | Metrics / Dashboard / Alerts | — | 未开始 | 可并行 |
| PR-064 | Doctor 完整检查 | — | 阻塞 → 各 Track | 接入真实依赖 |
| PR-065 | Backup / Restore Automation | — | 未开始 | 不混业务迁移 |
| PR-066 | CI/CD Release Gate | — | 阻塞 → 各 Track | SBOM / 签名 |

---

## 9. Track G：Run 控制与 Worker

| 逻辑 PR | 主题 | GitHub PR | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| PR-070 | Run 状态 CAS | — | 未开始 | terminal / cancel / resume |
| PR-071 | Ownership / Lease | — | 阻塞 → PR-070 | Redis key |
| PR-072 | Reconciler | — | 阻塞 → PR-071 | 过期 owner |
| PR-073 | SSE 跨副本恢复 | — | 阻塞 → PR-071 | StreamBridge |
| PR-074 | Profile H 门禁 | — | 阻塞 → PR-073 | 24h soak |
| PR-075 | Dispatcher / Executor Protocol | — | 未开始 | 同进程接口化 |
| PR-076+ | 物理 Worker | — | 阻塞 → ADR-0006 触发 | 独立计划 |

---

## 10. 阶段映射

| 阶段 | 窗口 | 对应 Track / PR | 进度 |
| --- | --- | --- | --- |
| Phase A | 0–30 天 | Track 0（完成）+ Track A + PR-062/063 | Track 0 已交付；**Track A 出口达成**（PR-010 / PR-011 / PR-012 / PR-013 / PR-014A / PR-014C 落地；PR-014B 阻塞 scheduler greenfield，不阻塞 Track B）；Track B 进行中（PR-020A / PR-020B / PR-021 / PR-022 / PR-023 / PR-024 落地，下一步 PR-025 Enforce） |
| Phase B | 31–60 天 | Track B + C + D + PR-060 | 进行中（PR-020 全部交付、PR-021/PR-022/PR-023/PR-024 落地） |
| Phase C | 61–90 天 | Track E + F（UI/Doctor/Backup/Gate） | 未开始 |

阶段出口验收以 [90-day-mvp.md](../roadmap/90-day-mvp.md) 各 §x.4/§x.5 的 checkbox 为准；本表只跟踪 PR 落地状态，不替代验收清单。

---

## 11. 维护约定

1. **每个 PR 合并后立即更新本表**：状态列 + GitHub PR 号 + 落地 commit（`main` 的 merge commit 短 SHA）。
2. **`已合并未落地` 必须显式标注**：说明目标分支与待 rebase 路径，避免误认为 `main` 已可用。
3. **测试 ID 列**填写该 PR 交付的 test marker（如 `CONTRACT-010-*`、`TEN-001`），与 [testing-strategy.md](testing-strategy.md) §5 的域编号对应。
4. **阻塞列**写明前置 PR 编号，不写模糊的"后续"。
5. 本表不记录实现细节——细节在对应 ADR / 契约文档；本表只回答"做到哪了"。
