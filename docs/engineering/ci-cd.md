# DeerNexus CI/CD 与发布门禁

> 状态：MVP 工程规范  
> 关联：[测试策略](testing-strategy.md) · [生产 Runbook](../ops/production-runbook.md) · [安全基线](../security/baseline.md) · [可观测性与 SLO](../ops/observability-and-slo.md) · [ADR-0004](../adr/0004-agent-artifacts-and-release.md)

本文定义 DeerNexus 代码、数据库、镜像、配置和 Agent 制品的流水线。代码 Fork 尚未初始化，因此 Workflow 名和命令待补；门禁、证据、晋升和回滚语义为强制要求。

---

## 1. 发布面

DeerNexus 有三个独立发布面：

| 发布面 | 制品 | 主要回滚 |
| --- | --- | --- |
| 平台应用 | Backend / Frontend / Sandbox 镜像 | 回滚镜像与 Feature Flag |
| 数据库 | Alembic Migration | 兼容回滚、前滚修复或备份恢复 |
| Agent | AgentVersion + ReleaseChannel | Channel rollback |

禁止：

- 用回滚数据库替代 Agent Channel 回滚；
- 用修改已发布 AgentVersion 修复问题；
- 把数据库 contract migration 与应用不可逆变更捆成一步；
- 用 `latest` 作为生产执行身份。

---

## 2. 分支与保护

建议：

```text
main                 # 可发布主干
feature/*            # 短生命周期功能分支
fix/*                # 缺陷
security/*           # 安全修复
upstream-sync/*      # 上游同步
release/*            # 仅在需要稳定窗口时使用
```

`main`：

- 禁止直接 Push；
- 要求 PR；
- 必需检查全绿；
- 至少一名代码 Owner Review；
- 租户 / IAM / Release / Audit / Sandbox 改动要求专项 Owner；
- 禁止跳过保护规则；
- Merge 后产生唯一 commit / build identity；
- 是否 Squash / Merge Commit 在 Fork 初始化时固定，不能混用导致上游历史难追踪。

紧急修复仍需 PR 和审计，只能缩短 Review 路径，不能跳过隔离、Secret、目标漏洞和 ReleaseRef 测试。

---

## 3. PR 门禁

### 3.1 必跑

```text
format / lint
→ type check
→ harness import boundary
→ unit
→ component with PostgreSQL
→ TEN-009 connection-pool tenant reuse
→ contracts / OpenAPI diff
→ tenant isolation core
→ Alembic upgrade
→ SAST / SCA / Secret scan
→ build smoke
```

### 3.2 条件门禁

| 变更路径 / 类型 | 增加门禁 |
| --- | --- |
| Runtime / Run / SSE | PostgreSQL + Redis Integration、cancel / resume |
| Tenant / Repository | 完整受影响资源隔离矩阵 |
| IAM / Auth | RBAC、Session 撤销、API Key、OIDC |
| Agent / Release | digest、状态机、v1→v2→rollback |
| Audit | outbox、权限、归档 Schema |
| Sandbox / Tool / MCP | SSRF、隔离、Secret、风险等级 |
| Migration | 生产规模 dry-run、锁评估、旧 / 新应用兼容 |
| Frontend | type、unit、build、CSP / 不可信内容渲染 |
| Dependency / Base Image | 全量 SCA、SBOM、许可证 |
| Upstream Sync | 上游原生 + DeerNexus 全量关键回归；触及 ownership / StreamBridge / 多副本 SSE 时增加测试策略 §16.2，生产目标为 Profile H 时增加 24 小时 Soak |

### 3.3 PR 描述

必须包含：

```text
why
linked ADR / requirement
changed API / DTO / schema
tenant impact
authn / authz / audit impact
migration and rollback
idempotency / concurrency
observability
test IDs and evidence
feature flag
upstream impact
```

涉及租户资源时必须回答：OrgA 如何证明看不到 OrgB？

---

## 4. Pipeline 层级

§3.1 是 PR 合并的完整阻断集，由 §4.1、§4.2 和受影响的 §3.2 条件门禁共同实现；Fast 分层不允许省略全量 SAST / SCA 或 build smoke。

### 4.1 PR Fast

目标 10 分钟：

- 静态检查；
- Unit；
- Boundary；
- Contract fixture；
- Secret Scan；
- SAST / 全量 SCA；
- build smoke。

### 4.2 PR Integration

目标 25 分钟：

- PostgreSQL；
- Redis（受影响时）；
- Core tenant isolation；
- Migration upgrade；
- Release / Audit / API 受影响套件。

### 4.3 Main / Nightly

- 完整隔离矩阵；
- E2E；
- OIDC；
- Scheduler / IM / Webhook；
- SSRF；
- 双副本故障注入；
- 性能趋势；
- 较长 Soak；
- 上游兼容。

### 4.4 平台 Release

- 全 PR / Main 必需门禁；
- 生产配置真实 Doctor；
- 完整 E2E；
- Migration dry-run；
- 镜像、依赖、SBOM、签名验证；
- [可观测性 §10.3](../ops/observability-and-slo.md#103-release-slo-smoke)的逐项 SLO smoke；
- 备份在 RPO 内；
- 已知豁免未过期；
- 所有 Release Candidate 至少 8 小时 Soak；
- `replicas>1` 时 Profile H + 24 小时 Soak；
- 发布证据可追踪。

### 4.5 Agent Release

- ADR-0004 的 Version 状态、digest、Org 与通道门禁；
- Agent 面变更或最近 7 天无通过证据时执行 v1 → v2 → rollback；
- Promote / rollback 的 CAS、ReleaseEvent 与 AuditEvent；
- prod 只接受 published digest；
- Channel rollback 不触发平台镜像或数据库回滚。

---

## 5. 构建制品

### 5.1 Backend / Frontend / Sandbox

每个制品记录：

```text
artifact_name
version
git_commit
build_id
image_digest
base_image_digest
sbom_ref
scan_ref
signature_ref?
created_at
builder_identity
```

要求：

- 构建环境可重复；
- 依赖使用锁文件；
- 基础镜像固定 digest；
- 镜像以非 root 运行；
- 生产按 digest 部署；
- 生成 SBOM；
- Critical / High 漏洞遵循安全 SLA；
- Registry 写权限仅给 CI Builder；
- 部署身份只读拉取；
- build log 不输出 Secret。

### 5.2 Provenance

MVP 至少保留：

- Git commit；
- Workflow / build definition version；
- Builder identity；
- Input dependency lock digest；
- Output digest；
- SBOM；
- 扫描结果。

完整 SLSA / 公共证明不是 MVP 硬门槛。

---

## 6. 版本

平台应用：

- 使用 SemVer 或清晰的日期 / commit 版本；
- 镜像 tag 只作显示，digest 是部署身份；
- Backend、Frontend、Migration compatibility 写入发布清单。

Agent：

- 由 ADR-0004 管理；
- Agent SemVer 不与平台版本绑定；
- ReleaseRef digest 是运行身份。

Contracts：

- 记录 `v1alpha1` / `v1`；
- 新旧版本并行窗口必须经过消费者契约测试；
- 破坏性变化不得只靠应用同时发布假设。

---

## 7. 环境

至少：

```text
dev
staging
prod
```

要求：

- 独立数据库 / Redis / Secret / Object prefix；
- 生产 Secret 不复制到非生产；
- 配置 Schema 相同，值不同；
- staging 与生产使用同类组件和安全控制；
- dev 可以使用本地简化项，但不能把 host bash 结果作为生产验收；
- prod 变更必须来自流水线，不手工替换镜像；
- 配置漂移定期检查。

Agent dev / staging / prod Channel 与部署环境有关联但不是同一概念；映射显式配置。

---

## 8. Secret 与流水线身份

- CI 使用 OIDC / workload identity 或短期凭证；
- 禁止长期云 Key 存在 Repo Secret，平台不支持短期身份时需安全豁免；
- PR from fork / untrusted context 不获得生产 Secret；
- 构建和部署身份分离；
- 非生产和生产部署身份分离；
- Secret 只传给需要步骤；
- 不把 Secret 写入 cache、artifact、test report；
- 日志自动脱敏不能替代脚本不输出；
- Workflow 权限显式最小化；
- 第三方 Action 固定 commit；
- Environment approval 保护生产 Secret。

---

## 9. 缓存与供应链

CI Cache：

- Key 包含 lockfile digest、平台和工具版本；
- 非可信 PR 不能写生产可复用高信任 cache；
- Cache 内容不能包含 Secret；
- 构建结果最终由 digest 和扫描验证；
- 发现污染可全量失效。

依赖：

- PyPI / npm 源固定或使用受控代理；
- 禁止未固定 Git branch 依赖；
- 检测 typosquatting / dependency confusion；
- 内部包 namespace 受保护；
- 许可证扫描；
- 新增高权限依赖需要 Review。

---

## 10. 数据库 Migration

### 10.1 Gate

每个 Migration：

- 空库 upgrade；
- 上个支持 revision upgrade；
- 生产规模样本 dry-run；
- 锁和时长评估；
- 旧 / 新应用兼容；
- 回滚 / 前滚策略；
- 备份检查；
- 数据校验查询。
- 声明目标阶段：Expand / Backfill / Enforce / Multi-org Active / Contract；
- Doctor 阶段判定与生产 Runbook §5.2 一致。

### 10.2 Expand / Contract

```text
expand
→ deploy compatible app
→ backfill
→ validate
→ feature enable
→ observe
→ contract
```

规则：

- Contract 至少晚一个稳定窗口；
- Contract 前确认旧版本不再运行；
- 不可逆 Migration 明确；
- 不手改 Alembic revision；
- 多组织开启前双 Org 隔离全绿；
- Migration Job 使用专用 Role；
- 普通应用 Role 无 DDL。

### 10.3 CD 顺序

```text
preflight / backup
→ expand migration
→ backend canary
→ backend rolling
→ backfill and validate
→ create non-public validation Org
→ full dual-Org isolation matrix
→ enforce constraints
→ frontend
→ enable multi-org feature
→ post-enable verification
→ observe stable window
→ later contract
```

Multi-org Feature ON 以前一项双 Org 矩阵全绿、空 `org_id=0` 和 Enforce 成功为阻断条件。

---

## 11. Feature Flag

每个高风险 Flag 记录：

```text
name
owner
default
environment
dependencies
enable criteria
rollback behavior
expires_at
```

要求：

- 默认安全；
- Server 端决定，不信前端；
- 多组织 Flag 依赖 migration / isolation；
- Profile H 依赖一致性测试；
- Flag 变化写发布记录；高风险变化审计；
- 临时 Flag 有清理日期；
- 关闭 Flag 不导致无法读取历史数据。

---

## 12. 部署

### 12.1 Profile S

- replicas=1；
- 可使用 Recreate 或受控单副本维护；
- 明确维护窗口；
- 部署前备份和 drain；
- 不宣称 HA。

### 12.2 Profile H

- Rolling Update；
- readiness / draining；
- 停止领取新 lease；
- 等待或释放 ownership；
- PodDisruptionBudget；
- maxUnavailable 符合容量；
- Profile H 故障测试通过；
- 24 小时 Soak；
- Redis / PostgreSQL 共享依赖满足生产要求。

### 12.3 Profile W

- Gateway 与 Worker 物理拆分；
- 必须满足 ADR-0006 全部前置条件；
- 执行测试策略 §16.3 的消息、身份、取消、重放和回滚测试；
- Gateway 仍明确声明接入层采用 Profile S 或 H；
- 先 Shadow，再受控内部 Org，最后切默认；
- 远程 Worker Release Candidate 至少 24 小时目标负载 Soak；
- 生产发布证据包含 Queue / Dispatch、Worker 镜像、contracts version 和回滚结果。

### 12.4 Canary

平台应用可采用小流量 Canary：

- 不按 Org 随机导致同一 Run 跨不兼容版本；
- Contracts 保持兼容；
- 对比 5xx、Run create、SSE、Audit、Reconcile；
- 自动或人工门槛明确；
- Canary 失败立即停止扩容。

MVP 不强制自动 Canary 平台。

---

## 13. 晋升

环境晋升使用同一个镜像 digest：

```text
build once
→ verify in dev
→ promote digest to staging
→ verify
→ promote same digest to prod
```

禁止为 prod 重新构建“相同版本”。

晋升证据：

- 测试；
- 扫描；
- SBOM；
- Migration；
- Doctor；
- Backup；
- SLO smoke；
- 批准人；
- 豁免。

---

## 14. 回滚

### 14.1 应用

- 回滚到已验证镜像 digest；
- 检查数据库兼容；
- Feature Flag 优先止损；
- 不回滚到不理解当前 `org_id` / Contract 的旧版本；
- 保存失败版本证据。

### 14.2 数据库

优先：

1. 关闭 Feature；
2. 回滚应用到兼容版本；
3. 前滚修复；
4. 仅在必要时执行已验证 downgrade；
5. 灾难场景从备份恢复。

### 14.3 Agent

- 使用 ReleaseChannel rollback；
- 不修改 AgentVersion；
- 只影响新 Run；
- 在途 Run 固定原 digest；
- 发布与回滚审计。

---

## 15. 发布证据

```text
release_id
environment
git_commit
upstream_commit
image_digests
sbom_refs
scan_refs
signature_refs
database_revision_before / after
contracts_version
openapi_digest
config_digest
feature_flags
doctor_result
test_report
backup_id
profile
slo_smoke
  gateway_availability_probe
  run_create_probe
  run_admission_latency_probe
  sse_first_business_event_probe
  console_default_query_probe
  redis_stream_lag_probe
agent_channel_before / after?
agent_target_version_id?
agent_release_digest?
agent_release_event_ids[]?
deployer
approver
started_at / completed_at
rollback_target
waivers
```

证据保存于受控位置，不含 Secret。

---

## 16. 失败处理

- Pipeline Fail 默认阻断，不允许无说明 rerun 到偶然通过；
- Flaky 按测试策略治理；
- Registry / Scanner 短时不可用时，生产发布默认阻断；
- Critical 安全修复可使用预批准应急流程；
- 部署失败自动停止后续批次；
- Migration 失败不手工改 revision；
- Audit / 发布证据写入失败时不宣称发布完成；
- 任何跨 Org 回归失败立即停止发布。

---

## 17. Owner

| 区域 | Owner |
| --- | --- |
| Workflow / Runner | Platform Engineering |
| Runtime Tests | Runtime |
| Tenant / IAM | Control Plane + Security |
| Migration | Backend + DBA |
| Image / SBOM / Scan | Platform + Security |
| Deployment / Rollback | SRE |
| Agent Release | Studio + Runtime |
| OpenAPI / Contracts | 对应 API Owner |

具体人和升级渠道在 Fork 初始化后补入 CODEOWNERS 和 Runbook。

---

## 18. 验收

- [ ] main 分支保护和必需检查生效
- [ ] harness boundary 每 PR 必跑
- [ ] Core tenant isolation 每 PR、完整矩阵 Nightly
- [ ] OpenAPI / contracts breaking diff 阻断
- [ ] Migration dry-run 与兼容性门禁
- [ ] Secret / SAST / SCA / Image scan
- [ ] 镜像生成 SBOM 并按 digest 部署
- [ ] Build once，环境晋升同 digest
- [ ] 生产 Secret 只给受保护部署环境
- [ ] prod Doctor、Backup、E2E 与 SLO smoke
- [ ] Profile H 条件门禁
- [ ] 应用、数据库、Agent 三类回滚可区分
- [ ] 发布证据完整且不含 Secret
- [ ] 紧急发布仍保留隔离与目标安全测试

---

## 19. Fork 后实现映射

1. Git Provider 与 Branch Protection；
2. Workflow 文件和 Job 名；
3. Python / Node 包管理命令；
4. 测试命令与 Marker；
5. 镜像 Registry；
6. SBOM / SAST / SCA / Secret / Image Scanner；
7. 签名工具；
8. 部署平台、Chart / Compose / IaC；
9. Environment Protection；
10. Release Evidence 存储；
11. CODEOWNERS；
12. On-call / Approver。
