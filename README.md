# DeerNexus

**DeerNexus**是基于 [DeerFlow](https://github.com/bytedance/deer-flow) 渐进演进的 **Enterprise Agent OS**。

它把 DeerFlow 成熟的超级 Agent 运行内核，扩展为企业可治理、可审计、可发布、可计量的 Agent 操作系统：统一编排 Agent、模型、工具、技能、知识与企业系统，并提供组织级身份、策略与运营控制面。

> **命名**：（延续 DeerFlow 血缘）+ （统一编排与治理中枢）+ Nexus（连接中心）。

## 与 DeerFlow 的关系

DeerNexus 采用 **Fork 渐进改造**，不是另起炉灶重写运行时。

| 层 | 策略 |
| --- | --- |
| Agent 运行内核（harness） | **强复用**：LangGraph 图、Middleware 链、RunManager、Subagent、Sandbox、Skills、MCP、Guardrails |
| Gateway / IM / 调度 | **增强**：引入租户上下文、真实 RBAC、审计、配额钩子 |
| 企业控制面 | **新建**：Tenant / Workspace、Catalog、Policy、Approval、Quota、Audit、Release |
| 前端 | **扩展**：保留 `/workspace` 终端工作台；新增 `/admin`、Studio、Registry 等企业管理面 |

完整决策见 [ADR-0001：Fork 渐进演进策略](docs/adr/0001-fork-evolution-strategy.md)。

## 核心原则

1. **运行内核不重写**：企业能力通过 ContextVar、策略钩子与稳定契约注入 harness，禁止 `deerflow` 反向依赖企业控制面业务模块。
2. **控制面与数据面分离**：组织、权限、目录、审批、预算、发布属于控制面；Run 执行、工具调用、沙箱、流式事件属于数据面。
3. **租户主键贯穿全局**：所有资源（thread / run / agent / skill / connector / audit）携带 `org_id`（及可选 `workspace_id`）；现有 `user_id` 隔离升级为 `org_id + user_id`。
4. **语义不混用**：
   - `ask_clarification` ≠ 企业审批
   - Memory（用户偏好事实）≠ 企业知识库
   - IM `workspace_id`（外部平台字段）≠ 平台 Workspace
5. **先契约后市场**：租户、RBAC、审计、不可变制品到位后，再建设计费与公共市场。

## 产品愿景（简版）

```text
DeerNexus
├── 工作台 /workspace          ← 终端用户（复用并演进 DeerFlow UI）
├── 企业控制台 /admin          ← 组织、IAM、观测、用量、审计、平台设置
├── Agent Studio /studio       ← 制品、版本、评测、发布通道
├── 连接器中心 /connectors     ← 租户 MCP / 凭证 / 知识库
├── 审批中心 /approvals        ← 高风险工具、发布、策略升级
├── 注册表 /registry           ← Skill / Agent / MCP 包（后期）
└── 开发者 /developers         ← API Key、Webhook、SDK / CLI（后期）
```

命名拆分（子产品）：

- **DeerNexus Runtime** — Agent 运行内核
- **DeerNexus Control** — 企业控制面
- **DeerNexus Studio** — Agent 开发与发布
- **DeerNexus Registry** — 制品市场（后期）

## 首个 90 天目标

不以「建成完整 OS」为验收标准，而以「建立不会反复推倒的平台基座」为标准：

1. **0–30 天**：生产基线（Postgres + Redis + 生产沙箱）与 Tenant ADR/上下文骨架
2. **31–60 天**：Organization / Workspace / Membership、真实 RBAC、统一审计事件
3. **61–90 天**：企业 Console UI、不可变 Agent 版本 MVP

详见 [90 天 MVP 路线图](docs/roadmap/90-day-mvp.md)。

## 文档导航

| 文档 | 说明 |
| --- | --- |
| [文档索引](docs/README.md) | 文档目录、贡献约定、后续文档计划 |
| [目标架构](docs/architecture/target-architecture.md) | 分层、复用边界、数据流、部署拓扑 |
| [运行时稳定契约](docs/architecture/runtime-contracts.md) | TenantContext、Policy、Release、Audit、Usage 协议 |
| [MVP 数据模型](docs/architecture/data-model.md) | Org、RBAC、制品、审计及资源归属 |
| [API 边界](docs/architecture/api-boundaries.md) | Runtime、Admin、Studio 与 Internal API |
| [生产安全基线](docs/security/baseline.md) | 身份、隔离、Secret、Sandbox、SSRF 与供应链底线 |
| [威胁模型](docs/security/threat-model.md) | STRIDE、Abuse Case、控制追踪与残余风险 |
| [数据治理](docs/compliance/data-governance.md) | 分类、用途、保留、删除、导出与合规证据 |
| [生产 Runbook](docs/ops/production-runbook.md) | 部署、迁移、备份恢复、双副本和故障处理 |
| [可观测性与 SLO](docs/ops/observability-and-slo.md) | 日志、指标、Trace、告警与错误预算 |
| [容量与灾备](docs/ops/capacity-and-dr.md) | 容量测量、扩容水位、RPO/RTO 与恢复演练 |
| [测试策略](docs/engineering/testing-strategy.md) | 隔离、契约、迁移、发布、安全和恢复门禁 |
| [CI/CD](docs/engineering/ci-cd.md) | 流水线、制品、环境晋升和迁移门禁 |
| [上游同步](docs/engineering/upstream-sync.md) | DeerFlow 基线、同步节奏与分叉度量 |
| [PR 拆分指南](docs/engineering/pr-split-guide.md) | 大改造的依赖顺序、PR 粒度和回滚要求 |
| [90 天 MVP](docs/roadmap/90-day-mvp.md) | 分期交付、依赖、验收、非目标 |
| [ADR-0001 Fork 策略](docs/adr/0001-fork-evolution-strategy.md) | 改造路径、约束与后续 ADR 入口 |
| [ADR-0002 租户主键](docs/adr/0002-tenant-workspace-keys.md) | Organization、Workspace、外部标识与迁移规则 |
| [ADR-0003 RBAC](docs/adr/0003-rbac-and-service-accounts.md) | 角色、服务账号、API Key 与撤权 |
| [ADR-0004 Agent 发布](docs/adr/0004-agent-artifacts-and-release.md) | 不可变制品、通道、晋升与回滚 |
| [ADR-0005 AuditEvent](docs/adr/0005-audit-event.md) | 审计事件、可靠写入、防改写与保留 |
| [ADR-0006 Worker 拆分](docs/adr/0006-gateway-worker-split.md) | Gateway / Worker 的证据触发拆分条件 |

后续重点转为 Fork 初始化后的真实实现映射、容量实测，以及知识库、审批、计费和 Registry 启动前的专项 ADR。

## 仓库状态

当前仓库已从文档先行阶段进入 Fork 实施阶段。初始代码基线固定为 DeerFlow `v2.0.0`（commit `7e7f0410797693cf882594555ba414e0361d4c6f`），精确来源见根目录 [`UPSTREAM_BASE`](UPSTREAM_BASE)；后续企业改造按本仓库架构、路线与 PR 拆分指南渐进实施。

## 术语速查

| 术语 | 含义 |
| --- | --- |
| Org / Tenant | 计费与隔离边界的组织；平台最高隔离单元 |
| Workspace | Org 内项目或命名空间；配额与资源分组（可选） |
| Catalog | Agent / Skill / MCP / Tool 注册与发布态目录 |
| Policy Engine | 工具、模型、网络、数据访问的统一策略决策 |
| Approval | 多级审批工作流；与 Human Input 协议可衔接但独立建模 |
| Release Channel | `dev` / `staging` / `prod` 等环境绑定与晋升通道 |
| Run | 一次 Agent 执行生命周期（pending → running → terminal） |

## License / 归属

DeerNexus 演进自 DeerFlow（MIT）。上游致谢与许可证继承在代码仓初始化时一并落档。
