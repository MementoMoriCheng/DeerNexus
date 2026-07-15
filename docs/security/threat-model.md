# DeerNexus 威胁模型

> 状态：MVP 威胁模型 v0.1  
> 方法：STRIDE + 资产 / 信任边界 / Abuse Case  
> 关联：[安全基线](baseline.md) · [目标架构](../architecture/target-architecture.md) · [ADR-0002](../adr/0002-tenant-workspace-keys.md) · [ADR-0003](../adr/0003-rbac-and-service-accounts.md) · [ADR-0004](../adr/0004-agent-artifacts-and-release.md) · [ADR-0005](../adr/0005-audit-event.md) · [测试策略](../engineering/testing-strategy.md)

本文识别 DeerNexus Enterprise Agent OS 的主要攻击面、威胁、控制和残余风险。它不宣称穷尽所有攻击，也不替代代码级 Security Review、渗透测试和供应商安全评估。

---

## 1. 范围

### 1.1 包含

- Browser / CLI / SDK / API Client；
- Reverse Proxy、Gateway、Runtime / Admin / Studio API；
- OIDC、Session、ServiceAccount、API Key；
- Organization、Workspace、RBAC；
- RunManager、Worker、Scheduler、IM / Webhook；
- Agent、Tool、Skill、MCP、Connector；
- Sandbox、Provisioner；
- PostgreSQL、Redis、对象存储、Secret Store；
- Agent 制品、ReleaseChannel、Catalog；
- Audit、Usage、日志、Metrics、Trace；
- CI/CD、镜像、依赖和上游同步；
- 备份、恢复和运维 Break-glass。

### 1.2 不包含

- 外部模型供应商内部实现；
- 企业 IdP 内部控制；
- 云厂商底层物理安全；
- 尚未进入 MVP 的公共 Registry、账单和完整 KB；
- 客户自有 Tool / MCP 的内部系统，但包含 DeerNexus 与其交互的边界。

外部系统不在控制范围内不代表其风险可忽略；相关风险通过合同、最小数据、网络和凭证控制降低。

---

## 2. 安全目标

1. OrgA 不能观察、推断或修改 OrgB 的资源和运行数据；
2. 主体不能通过伪造上下文、缓存、异步任务或兼容路由扩大权限；
3. Agent 内容不能自行获得控制面权限；
4. 高风险 Tool / MCP 只能在 RBAC 与 Policy 同时允许时执行；
5. prod Run 使用已发布且摘要匹配的不可变制品；
6. Secret 不进入普通存储、Prompt、日志、Audit payload 和制品；
7. Sandbox 故障或逃逸不能直接取得宿主、集群和控制面权限；
8. 关键管理变更和安全拒绝可归因、可靠写入且普通应用不可改写；
9. 重试、故障恢复和 Worker 切换不静默重复外部副作用；
10. 单租户和恶意输入不能无限消耗共享资源。

---

## 3. 资产

| 资产 | 分类 | 安全属性 |
| --- | --- | --- |
| Organization / Membership / RoleBinding | Confidential | 完整性、隔离、可用性 |
| Session、API Key、OAuth Token、KMS Key | Restricted | 机密性、完整性、可撤销 |
| Prompt、Response、Thread、Memory | Confidential | 机密性、隔离、保留 |
| Run、Checkpoint、Artifact | Confidential | 隔离、完整性、可恢复 |
| AgentVersion / Skill / MCP 配置 | Confidential | 来源、摘要、不可变、可用性 |
| ReleaseChannel / ReleaseRef | Confidential | 完整性、原子性、可追踪 |
| Tool / Connector 凭证 | Restricted | 最小授权、短期、可撤销 |
| AuditEvent / ReleaseEvent | Confidential | 完整性、可归因、保留 |
| UsageRecord | Internal / Confidential | 正确归因、完整性 |
| PostgreSQL / Redis / Object Store | Restricted | 机密性、完整性、恢复 |
| 日志 / Trace / Crash Dump | Confidential | 脱敏、受控访问、保留 |
| CI 身份、镜像、SBOM、签名 | Restricted | 来源、完整性、最小权限 |
| 备份与归档 | Restricted | 加密、完整性、恢复、保留 |

---

## 4. 威胁主体

| 主体 | 能力假设 |
| --- | --- |
| 未认证互联网攻击者 | 可调用公网端点、发送恶意 URL / 文件 / Webhook、枚举资源 |
| 恶意或失陷租户用户 | 拥有合法低权限账号，尝试跨 Org、提权和资源耗尽 |
| 恶意 Agent / Prompt 内容 | 可影响模型输出，诱导 Tool、MCP、网络和数据访问 |
| 失陷 Tool / MCP / Connector | 可返回恶意内容、滥用凭证、访问不期望网络 |
| 供应链攻击者 | 污染依赖、镜像、Agent / Skill 包、上游补丁或 CI |
| 失陷 Worker / Sandbox | 尝试访问控制面、其他租户或宿主资源 |
| 内部普通运维人员 | 有有限生产访问，可能误操作或越权 |
| 特权管理员 | 有数据库、KMS 或对象存储高权限；需要职责分离与审计 |
| 外部供应商 | 模型、IdP、云和 SaaS 发生故障、泄露或恶意行为 |

不假设客户端、模型输出、Agent 制品、外部事件或 MCP 返回内容可信。

---

## 5. 信任边界与数据流

```text
TB1 Internet / Enterprise Client
  → Reverse Proxy
TB2 Reverse Proxy
  → Gateway
TB3 Gateway Authenticated Context
  → Runtime / Admin / Studio Services
TB4 Runtime
  → Worker / Queue / Redis
TB5 Worker
  → Sandbox / Tool / MCP / Model
TB6 Application
  → PostgreSQL / Object Store / Secret Store
TB7 CI
  → Registry / Deployment Environment
TB8 Operations
  → Break-glass / Backup / Audit Archive
```

### 5.1 每次跨界必须回答

1. 调用方身份是什么；
2. Org / Workspace 从哪里解析；
3. 数据和指令是否完整；
4. 最小权限是什么；
5. 超时、重试和失败是否安全；
6. 是否包含 Secret 或敏感内容；
7. 事件如何关联和审计；
8. 对端失陷时最大影响范围是什么。

---

## 6. 风险方法

### 6.1 评分

- Likelihood：1（罕见）至 5（高度可能）；
- Impact：1（有限）至 5（跨租户、重大合规或平台级影响）；
- Score = Likelihood × Impact。

| Score | 等级 | 处理 |
| --- | --- | --- |
| 20–25 | Critical | 生产前消除或隔离；不接受普通豁免 |
| 12–19 | High | MVP 有验证控制；残余风险具名批准 |
| 6–11 | Medium | 计划控制、监控和 Owner |
| 1–5 | Low | 接受或常规改进 |

安全基线中的不可豁免项优先于分数。

---

## 7. 威胁登记表

### 7.1 身份、租户与授权

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-001 | S/E/I | 客户端伪造 `org_id` / Workspace | Critical | 认证映射、TenantContext、从属校验、不信请求体 | TEN-入口、API 跨 Org |
| TM-002 | I/E | 主键查询漏 `org_id` 导致 IDOR | Critical | 仓储强制复合范围、RLS 可选纵深、隔离矩阵 | 测试策略 §6 |
| TM-003 | I/E | ContextVar / 连接池复用串租户 | Critical | finally 清理、显式任务信封、TEN-009 | 测试策略 §7 |
| TM-004 | S | 伪造 / 过期 OIDC Token | High | iss/aud/signature/exp/nbf/alg、JWKS 受控刷新 | Auth 契约与安全测试 |
| TM-005 | E | OIDC group 自动映射高权限 | High | issuer/group allowlist、禁止 system 权限、dry-run | ADR-0003 §10 |
| TM-006 | E | 过期 RBAC 缓存继续授权 | High | TTL≤60s、主动失效、高风险强读取 | 撤权 P99≤60s |
| TM-007 | S/E | API Key 泄露或 scope 扩大 | High | 单向存储、单 Org、scope 交集、过期、轮换、限流 | IAM / API Key 套件 |
| TM-008 | E | 最后管理员被移除或恶意接管 | High | 最后 admin 保护、system-admin 专用恢复、双人记录 | ADR-0003 测试 |
| TM-009 | E/R | system-admin 跨 Org 滥用 | High | 专用接口、MFA / Break-glass、reason、强审计 | system 跨 Org 事件 |
| TM-010 | I/E | Compatibility API 绕过企业中间件 | Critical | 统一 Auth/Tenant/RBAC/Audit、默认拒绝新路由 | Compatibility 契约 |

### 7.2 Agent、Policy、Tool 与外部连接

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-011 | E | Prompt 注入诱导未授权 Tool | Critical | RBAC、Tool 风险元数据、实时 Policy、参数 Schema | Policy deny / Tool 测试 |
| TM-012 | I | 模型输出或 MCP 内容被当可信指令 | High | 全部外部内容不可信、结构化解析、权限不随内容变化 | 恶意 MCP / Prompt 测试 |
| TM-013 | I/E | 调用方降低 Tool `risk_class` | High | 注册表风险为下限、调用方不可覆盖 | 契约负向测试 |
| TM-014 | I/E | `require_approval` 被普通 resume 绕过 | High | 独立状态、resume 重评估、未启用时安全中断 | Approval 预留测试 |
| TM-015 | S/I | Webhook 伪造或重放 | High | 签名、5 分钟窗口、event id 幂等、Body 原文验签 | SEC Webhook |
| TM-016 | I | MCP / Connector 返回超大或恶意响应 | High | 大小 / 超时 / Schema、Sandbox、输出视为不可信 | SEC MCP |
| TM-017 | E/I | Connector Secret 被 Agent 或日志读取 | Critical | secret_ref、最小注入、短期凭证、脱敏 | Secret scan / E2E |

### 7.3 网络与 Sandbox

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-018 | I/E | SSRF 访问 metadata、集群或数据服务 | Critical | egress deny、DNS/IP/redirect 复核、代理、地址阻断 | 测试策略 §18.2 |
| TM-019 | E | DNS rebinding / 编码 IP 绕过 | High | 连接 IP 与校验结果一致、全表达覆盖 | SSRF 专项 |
| TM-020 | E/I | Sandbox 逃逸取得宿主 / 集群权限 | Critical | 非 root、只读根、最小 capabilities、无 socket / 管理凭证 | Sandbox 安全测试 |
| TM-021 | I | Sandbox 跨 Org 复用残留数据 | Critical | MVP 不跨 Org 复用有状态实例、完全重置、quarantine | Pool 隔离测试 |
| TM-022 | D | Fork bomb / 磁盘 / 网络耗尽 | High | CPU、内存、PID、磁盘、时间、输出和网络限制 | 资源限制与故障注入 |
| TM-023 | I/E | Sandbox 获得过量 Secret | Critical | Run 级最小 Secret、短期、结束撤销、独立身份 | Secret profile 测试 |

### 7.4 Run、Worker 与一致性

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-024 | S/T | RunEnvelope 伪造或篡改 | Critical | 同库可信来源；跨边界 EnvelopeIntegrity | contracts / queue 测试 |
| TM-025 | T | 重放 RunEnvelope 创建重复 Run | High | idempotency key、原子 claim、Run 状态校验 | 重放测试 |
| TM-026 | T/R | 多 Worker 同时执行同一 Run | High | lease token、CAS、heartbeat、reconcile | Profile H 故障注入 |
| TM-027 | T | cancel 与 completion 竞争破坏终态 | High | terminal 不可逆、CAS、持久化 cancel intent | Run 状态机测试 |
| TM-028 | T/I | Worker 故障重复外部副作用 | Critical | 至少一次语义、幂等声明、不可证明幂等不自动重放 | 故障注入 / 人工处理 |
| TM-029 | I | Redis 旧状态覆盖 PostgreSQL | High | PostgreSQL 权威、恢复后清 lease、terminal 优先 | 恢复演练 |
| TM-030 | I/E | legacy unpinned Run 在 prod 恢复 | High | 409 `release_unpinned`、只读 / 取消 / 归档 | Release 门禁 |

### 7.5 制品与供应链

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-031 | T | 修改已发布 AgentVersion | Critical | digest、published 不可变、数据库约束、对象对账 | ADR-0004 测试 |
| TM-032 | T | Channel 并发更新或跨 Org 引用 | High | CAS、复合外键、Org 校验、ReleaseEvent | Promote 并发测试 |
| TM-033 | T | 文件态最新版本绕过 prod | Critical | prod 只解析持久化 ReleaseRef | 反向门禁测试 |
| TM-034 | T/E | 恶意 Skill / MCP 包 | High | 来源、digest、扫描、依赖锁、Manifest、Sandbox | 制品安全套件 |
| TM-035 | T | 对象缺失 / 摘要不匹配 | High | 执行前校验、inventory、告警、拒绝运行 | Object 对账 |
| TM-036 | E/T | Dependency confusion / typosquatting | High | 受控源、namespace、lock、SCA、SBOM | CI 供应链扫描 |
| TM-037 | T | CI / 第三方 Action 被污染 | Critical | 短期身份、Action 固定 commit、最小权限、环境保护 | CI 配置审计 |
| TM-038 | T | 上游恶意或不兼容变化 | High | 固定基线、Sync PR、差异分类、全量回归 | 上游同步演练 |

### 7.6 数据、审计与可观测

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-039 | I | Secret / Prompt 进入日志或 Trace | Critical | 字段白名单、调用点脱敏、禁止完整内容、扫描 | 日志安全测试 |
| TM-040 | R/T | 高风险操作无 Audit 或被改写 | Critical | 同事务 outbox、append-only、独立 DB Role、归档摘要 | ADR-0005 测试 |
| TM-041 | D/T | Audit outbox 积压 / 丢失 | High | 背压、dead letter、P1/P2 告警、幂等恢复 | 故障测试 |
| TM-042 | I | Signed URL 越权或长期有效 | High | 授权后生成、短 TTL、私有 Bucket、路径 Org namespace | Artifact 测试 |
| TM-043 | I | 备份 / 测试数据泄露 | Critical | 加密、权限分离、脱敏、受控恢复环境 | 恢复演练 |
| TM-044 | I | Metrics 高基数标签泄露 Org / User | Medium | 公共 Metrics 禁止 org_id / principal_id | Metrics schema 检查 |
| TM-045 | T | Audit 最高权限管理员联合篡改 | High | 权限分离、Object Versioning / Lock、摘要、变更审计 | 残余风险，后续外部证明 |

### 7.7 可用性

| ID | STRIDE | 威胁 | 初始风险 | 核心控制 | 验证 |
| --- | --- | --- | --- | --- | --- |
| TM-046 | D | 单 Org 占满 Run / Sandbox / SSE | High | Org 公平限流、并发上限、背压 | 多 Org 压测 |
| TM-047 | D | 超大 Prompt / Artifact / MCP 响应 | High | 请求、对象、响应大小上限 | 边界测试 |
| TM-048 | D | 数据库 / Redis 重试风暴 | High | 有界退避、readiness、停止领取新 Run | 故障注入 |
| TM-049 | D | IdP 故障导致错误绕过 | High | 新登录 fail-closed、已有 Session 有界继续 | IdP 故障测试 |
| TM-050 | D | Sandbox Pool 耗尽 | Medium | pending、容量告警、quarantine、公平调度 | Runbook 演练 |

---

## 8. 关键 Abuse Case

### AC-01：低权限用户跨 Org 读取 Run

1. 攻击者从日志、URL 或枚举获得另一个 Org 的 `run_id`；
2. 使用合法 OrgB Session 请求 OrgA Run；
3. 尝试详情、SSE、Artifact、Console、分页和 Signed URL 旁路。

安全结果：

- 仓储按 `org_id + run_id` 查询；
- 所有入口返回 404；
- 不暴露存在性；
- 重复探测触发速率或安全告警；
- Signed URL 不生成。

### AC-02：Prompt 注入执行高风险 Tool

1. 不可信文档要求模型忽略策略；
2. 模型生成 Tool 调用；
3. Tool 目标为外部写入或内部地址。

安全结果：

- 模型输出不授予权限；
- RBAC 与实时 Policy 同时评估；
- Tool 风险不能被调用方降低；
- SSRF / egress 再次阻断网络目标；
- deny 产生 AuditEvent。

### AC-03：Worker 崩溃后重复支付 / 写入

1. Tool 已完成外部副作用；
2. Worker 在提交状态前崩溃；
3. Lease 到期后新 Worker 领取 Run。

安全结果：

- Tool 必须有幂等键或明确 non-idempotent；
- 无法证明幂等时不自动重放；
- Run 进入人工处理或安全失败；
- Audit / Run journal 保存已知步骤；
- 不以“最终成功”为由掩盖不确定状态。

### AC-04：恶意 Agent 制品进入 prod

1. 攻击者上传含路径穿越、危险脚本或未锁定依赖的 Agent；
2. 尝试直接把 Channel 指向 draft；
3. 尝试修改已发布对象或替换同版本。

安全结果：

- Manifest / 路径 / 依赖 / digest 门禁；
- prod 只接受 published；
- published 内容不可变；
- CAS 更新 Channel；
- 对象摘要不匹配拒绝执行并告警。

### AC-05：内部管理员删除审计证据

1. 普通应用或运维账号尝试 UPDATE / DELETE audit_events；
2. 尝试删除未归档分区或对象版本；
3. 尝试用 correction 覆盖主体。

安全结果：

- 普通角色无 UPDATE / DELETE；
- 分区与归档使用专用角色和变更管理；
- 归档摘要、版本和 Object Lock 提供检测；
- correction 追加且关联原事件；
- 最高权限联合管理员风险保留为已知残余风险。

---

## 9. 控制追踪

| 控制域 | 权威文档 | 主要测试 |
| --- | --- | --- |
| TenantContext / Org 归属 | ADR-0002、Runtime Contracts | TEN、隔离矩阵 |
| RBAC / ServiceAccount | ADR-0003、API Boundaries | IAM、API 契约 |
| Policy / Tool | Runtime Contracts、安全基线 | Policy / SEC |
| Agent 制品 / Release | ADR-0004、Data Model | REL、E2E rollback |
| Audit | ADR-0005 | AUD、归档恢复 |
| Sandbox / SSRF | 安全基线 | SEC-SSRF、Sandbox |
| Run / Worker 一致性 | ADR-0006、生产 Runbook | RUN、Profile H |
| CI / 供应链 | CI/CD、上游同步 | SAST / SCA / SBOM / Sync |
| 备份 / 恢复 | 生产 Runbook、容量与灾备 | OPS restore |
| 观测数据 | 可观测性与 SLO | Metrics / log schema |

每个 Critical / High 威胁在实现 PR 中必须引用至少一个测试 ID 或具名验证证据。

---

## 10. 残余风险

### 10.1 MVP 接受但必须记录

| 风险 | 原因 | 补偿控制 | 复审触发 |
| --- | --- | --- | --- |
| 外部模型供应商处理数据 | 平台无法控制供应商内部 | 数据最小化、合同、配置、区域选择 | 新供应商 / 新数据分类 |
| 数据库 + KMS + 对象存储最高权限联合管理员 | MVP 无外部公证 | 职责分离、Object Lock、摘要、强审计 | 强合规客户 |
| Sandbox / Runtime 零日漏洞 | 无法完全消除 | 隔离、最小权限、网络 deny、快速补丁 | 新 CVE / 逃逸迹象 |
| 至少一次执行导致不确定外部副作用 | 分布式故障固有 | 幂等键、non-idempotent 不自动重放 | Tool 类型扩大 |
| 单区域故障 | MVP 不做跨区域多活 | 加密备份、RPO/RTO、恢复演练 | RTO 需求提高 |
| PostgreSQL RLS 非强制 | 上游兼容与连接池需验证 | 应用强过滤、复合查询、隔离套件 | 首次生产 / 高敏租户 |
| Agent 包未具备完整签名供应链 | MVP 只做 digest / 来源 / 扫描 | 私有 Registry、CI 身份、发布门禁 | 公共 Registry |

残余风险必须有 Owner、复审日期和适用环境；不能只写“已知风险”。

### 10.2 不接受

- 已知跨 Org 读取 / 写入路径；
- 明文生产 Secret；
- 未认证 Admin / Studio API；
- 生产 host bash；
- prod 读取文件系统最新草稿；
- Critical 审计写路径静默丢失；
- 已知 Sandbox 逃逸仍继续生产执行；
- 为通过测试关闭 Tenant / RBAC / ReleaseRef 门禁。

---

## 11. 安全验证计划

### 每 PR

- Boundary、Tenant Core、Authn/Authz；
- Secret scan、SAST、SCA；
- 受影响的 SSRF / Sandbox / Release / Audit 测试；
- OpenAPI 与 Runtime Contracts diff；
- 新外部 URL / Secret / Tool 权限专项 Review。

### Nightly

- 完整隔离矩阵；
- SSRF 编码、重定向、DNS rebinding；
- Webhook replay；
- 双副本故障注入；
- 恶意 Agent / MCP fixture；
- 日志 / Trace 敏感字段扫描。

### Release

- 生产配置 doctor；
- 制品 digest、SBOM、漏洞与来源；
- v1 → v2 → rollback；
- 双 Org E2E；
- Backup / Audit 水位；
- 有效豁免与残余风险复核。

### 季度或重大变化

- 恢复演练；
- Sandbox / Network Policy 复核；
- Break-glass 演练；
- 上游同步与依赖风险；
- 权限和审计访问复核；
- Threat Model 更新。

---

## 12. 评审触发

发生以下任一情况必须更新本文：

- 新增外部模型、Tool、MCP、Connector 或 Webhook；
- 新增数据分类、知识库或跨区域传输；
- Gateway / Worker 物理拆分；
- 启用 Workspace 级 RBAC；
- 引入公共 Registry / 市场；
- 改变 Secret、KMS、对象存储或 Sandbox 实现；
- 新增 system-admin 或 Break-glass 能力；
- 发生 P1 安全事件；
- Critical / High 漏洞暴露新攻击路径；
- 上游同步改变 Auth、Router、Store、Run 或 Sandbox；
- 年度安全评审到期。

---

## 13. Owner

| 区域 | Owner |
| --- | --- |
| Tenant / IAM | Control Plane + Security |
| Runtime / Worker | Runtime + Platform |
| Sandbox / Network | Platform Security |
| Tool / MCP / Connector | Extension Owner + Security |
| Release / Supply Chain | Studio + Platform Engineering |
| Audit / Data | Control Plane + Security |
| Backup / DR | SRE / DBA |
| Threat Model 总维护 | Security Architecture |

具体人员在 Fork 初始化后写入 CODEOWNERS 和文档元数据。

---

## 14. 验收

- [ ] 所有生产信任边界有身份、Org、完整性和失败语义
- [ ] Critical / High 威胁均映射控制与测试
- [ ] 不接受风险为零
- [ ] 残余风险有 Owner、环境和复审日期
- [ ] 安全基线不可豁免项与本文一致
- [ ] 关键 Abuse Case 进入测试或演练
- [ ] Release Gate 检查新增外部接口和高权限依赖
- [ ] P1 事件后更新威胁、控制和回归测试
