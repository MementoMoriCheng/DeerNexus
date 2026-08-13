# DeerNexus 架构设计总览

> **视角**：高级架构师 / 技术负责人
> **目的**：为工程团队和利益相关者提供一份从顶层设计哲学到关键实现细节的全景文档。
> **覆盖范围**：设计思路、架构亮点、核心子系统实现、工程纪律。
> **权威来源**：ADR-0001~0006、`runtime-contracts.md`（v1alpha1）、`progress.md`（单一权威进度账本）。

---

## 目录

1. [项目定位与设计哲学](#1-项目定位与设计哲学)
2. [整体架构分层](#2-整体架构分层)
3. [核心设计亮点](#3-核心设计亮点)
4. [关键子系统实现](#4-关键子系统实现)
5. [工程纪律与质量保障](#5-工程纪律与质量保障)
6. [技术选型与取舍](#6-技术选型与取舍)
7. [演进路线与未来展望](#7-演进路线与未来展望)

---

## 1. 项目定位与设计哲学

### 1.1 项目定位

**DeerNexus = 可治理的企业级 Agent 执行操作系统（Enterprise Agent OS）。**

它由字节跳动开源的 **DeerFlow**（LangGraph + FastAPI 超级 Agent harness）通过 **Fork 渐进演进** 而来。命名取意：Deer（DeerFlow 血缘）+ Nexus（连接中心）。

它不是单一 Deep Research 应用，而是一套完整的 OS：

| 层次 | 角色 | 类比 |
|---|---|---|
| **运行内核** | 调度 Run、装载 Agent 制品、沙箱、工具/技能/MCP、流式输出 | 操作系统内核 |
| **企业控制面** | 租户/权限/目录/策略/审批/配额/审计/发布 | 管理面 + 安全子系统 |
| **体验层** | Workspace（终端工作台）+ Admin Console + Agent Studio | 用户空间应用 |

### 1.2 五条核心原则

这五条原则贯穿全部设计决策，是理解整个项目的前提：

```
┌─────────────────────────────────────────────────────────────────────┐
│  ① 运行内核不重写 — 强复用 DeerFlow 的 LangGraph 图编排与并行执行   │
│  ② 控制面/数据面分离 — 契约隔离，企业能力可独立演进                 │
│  ③ 租户主键贯穿全局 — org_id 是硬隔离边界，零泄露、零错误预算      │
│  ④ 默认拒绝（fail-closed）— 缺失即失败，永不静默回退              │
│  ⑤ 先契约后市场 — 稳定契约先行，市场功能（Registry）后置          │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 核心设计决策：Fork 渐进演进（ADR-0001）

在三选一中，项目选择了 **渐进 Fork**（方案 A），而非双仓契约漂移（方案 B）或完全重写（方案 C）：

```
          ┌── 方案 A（采纳）────────── Fork 渐进演进
          │   ✓ 复用已验证的 Agent 编排栈
          │   ✓ 单仓契约零漂移
选择 ─────┤   ✓ 企业能力在 app/ 层叠加
          │
          ├── 方案 B（否决）────────── 双仓独立
          │   ✗ 双仓契约维护成本高，漂移不可避免
          │
          └── 方案 C（否决）────────── 完全重写
              ✗ 违背强复用，重新验证并行执行栈的风险过高
```

**仓库映射**：

```
backend/
├── packages/harness/deerflow/    ← Agent 运行内核（上游对齐 + 最小企业钩子）
│   ├── contracts/                   稳定契约（DTO/Protocol/ContextVar）
│   ├── runtime/                     Run 生命周期 / StreamBridge / checkpointer
│   ├── agents/                      Lead Agent + middleware 链
│   ├── tenancy/                     ← DeerNexus 新增：租户上下文
│   └── ...
│
├── app/                          ← 应用编排层（DeerNexus 企业控制面）
│   ├── gateway/                     FastAPI Gateway（认证/授权/路由/租户注入）
│   ├── channels/                    IM 渠道适配（飞书/Slack/Telegram/...）
│   └── doctor/                      生产健康探测
│
└── tests/                        ← 300+ 测试文件（含跨 Org 隔离回归）
```

**依赖方向（硬约束）**：

```
deerflow runtime  ──→  deerflow.contracts  ←──  app control-plane adapters
                        (稳定边界)
```

harness **禁止** import `app.control_plane.*`；运行时只消费稳定契约（DTO/Protocol/ContextVar），不读取控制面私有表（Organization/RoleBinding/AgentPackage 等）。

---

## 2. 整体架构分层

### 2.1 四层架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        体验层 (Experience)                        │
│  Workspace UI    Admin Console    Agent Studio    [未来: Registry]│
│  (DeerFlow 复用)  (DeerNexus 新增) (DeerNexus 新增)                │
├──────────────────────────────────────────────────────────────────┤
│                     企业控制面 (Control Plane)                    │
│  Tenant_IAM │ Catalog │ Policy │ Approval │ Quota │ Audit │ Release│
├──────────────────────────────────────────────────────────────────┤
│                   Agent 运行内核 (Runtime Kernel)                 │
│  Gateway_API │ RunManager_StreamBridge │ LeadAgent_Middleware    │
│  Subagent │ Skills_MCP_Tools │ Guardrails                         │
│  【强复用 DeerFlow harness，不改图编排与并行执行栈】               │
├──────────────────────────────────────────────────────────────────┤
│                   执行与数据平面 (Execution & Data)               │
│  Worker(可选) │ Sandbox_Pool │ PostgreSQL │ Redis │ ObjectStore   │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 运行时拓扑

```
                    ┌──────────────┐
                    │   Nginx      │ :2026（统一入口）
                    │  (反代)       │
                    └──┬───────┬───┘
               /api/*  │       │  非API（页面/静态资源）
                        ▼       ▼
              ┌─────────────┐  ┌──────────────┐
              │ FastAPI     │  │  Next.js 16  │ :3000
              │ Gateway     │  │  Frontend    │
              │ :8001       │  └──────────────┘
              │ + 内嵌      │
              │   Agent     │
              │   Runtime   │
              └──┬───┬──┬───┘
         ┌───────┘   │  └────────────┐
         ▼           ▼               ▼
   ┌──────────┐ ┌────────┐   ┌──────────────┐
   │PostgreSQL│ │ Redis  │   │ Sandbox Pool │
   │ (权威)   │ │(协调)  │   │ (AIO/K8s)    │
   └──────────┘ └────────┘   └──────────────┘
```

**数据权威性分层（关键设计）**：

| 存储 | 角色 | 权威性 |
|---|---|---|
| **PostgreSQL** | Run/Thread/ReleaseRef/terminal 权威 | **唯一权威**，Redis 丢失可据此 reconcile |
| **Redis** | Stream/lease/heartbeat/SSE/cancel 通知 | **非权威**，纯协调加速，丢失不影响终态 |
| **ObjectStore** | Agent 制品/审计归档 | 私有 Bucket + 静态加密 + 签名 URL |

> **设计不变量（TM-029）**：Redis 非权威。任何终态以 PostgreSQL 为准；Redis flush 后 reconciler 根据 PG 恢复，不复活终态 Run。

### 2.3 部署 Profile（ADR-0006）

项目定义了三个递进的生产部署 Profile，采用 **证据触发**（非日期触发）的升级策略：

| Profile | 含义 | 准入条件 |
|---|---|---|
| **Profile S** | 单副本，逻辑 Worker 内嵌 | 默认，不宣称 HA |
| **Profile H** | 多副本，逻辑 Worker 可内嵌 | lease/cancel/reconcile/rolling 验收 + **24h Soak** + fault-injection 通过 |
| **Profile W** | 物理拆分 Gateway/Worker | 须声明 Gateway 自身 S 或 H + 全部前置条件 |

---

## 3. 核心设计亮点

### 亮点 1：稳定契约层（contracts）— 架构的"根"

整个项目最精巧的设计是 `deerflow/contracts/` 包——它是运行内核与企业控制面之间的**唯一桥梁**。

```
deerflow/contracts/
├── context.py        TenantContext + ContextVar 生命周期
├── identity.py       PrincipalRef（审计主体）
├── runs.py           RunEnvelope（可信任务信封）
├── release.py        ReleaseRef（不可变执行引用）
├── policy.py         PolicyRequest → PolicyDecision
├── events.py         AuditEvent + UsageRecord
├── rbac.py           Permission 注册表 + 内置角色
├── errors.py         22 个错误码 + HTTP 映射
└── versioning.py     契约版本管理
```

**设计约束**：

- 契约层 **只依赖** `pydantic`、`contextvars`、`datetime`、`enum`、`typing`（白名单）。
- **禁止**导入 ORM/SQLAlchemy/FastAPI/LangGraph/Alembic。
- **CI 强制执行**：`test_harness_boundary.py` 用 AST 扫描（含 `importlib` 动态导入检测）确保约束不被违反。白名单外的任何新依赖都使 CI 变红——**fail-closed**。

**契约可演进性**：

| 兼容 | 破坏性 |
|---|---|
| 新增可选字段 | 删除/改字段语义 |
| 新增可忽略 metadata | 可选改必填 |
| 新增错误码（旧消费者须能安全拒绝） | 改默认授权或 fail 行为 |
| 新增枚举值 | 改 ID/时间/digest 格式 |

破坏性变更需升主版本（如 `v1alpha1` → `v2alpha1`），旧版保留一个发布周期。

### 亮点 2：TenantContext + AUTO_ORG 三态 Sentinel

多租户隔离的核心机制。`TenantContext` 是一个不可变 DTO，通过 `ContextVar` 在请求生命周期内传播：

```python
# contracts/context.py — 核心设计
class TenantContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")  # ① 不可变 + 拒绝未知字段
    org_id: str           # 硬隔离边界
    workspace_id: str | None = None
    principal: PrincipalRef
    auth_method: Literal["oidc", "session", "api_key", "internal"]
    request_id: str
    trace_id: str | None = None
    issued_at: datetime
```

**关键设计决策**：

1. **`extra="ignore"`**：上下文不携带任何 secret/token/cookie——泄露的凭据无法搭便车走私。
2. **`frozen=True`**：下游不可篡改已绑定的租户上下文。
3. **fail-closed**：`require_tenant_context()` 在未绑定时抛错，**永不退回默认 Org**。
4. **AUTO_ORG 三态 sentinel**——最精巧的设计：

```python
AUTO_ORG   → 从 ContextVar 读 org_id（默认，生产路径）
"explicit" → 覆盖绑定（管理员/测试路径）
None       → 跳过 org WHERE（迁移脚本/系统扫描，仅限 system-admin）
```

这使得 repository 方法可以在**保持 fail-closed 默认行为**的同时，为迁移脚本和 system-admin 提供 escape hatch。

### 亮点 3：审计 Append-Only 三层防御

审计系统的防篡改设计是 defense-in-depth 哲学的教科书实现：

```
┌─────────────────────────────────────────────────────────┐
│ 第一层：应用层 API 表面                                 │
│   AuditRepository 只暴露 insert/get/count/list          │
│   根本没有 update/delete 函数                            │
│   修正 = 追加 audit.event.corrected（引用 original_id） │
├─────────────────────────────────────────────────────────┤
│ 第二层：DB 触发器（防绕过）                              │
│   BEFORE UPDATE OR DELETE → RAISE EXCEPTION             │
│   方言感知：SQLite(RAISE ABORT) / PG(RAISE EXCEPTION)   │
│   即使裸 SQL 也不能修改/删除                             │
├─────────────────────────────────────────────────────────┤
│ 第三层：payload 二次 scrub                              │
│   持久化时再次调用 _scrub_payload()                      │
│   即使 model_construct 跳过 DTO 验证，秘密也不入库       │
└─────────────────────────────────────────────────────────┘
```

**写入可靠性等级**：

| 等级 | 场景 | 策略 |
|---|---|---|
| **Class A** | 高风险控制面写（Role/SA/API Key/Agent 发布） | 业务变更与 outbox **同事务**，outbox 失败则业务回滚 |
| **Class B** | 运行时安全事件（login/Policy deny/sandbox 违规） | 先入可靠 outbox，全路径不可写则 fail-closed |
| **Class C** | 可选访问审计 | 尽力写入 |

### 亮点 4：Run 并发控制 — CAS / Lease / Reconciler 四层正交

这是项目工程深度最高的子系统（Track G，PR-070~077），解决分布式 Agent Run 的并发安全：

```
┌──────────────────────────────────────────────────────────────┐
│ PR-070  CAS (row_version)          "单一终端赢家"            │
│         UPDATE ... AND row_version = :expected                │
│         cancel 与 completion 竞争由 CAS 仲裁（TM-027）       │
├──────────────────────────────────────────────────────────────┤
│ PR-071  Lease (Redis SET NX)       "谁可以驱动此 Run"        │
│         SET key NX EX 30s → 原子性，只有一个 claim 获胜      │
│         Lua CAS 续租（防过期持有者错误）                     │
│         Lua CAD 释放（只有 owner 能删除）                    │
├──────────────────────────────────────────────────────────────┤
│ PR-072  Reconciler                 "孤儿 Run → 安全终态"     │
│         PG 终态优先（不复活）                                 │
│         lease 感知（跳过活跃 Run）                            │
│         CAS 驱至 error（从不重放副作用）                     │
├──────────────────────────────────────────────────────────────┤
│ PR-073  StreamBridge (Redis Stream) "跨副本 SSE 恢复"        │
│         Last-Event-ID 严格恢复（XREAD ID 排他）             │
│         ended 侧 key + TTL 防尾标记被 MAXLEN 驱逐           │
│         非权威（从不触及 Run/终态）                          │
└──────────────────────────────────────────────────────────────┘
```

**Cancel 的双路径设计（PR-077）**：

```
RunManager.cancel(run_id)
├── 本地快速路径（Run 在本进程）
│   ├── 设置 abort_event → 取消 asyncio.Task
│   └── CAS → interrupted（TM-027 单一赢家）
│
└── 跨副本路径（Run 在其他副本）
    ├── PG 持久化 cancel_requested=true（权威兜底）
    ├── Redis 通知 deerflow:run:cancel:{run_id}（加速）
    └── 持有者心跳轮询 PG（≤10s 感知）→ 设置 abort_event
```

### 亮点 5：Agent 不可变制品 + 发布通道（ADR-0004）

Agent 作为**不可变制品**管理，借鉴了容器镜像的成熟模式：

```
AgentPackage（逻辑身份）
    └── AgentVersion（不可变内容版本）
            digest = sha256:<hex>    ← digest 是执行身份，version 仅展示
            state: draft → reviewed → published → revoked
                                          │
                                          ▼
            ReleaseChannel（可变指针）     dev / staging / prod
                    │                     prod 只允许 published
                    │                     晋升/回滚用 CAS（row_version）
                    ▼
            ReleaseRef（Run 创建时持久化）
                    创建后不可变，执行阶段不重读 channel
                    → 在途 Run 不因发布变更而漂移
```

**安全门禁矩阵**：

| 通道 | 允许的版本状态 | 额外门禁 |
|---|---|---|
| **dev** | draft/reviewed/published | — |
| **staging** | reviewed/published | Reviewed 门禁通过 |
| **prod** | **仅 published** | 静态安全 + digest 匹配 + E2E 回滚通过 + 无阻断撤销 + Policy allow |

**Legacy Run 保护**：`legacy_unpinned=true` 的 Run（在 ReleaseRef 强制之前的旧 Run）在 prod 下只允许读取/取消/归档，**不可 resume/stream/继续**。

### 亮点 6：撤权 SLO P99 ≤60s（ADR-0003）

权限撤销的实时性是企业级的核心要求：

```
                    缓存层（TTL ≤60s）
            ┌──────────────────────────┐
            │ InMemoryPermissionCache  │
            │ key: org_id + principal  │
            │ TTL: ≤60s                │
            └──────────┬───────────────┘
                       │ 变更时主动失效
                       ▼
            ┌──────────────────────────┐
            │ invalidate_principal()    │ ← 撤权触发
            │ force_refresh()           │
            └──────────┬───────────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
     ┌─────────────┐   ┌──────────────────┐
     │ HTTP 请求    │   │ SSE 流           │
     │ (下个请求)   │   │ (60s 或下条事件  │
     │             │   │  前重确认 →      │
     │             │   │  撤权后关流)     │
     └─────────────┘   └──────────────────┘
```

**保证**：即使主动失效失败，TTL 也会在 60s 内过期。SSE 长连接每 60s 或下条业务事件前重确认 Principal/权限，撤权后立即关闭流。

### 亮点 7：Profile H 故障注入测试套件

ADR-0006 要求"至少一次生产等价故障注入"，项目把它变成了**可回归的 hermetic 测试**：

```python
# tests/test_profile_h_fault_injection.py — 5 个 ADR 场景
TM-026  单 owner 竞争     → 3 个并发 claim，恰好 1 个 winner
租约过期 + 回收           → expired lease 可被新 worker reclaim
TM-029  Redis 丢失        → PG 终态不被复活（Redis 非权威）
跨副本 SSE 恢复           → Last-Event-ID 严格恢复 m2,m3,m4
TM-028  Reconciler 收敛   → 孤儿 Run → error，无 replay
```

全部使用 `fakeredis`（无外部依赖），另有 `@pytest.mark.real_redis` 类对真实 Redis 重跑以捕获行为差异。

---

## 4. 关键子系统实现

### 4.1 Run 生命周期子系统

**核心文件**：`backend/packages/harness/deerflow/runtime/runs/`

```
runs/
├── manager.py        RunManager — 内存注册表 + 持久化编排
├── worker.py         run_agent() — 实际的 LangGraph 图执行
├── ownership.py      lease claim/renew/release（Redis 协调）
├── transitions.py    状态机 + 终端不变性
├── dispatch.py       Dispatcher/Executor Protocol（PR-075 接缝）
├── schemas.py        RunStatus 枚举（6 状态）
└── naming.py         辅助函数
```

**状态机（6 状态，终端不可逆）**：

```
pending ──→ running ──→ success      (正常完成)
              │         error         (执行失败)
              │         timeout       (超时)
              │         interrupted   (用户取消 / rollback)
              │
              └── [终端状态] ── 无出边，CAS 保证单一赢家
```

**RunManager 的核心方法**：

| 方法 | 职责 | 关键约束 |
|---|---|---|
| `create_or_reject()` | 原子创建 Run | TOCTOU 安全；多任务策略 reject/interrupt/rollback；幂等 |
| `set_status()` | 状态转换 | 强制状态机；CAS 持久化（row_version） |
| `cancel()` | 取消 Run | 双路径（本地 / 跨副本）；CAS 仲裁 |
| `reconcile_orphaned_inflight_runs()` | 启动 + 周期性回收 | PG 终态优先；lease 感知；无 replay |
| `shutdown()` | 优雅停机 | drain 进行中 Run；在 checkpointer 销毁前 |

### 4.2 StreamBridge 子系统

**核心文件**：`backend/packages/harness/deerflow/runtime/stream_bridge/`

解耦生产者（`run_agent` worker）和消费者（SSE HTTP 端点）：

```
                生产者                          消费者
        run_agent worker               SSE HTTP endpoint
                │                             │
                ▼                             ▼
        ┌──────────────┐             ┌──────────────┐
        │ bridge.      │             │ bridge.      │
        │ publish()    │             │ subscribe()  │
        └──────┬───────┘             └──────┬───────┘
               │                            │
        ═══════╪════════════════════════════╪═══
        Memory │ (单进程: asyncio.Queue)    │
        Redis  │ (跨副本: Redis Stream)     │
        ═══════╪════════════════════════════╪═══
               │                            │
               ▼                            ▼
        StreamEvent(id, data)    产生 StreamEvent 流
        Redis entry ID ──────────→ SSE id: 字段
                                   支持 Last-Event-ID 重连
```

**RedisStreamBridge 的关键设计**：

- 每个 Run 一个 Redis Stream：`deerflow:run:stream:{run_id}`
- `StreamEvent.id == Redis entry ID`——`XREAD` ID 是排他的，`subscribe(last_event_id=X)` 在 X 之后严格恢复
- **结束信号用侧 key**（`deerflow:run:stream:{run_id}:ended`），不用流条目——因为 `MAXLEN` 修剪可能驱逐尾标记
- Stream + 侧 key 均有 TTL（3600s），即使清理遗漏也自动过期
- **非权威**：从不触及 Run/终态

### 4.3 RBAC 子系统

**核心**：`app/gateway/authorize.py` + `deerflow/contracts/rbac.py`

**权限模型**：`<domain>:<resource>:<action>`（22 权限，5 domain）

**授权计算公式**：

```
effective = active_membership
          ∩ active_principal
          ∩ non_expired_role_bindings
          ∪ union(role.permissions)
          ∩ api_key.scopes_if_present
          ∩ organization_state
          ∩ policy_decision
```

**三个内置角色**：

| 角色 | 权限数 | 定位 |
|---|---|---|
| `org:admin` | 19 | 组织管理员 |
| `org:developer` | 10 | 开发者（Studio 读写） |
| `org:viewer` | 4 | 只读 |

`system:admin` 是平台级独立身份，独立于 RoleBinding，须 MFA/break-glass。

### 4.4 Doctor 探测系统

**核心文件**：`backend/app/doctor/`

生产准入信号——**doctor 命令的退出码即部署门禁**（0=PASS, 1=FAIL）：

```
三类检查：
├── STATIC_CHECKS（11 个，纯配置断言）
│   ├── 认证旁路在生产必须关闭
│   ├── Redis URL 必须 rediss://（强制 TLS）
│   ├── OIDC issuer 必须 https:// + audience 非空
│   ├── Profile H 准入要三件套（HA evidence + Soak + fault-injection）
│   ├── Feature Flag 过期检查（过期 FAIL，30 天内 WARN）
│   └── Secret 引用检查（禁止字面量凭据）
│
├── LIVE_PROBE_REGISTRY（10 个，异步真实探测）
│   ├── postgres.connectivity（SELECT 1 + version ≥15）
│   ├── redis.connectivity
│   ├── audit.outbox（backlog 检查）
│   ├── backup.freshness（RPO 验证 + manifest 防篡改）
│   └── ...
│
└── DEFERRED_LIVE_CHECKS（占位，按 Track 标注 remediation）
    "Blocked on Track C (PR-036 OIDC)" — 知道该等什么
```

**诚实契约设计**：backup probe 的 PASS 消息强制附加"不替代 DB 平台备份（pg_dump/WAL/PITR）"——明确这是应用层证据，不是 DB 级备份替代。

### 4.5 前端架构

**技术栈**：Next.js 16 + React 19 + TypeScript 5.8 + TailwindCSS 4 + shadcn/ui + pnpm 10

**领域驱动组织**（`frontend/src/core/`）：

每个领域遵循一致的三件套约定：
```
core/<domain>/
├── api.ts     REST 客户端（纯函数，无副作用）
├── hooks.ts   TanStack Query 封装（缓存键/invalidation/optimistic）
├── types.ts   契约类型（对应后端 Pydantic 模型 1:1）
└── index.ts   桶文件
```

**分层信任策略**（契约映射的精妙之处）：

| 领域 | 运行时校验 | 理由 |
|---|---|---|
| `core/auth/` | **zod 运行时校验** | 用户输入/SSR 边界不可信 |
| `core/admin/` | **无 zod，plain interface** | Gateway 是 trusted source |
| `core/studio/` | **无 zod，plain interface** | 字段名与 Pydantic 1:1，重命名暴露为 TS 编译错误 |

**鉴权流程**：所有受保护 layout 统一调用 `getServerSideUser()` → 穷举 switch（`assertNever` 保证完整性）→ SSR `redirect()`，无客户端闪烁。

**SSE 流式**：通过 `@langchain/langgraph-sdk` 的 `useStream` hook（非手写 EventSource），处理 `onCreated/onUpdateEvent/onCustomEvent/onError/onFinish` 全生命周期。

**DeerNexus 前端增量**（相对 DeerFlow）：

| 增量 | 对应 ADR/PR |
|---|---|
| 多租户认证 + RBAC | ADR-0003，RFC-001 |
| Admin 控制台 | ADR-0005，PR-060 |
| Agent Studio（制品+发布） | ADR-0004，PR-050~057 |
| per-user model-providers CRUD | 新增 |
| CAS + 幂等契约（`If-Match`/`Idempotency-Key`） | PR-053/055 |
| 契约防漂移测试（`e2e-real-backend`） | — |

---

## 5. 工程纪律与质量保障

### 5.1 单一职责 PR（`pr-split-guide.md`）

项目把 90 天大改造拆成可审查/可测试/可回滚的 PR，每个 PR 只有一个清晰架构意图：

**10 条原则**：
1. 契约先于实现
2. Expand 先于 Enforce
3. 安全门禁随第一条路径进入
4. 一 PR 一主要风险
5. 可独立回滚
6. 行为可观察
7. 不隐藏依赖
8. 上游区最小改动
9. 不做顺手重构
10. 文档与代码同 PR

**7 Track 交付序列**：

```
Track 0  Fork 基线（CI + boundary + doctor 骨架）
Track A  Contracts + TenantContext
Track B  Schema 迁移（expand → backfill → enforce → contract）
Track C  RBAC + 机器身份（ServiceAccount + API Key + OIDC Group）
Track D  审计（Schema + Outbox + Class A 原子性 + 查询）
Track E  Agent 制品 + Release（不可变版本 + CAS 通道 + Legacy 门禁）
Track F  Console + 观测 + 生产（Admin UI + Metrics + CI/CD Gate）
Track G  Run 控制 + Worker（CAS + Lease + Reconciler + StreamBridge + Dispatch Protocol）
```

### 5.2 Schema 迁移纪律

采用 **Expand/Contract** 模式，禁止合并不可回滚的发布：

```
Phase 1: Expand   — 新增可空列（向后兼容，旧应用可运行）
Phase 2: Backfill — 填充已有数据
Phase 3: Enforce  — 加 NOT NULL + 复合唯一约束
Phase 4: Contract — 删除旧列/旧路径
```

每个 Phase 是独立 PR，可独立回滚。`progress.md` 记录每个 PR 的"严格不越界范围"和"回滚边界"。

### 5.3 测试金字塔

```
                    ┌─────────────┐
                    │   E2E       │  Playwright（前端 + real-backend 契约）
                    │  (真实后端)  │  auth-disabled-contract.spec.ts（防前后端漂移）
                    ├─────────────┤
                    │  Integration │  Profile H fault-injection（fakeredis）
                    │  (跨子系统)  │  org-isolation / owner-isolation（存储层直击）
                    ├─────────────┤
                    │    Unit     │  300+ 测试文件
                    │  (单模块)   │  contracts boundary（AST 扫描）
                    └─────────────┘
```

**门禁测试**：

| 测试 | 门禁内容 |
|---|---|
| `test_harness_boundary.py` | AST 扫描，harness 不得 import app；contracts 只能用白名单依赖 |
| `test_org_isolation.py` | 跨 Org 零泄露（同一 user 在两个 Org，排除 user filter 混淆） |
| `test_owner_isolation.py` | 同 Org 内 user 边界 |
| `test_profile_h_fault_injection.py` | 5 个 ADR 场景（fakeredis + real_redis 双跑） |
| `blocking_io/` | Blockbuster 运行时门禁 + AST 静态扫描 + git diff 交集 |

### 5.4 阻塞 IO 检测（三层互补）

项目对 asyncio 事件循环阻塞零容忍，构建了三层互补的检测体系：

```
第一层：静态 AST 扫描（不执行代码）
  ├── 识别 open/pathlib/os/time.sleep/subprocess/requests/httpx
  ├── event loop 可达性分析（async 函数 → 同步调用链 BFS）
  └── AgentMiddleware 同步钩子检测

第二层：运行时硬门禁（Blockbuster）
  ├── 包裹 pytest_runtest_protocol（setup+call+teardown）
  ├── 只对 blocking_io/ 目录激活
  └── scanned_modules=("app","deerflow")（排除测试基础设施）

第三层：git diff 交集（回答"本次改动引入了哪些"）
  ├── changed-line 选择（unified diff + 行号匹配）
  └── new-vs-base 选择（跨文件盲区：新增 async caller 暴露旧 sync helper）
```

### 5.5 跨 Org 泄露零容忍

```
测试设计（test_org_isolation.py）：
├── 同一 SHARED_USER 在两个 Org 上下文（排除 user filter 混淆变量）
├── 绕过 HTTP，直击存储层 org filter（_as_org context manager）
├── 覆盖 threads_meta / runs / run_events / feedback
├── 三类断言：
│   ├── 跨 Org 读隔离（get → None）
│   ├── 跨 Org 写隔离（update/delete → no-op）
│   └── run_events 内容泄露（"最敏感泄露向量"）
├── fail-closed 证明（AUTO_ORG + 无绑定 → RuntimeError）
└── escape hatch 证明（org_id=None → system-admin 可见全部）
```

> **跨 Org 泄露 = 阻断级缺陷，零错误预算。**

---

## 6. 技术选型与取舍

### 6.1 后端技术栈

| 领域 | 选型 | 取舍理由 |
|---|---|---|
| **语言** | Python 3.12+ | 上游 DeerFlow 基线，异步生态成熟 |
| **Web 框架** | FastAPI | async-first，OpenAPI 自动生成，与 LangGraph SDK 兼容 |
| **Agent 编排** | LangGraph + LangChain | 上游核心资产，不重写 |
| **包管理** | uv workspace | 快速解析，monorepo workspace 支持 |
| **数据库** | PostgreSQL 15+（asyncpg） | JSONB、UNIQUE NULLS NOT DISTINCT、触发器、RLS 就绪 |
| **缓存/协调** | Redis（非权威） | Stream + SET NX EX + Lua CAS |
| **ORM** | SQLAlchemy 2.0 async | 异步原生，Alembic 迁移 |
| **观测** | OpenTelemetry + Prometheus + Langfuse | 双 Tracing provider 支持 |

### 6.2 前端技术栈

| 领域 | 选型 | 取舍理由 |
|---|---|---|
| **框架** | Next.js 16（App Router） | SSR + RSC，与 FastAPI cookie 认证天然契合 |
| **UI** | React 19 + Tailwind 4 + shadcn/ui | 复制式组件（可定制）vs 黑盒组件库 |
| **Server State** | TanStack Query | 缓存/invalidation/optimistic 标准化 |
| **Agent SDK** | @langchain/langgraph-sdk | `useStream` hook 封装 SSE 细节 |
| **包管理** | pnpm 10 | 硬链接节省磁盘，严格 hoisting |
| **测试** | Playwright + Rstest | E2E（含 real-backend 契约）+ 单元 |

### 6.3 关键取舍

**为什么用触发器而非 GRANT/REVOKE 做审计防篡改？**
harness 当前只用单一 owner DSN，没有角色配置机制。触发器让保证"dialect-portable and CI-testable"——完整的角色级权限隔离推迟到 ops runbook。这是诚实的工程取舍。

**为什么 Redis 不做权威？**
Redis 是易失的（可被 flush、重启丢数据）。把终态权威放在 PostgreSQL，让 Redis 只做协调加速，意味着 Redis 故障不会导致终态丢失。reconciler 能根据 PG 恢复一切。这简化了运维心智负担。

**为什么 contracts 层禁止 ORM/FastAPI 导入？**
契约是稳定边界。如果 contracts 依赖了 ORM，那么数据库 schema 变更会迫使契约变更，进而迫使所有消费者升级。白名单 + CI AST 扫描确保了契约层的纯净性——这是整个 fork 演进策略的技术基石。

---

## 7. 演进路线与未来展望

### 7.1 当前进度（截至 2026-08）

```
Track 0  ████████████████████ 100% — Fork 基线就绪
Track A  ████████████████████ 100% — Contracts + TenantContext 出口达成
Track B  ███████████████████░  95% — multi-org Feature Flag 三态就位（PR-025D 待开始）
Track C  ████████████████████ 100% — RBAC + 机器身份 + 撤权 SSE
Track D  ███████████████████░  90% — 9 类审计事件 8/9 落地（缺 policy.approval.required）
Track E  ████████████████████ 100% — Agent 制品 + Release 全交付
Track F  ████████████████████ 100% — Console + 观测 + CI/CD Gate
Track G  ████████████████████ 100% — Run 控制 + Worker（PR-077 listener follow-up）
```

### 7.2 架构预留点

| 预留 | 状态 | 何时激活 |
|---|---|---|
| **Policy 微服务** | 契约已定义（PolicyEvaluator Protocol） | 独立 Policy 服务后 |
| **Approval 工单** | ApprovalTicket 中断 + resume_token_ref | require_approval 有生产者后 |
| **实时账单** | UsageRecord + 归因 | Billing 系统接入后 |
| **Registry 市场** | 前端路由占位 | 公共 Agent 市场阶段 |
| **物理 Worker 拆分（Profile W）** | Dispatcher/Executor Protocol 已接口化 | 硬/普通触发条件满足后 |
| **Workspace 级 RBAC** | 数据模型预留 workspace_id | MVP 后阶段 |

### 7.3 上游同步策略

- 上游安全补丁**优先合入**
- 季度盘点"仅企业"harness 补丁行数——过高则启动方案 B 评估
- `UPSTREAM_BASE` 文件记录 fork 基线 commit，便于 rebase 冲突解决

### 7.4 风险与缓解

| 风险 | 缓解 |
|---|---|
| Fork 漂移上游 | `upstream-sync.md` 定义的季度盘点 + 最小企业钩子原则 |
| Redis 单点故障 | Redis 非权威，PG reconcile 兜底；fail-closed 而非 fail-open |
| Schema 迁移卡死 | Expand/Contract 分离 + 每步可回滚 + doctor 检测迁移阶段 |
| 契约破坏性变更 | 版本化契约 + 兼容窗口 + CI 契约测试 |
| 跨 Org 泄露 | 零错误预算 + 存储层直击测试 + AST 边界扫描 |

---

## 附录：文档导航

| 文档 | 内容 | 权威性 |
|---|---|---|
| `docs/adr/0001~0006` | 架构决策记录 | Accepted（决策源） |
| `docs/architecture/runtime-contracts.md` | 稳定契约规范 v1alpha1 | 契约权威 |
| `docs/architecture/target-architecture.md` | 目标架构蓝图 | 设计权威 |
| `docs/architecture/api-boundaries.md` | API 边界与端点目录 | 接口权威 |
| `docs/architecture/data-model.md` | 数据模型 | Schema 权威 |
| `docs/engineering/progress.md` | PR 落地进度账本 | **单一权威**（状态以 origin/main 为准） |
| `docs/engineering/pr-split-guide.md` | PR 拆分指南 | 工程纪律 |
| `docs/security/threat-model.md` | STRIDE 威胁模型 | 安全权威 |
| `docs/ops/production-runbook.md` | 生产运维手册 | 运维权威 |

---

> **最后的话**：DeerNexus 的架构哲学可以用一句话概括——**"在已验证的 Agent 运行内核之上，用稳定契约注入企业控制面，让安全不变量成为不可绕过的默认行为。"** 每一个设计决策都服务于这个目标：从 ContextVar 的 fail-closed，到 CAS 的单一赢家，到审计的三层防御，到 doctor 的退出码门禁。这不是偶然的堆砌，而是从顶层哲学到代码实现的系统化贯彻。
