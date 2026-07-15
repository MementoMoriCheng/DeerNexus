# DeerNexus 文档索引

本目录是 DeerNexus **文档先行**的权威入口。代码 fork 初始化后，工程实现须与下列文档保持一致；若行为变更，先改文档或补充 ADR。

## 当前文档

| 文档 | 状态 | 用途 |
| --- | --- | --- |
| [项目总览](../README.md) | 当前 | 定位、原则、术语、导航 |
| [目标架构](architecture/target-architecture.md) | MVP 草案 | 分层、边界、数据流、部署 |
| [运行时稳定契约](architecture/runtime-contracts.md) | `v1alpha1` | TenantContext、Policy、Release、Audit、Usage DTO / Protocol |
| [MVP 数据模型](architecture/data-model.md) | 实施草案 | Org、RBAC、Agent 制品、Audit、资源归属 |
| [API 边界](architecture/api-boundaries.md) | MVP 草案 | Runtime、Admin、Studio、Internal API |
| [生产安全基线](security/baseline.md) | MVP 强制 | 身份、隔离、Secret、Sandbox、SSRF、供应链 |
| [威胁模型](security/threat-model.md) | MVP v0.1 | STRIDE、Abuse Case、控制追踪与残余风险 |
| [数据治理](compliance/data-governance.md) | MVP v0.1 | 分类、用途、保留、删除、导出与合规证据 |
| [生产 Runbook](ops/production-runbook.md) | MVP v0.1 | 部署、迁移、备份、恢复、双副本与故障处理 |
| [可观测性与 SLO](ops/observability-and-slo.md) | MVP 规范 | 日志、指标、Trace、告警与错误预算 |
| [容量与灾备](ops/capacity-and-dr.md) | MVP v0.1 | 容量测量、扩容水位、RPO/RTO 与演练 |
| [测试策略](engineering/testing-strategy.md) | MVP 强制 | 隔离、契约、迁移、发布、安全与恢复门禁 |
| [CI/CD](engineering/ci-cd.md) | MVP 规范 | 流水线、制品、环境晋升与迁移门禁 |
| [上游同步](engineering/upstream-sync.md) | MVP 规范 | DeerFlow 基线、同步节奏与分叉度量 |
| [PR 拆分指南](engineering/pr-split-guide.md) | MVP 指南 | Fork、多租户、RBAC、审计与发布的可审查拆分 |
| [90 天 MVP](roadmap/90-day-mvp.md) | 可执行草案 | 分期交付与验收 |
| [ADR-0001](adr/0001-fork-evolution-strategy.md) | Accepted | Fork 渐进演进决策 |
| [ADR-0002](adr/0002-tenant-workspace-keys.md) | Accepted | 租户主键、Workspace 语义与资源归属 |
| [ADR-0003](adr/0003-rbac-and-service-accounts.md) | Accepted | RBAC、服务账号、API Key 与撤权 |
| [ADR-0004](adr/0004-agent-artifacts-and-release.md) | Accepted | 不可变 Agent 制品、通道与回滚 |
| [ADR-0005](adr/0005-audit-event.md) | Accepted | AuditEvent、可靠写入、防改写与保留 |
| [ADR-0006](adr/0006-gateway-worker-split.md) | Accepted | Gateway / Worker 物理拆分条件与边界 |

## 后续文档方向

- Fork 初始化后的真实命令、路径、Owner、Dashboard 和容量实测记录
- `security/threat-model.md` 随新 Tool / MCP / Worker / 数据流持续更新
- 知识库、Approval、Quota / Billing、Registry 启动前新增独立架构与安全 ADR
- 具体行业合规映射由适用客户、部署地区和合同范围决定

## 文档状态约定

- **Accepted**：架构决策已接受；实现变更必须引用对应 ADR。
- **MVP / 实施草案**：可指导首版实现，Fork 后须按真实上游结构校准。
- **`v1alpha1`**：字段契约可演进，但生产者、消费者和文档必须同 PR 更新。
- 代码仓初始化后，每份实施文档补充 Owner、Reviewers、Last reviewed 和对应实现路径。

## 贡献约定

1. 新增架构决策使用递增 ADR，状态为 Proposed → Accepted / Rejected / Superseded。  
2. 修改 MVP 必做项时同步更新验收表与非目标表。  
3. 禁止在未更新 ADR 的情况下变更「Fork 不重写内核」原则。
