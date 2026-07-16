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
| PR-020 | 控制面 Schema Expand（org / IAM 表） | — | 未开始（Track A 已解锁） | Alembic expand |
| PR-021 | 存量资源 `org_id` Expand | — | 阻塞 → PR-020 | 可空列 + 兼容索引 |
| PR-022 | 默认 Org Bootstrap | — | 阻塞 → PR-020 | 幂等 |
| PR-023 | Backfill Job | — | 阻塞 → PR-022 | dry-run |
| PR-024A–E | Repository Org Scope（按资源拆） | — | 阻塞 → PR-021 | 隔离矩阵 |
| PR-025A–D | Enforce + Multi-org Feature | — | 阻塞 → PR-024 | 不可回滚发布 |

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
| Phase A | 0–30 天 | Track 0（完成）+ Track A + PR-062/063 | Track 0 已交付；**Track A 出口达成**（PR-010 / PR-011 / PR-012 / PR-013 / PR-014A / PR-014C 落地；PR-014B 阻塞 scheduler greenfield，不阻塞 Track B）；下一步 Track B（PR-020） |
| Phase B | 31–60 天 | Track B + C + D + PR-060 | 未开始 |
| Phase C | 61–90 天 | Track E + F（UI/Doctor/Backup/Gate） | 未开始 |

阶段出口验收以 [90-day-mvp.md](../roadmap/90-day-mvp.md) 各 §x.4/§x.5 的 checkbox 为准；本表只跟踪 PR 落地状态，不替代验收清单。

---

## 11. 维护约定

1. **每个 PR 合并后立即更新本表**：状态列 + GitHub PR 号 + 落地 commit（`main` 的 merge commit 短 SHA）。
2. **`已合并未落地` 必须显式标注**：说明目标分支与待 rebase 路径，避免误认为 `main` 已可用。
3. **测试 ID 列**填写该 PR 交付的 test marker（如 `CONTRACT-010-*`、`TEN-001`），与 [testing-strategy.md](testing-strategy.md) §5 的域编号对应。
4. **阻塞列**写明前置 PR 编号，不写模糊的"后续"。
5. 本表不记录实现细节——细节在对应 ADR / 契约文档；本表只回答"做到哪了"。
