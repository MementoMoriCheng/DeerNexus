# ADR-0001：Fork 渐进演进策略

- **状态**：Accepted  
- **日期**：2026-07-15  
- **决策者**：DeerNexus 项目组  
- **关联**：[README](../../README.md) · [目标架构](../architecture/target-architecture.md) · [90 天 MVP](../roadmap/90-day-mvp.md) · [ADR-0002](0002-tenant-workspace-keys.md)

---

## 1. 背景

DeerFlow 2.x 已具备可生产的超级 Agent 运行栈：LangGraph 编排、长 Middleware 链、RunManager / StreamBridge、沙箱、Skills / MCP、OIDC 骨架、用户级隔离、Console 用量 API、定时任务与 IM 通道。

企业级 Agent OS 还缺：多租户组织模型、真实 RBAC、制品版本与发布通道、组织级审计、可运营控制台、（后续）审批、知识连接器、配额计费与市场。

可选改造路径有三条：

1. **Fork 渐进演进**（本 ADR）  
2. 保持上游不改，独立建设企业控制面仓库并远程调用 DeerFlow  
3. 复制后完全独立 Monorepo，大幅分叉甚至重写内核  

已确认选型：**Fork 渐进演进**。

---

## 2. 决策

> **DeerNexus 以 DeerFlow 为 fork 基线，保留 `packages/harness/deerflow` 作为 Agent 运行内核；企业控制面与管理体验在 `app/` 与 `frontend` 增长；通过 ContextVar、策略钩子与稳定契约将租户、权限、发布与审计注入运行时，而不是重写图编排或平行执行栈。**

配套约定：

1. 持续跟踪上游修复与能力；企业私有改动优先落在 `app/control_plane`、前端管理面与契约层。  
2. 禁止 harness 依赖控制面私有实现（维持现有 harness → app 防火墙测试思路）。  
3. 首个 90 天只验收基座（生产基线、租户/RBAC、审计、Console UI、Agent 版本），不做市场与完整计费。  
4. IM 频道的 `workspace_id` 不得复用为平台 Workspace 主键。  
5. 语义隔离：`ask_clarification` ≠ 审批；Memory ≠ 企业 KB。

---

## 3. 备选方案与取舍

### 方案 A：Fork 渐进演进（采纳）

**优点**

- 最大化复用已验证的中间件、Run 生命周期、沙箱与扩展生态  
- 控制面与运行时可同仓演进，契约落地反馈快  
- 与 90 天 MVP「先主键与权限」路径一致  

**代价**

- 需管理上游合并与企业补丁冲突  
- 早期 Gateway 仍可能「厚」，Worker 拆分要分阶段  

### 方案 B：上游只读 + 独立控制面

**优点**：边界清晰，上游合并压力小。  
**缺点**：双仓版本与发布契约极易漂移；租户上下文、审计归因、本地制品与运行时强耦合，初期集成成本反而更高。适合后期「控制面 SaaS 化」再评估。

### 方案 C：完全独立 Monorepo / 重写内核

**优点**：命名与目录完全自由。  
**缺点**：重复建设运行核；回归周期长；违背「强复用」原则，不符合当前人力与窗口。

---

## 4. 架构后果

### 必须做

- 引入 `TenantContext` 与资源 `org_id`  
- 替换 `authz` flat permissions 为组织角色权限  
- 扩展 Console 到组织级，并为 Admin UI 提供 API  
- 引入不可变 Agent 制品与 Release Channel  
- 统一 `AuditEvent` 而不是仅依赖可变 RunEvent 查询  

### 允许延后

- 物理拆分 Gateway / Worker  
- 独立 Model Gateway  
- 审批中心产品化、KB、Registry 市场、账单系统  

### 禁止

- 在 harness 内硬编码 org 业务规则与审批 UI 状态机  
- 生产默认开启 host bash 作为「隔离」  
- 为赶进度跳过跨 org 隔离回归  

---

## 5. 与仓库布局的映射

| 区域 | Fork 策略 |
| --- | --- |
| `backend/packages/harness/deerflow/` | 上游对齐区 + 最小企业钩子（契约读取） |
| `backend/app/gateway/` | 认证授权、控制面路由挂载、tenant 注入 |
| `backend/app/control_plane/` | 新建：tenant / iam / catalog / policy / approval / quota / audit / release |
| `backend/app/worker/` | 可选：多副本后拆出 |
| `frontend/src/app/workspace/` | 保留并演进终端体验 |
| `frontend/src/app/admin/` | 新建企业控制台 |
| `docs/`（本仓） | 文档先行；代码 fork 初始化后以本文档为验收基准 |

详细分层见 [目标架构](../architecture/target-architecture.md)。

---

## 6. 迁移与上游策略（原则）

1. **默认单组织 bootstrap**：存量 `user_id` 数据迁入 `org_id=default`（或部署时指定），再开启多组织。  
2. **特性开关**：企业控制面能力用 config / features API 逐步打开，避免一次性破坏开源单体路径。  
3. **上游合并**：安全补丁与运行时缺陷优先合入；与企业模型冲突的界面改动走适配层，不在 harness 打长期分叉补丁。  
4. **度量分叉**：每季度盘点「仅企业存在」的 harness 补丁行数；过高则启动方案 B 评估。

---

## 7. 验收（对本 ADR）

- [x] README 明示 Fork 渐进演进  
- [x] 目标架构与 90 天路线方向一致；已知实施歧义由 ADR-0002 与实施规格显式收敛  
- [ ] 代码仓初始化时建立 harness 边界测试（或继承上游 `test_harness_boundary`）  
- [ ] 首个 org/RBAC PR 引用本 ADR 编号  

---

## 8. 后续 ADR 候选

| 编号 | 状态 | 主题 |
| --- | --- | --- |
| [ADR-0002](0002-tenant-workspace-keys.md) | Accepted | Tenant / Workspace 主键与资源归属规则 |
| [ADR-0003](0003-rbac-and-service-accounts.md) | Accepted | RBAC 权限模型与服务账号 |
| [ADR-0004](0004-agent-artifacts-and-release.md) | Accepted | Agent 制品不可变发布与通道 |
| [ADR-0005](0005-audit-event.md) | Accepted | AuditEvent Schema、可靠写入与保留策略 |
| [ADR-0006](0006-gateway-worker-split.md) | Accepted | Gateway / Worker 拆分时机与前置条件 |

---

## 9. 参考

- DeerFlow 后端分层与运行时说明（上游 `backend/AGENTS.md`）  
- DeerFlow Console API：`backend/app/gateway/routers/console.py`  
- DeerFlow 认证设计与 `authz.py` 现状（flat permissions）  
- DeerNexus 内部分析结论：控制面新建 + 运行核复用最短路径
