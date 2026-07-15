# DeerNexus 生产安全基线

> 状态：MVP 强制基线  
> 适用范围：Gateway、Control Plane、Runtime、Worker、Sandbox、PostgreSQL、Redis、对象存储、MCP / Connector、前端与部署流水线  
> 关联：[目标架构](../architecture/target-architecture.md) · [ADR-0002](../adr/0002-tenant-workspace-keys.md) · [运行时契约](../architecture/runtime-contracts.md) · [API 边界](../architecture/api-boundaries.md) · [威胁模型](threat-model.md) · [数据治理](../compliance/data-governance.md) · [生产 Runbook](../ops/production-runbook.md)

本文定义 DeerNexus 进入生产环境前必须满足的最低安全要求。标记为“必须”的条款不得以功能进度为由跳过；确需例外时按 §17 的豁免流程处理。

---

## 1. 安全目标

1. 跨 Organization 数据泄露为零，不设错误预算。
2. 未认证、未授权或上下文不完整的请求默认拒绝。
3. Agent、Tool、Skill、MCP 和 Sandbox 不因运行能力获得控制面权限。
4. Secret 不进入源码、普通数据库字段、日志、审计 payload、Agent Prompt 或制品。
5. 生产 Run 可追溯到主体、Org、Release digest、策略版本与关键审计事件。
6. 单个租户、Run、工具或 Sandbox 故障不能无限消耗平台资源。
7. 安全补丁、事件响应、备份恢复和访问撤销具备明确时限。

---

## 2. 信任边界与主要威胁

### 2.1 信任边界

```text
Internet / Enterprise Network
  → Reverse Proxy / Gateway
    → Runtime API / Admin API / Studio API
      → Control Plane Services
      → Runtime Kernel
        → Tool / MCP / Connector
        → Sandbox
      → PostgreSQL / Redis / Object Store / Secret Store
```

以下跨越均视为信任边界：

- 浏览器或 API 客户端 → Gateway；
- Gateway → Worker / Sandbox Provisioner；
- Runtime → 外部模型、MCP、Tool 和 Connector；
- 应用 → 数据库、Redis、对象存储和 Secret Store；
- CI → 镜像仓库和生产部署；
- 外部 IM / Webhook → Channel Adapter。

### 2.2 MVP 威胁清单

| 威胁 | 最低控制 |
| --- | --- |
| 跨 Org IDOR / 查询漏过滤 | TenantContext、仓储强制 `org_id`、双 Org 回归套件 |
| 伪造 Org / Workspace | 只信认证映射，不信请求体；验证从属关系 |
| Prompt / Tool 注入 | Tool 权限、参数 Schema、Policy、Sandbox、出站限制 |
| SSRF / 云元数据访问 | URL 校验、DNS/IP 复核、egress deny、阻断元数据地址 |
| Sandbox 逃逸 | 非 host bash、非 root、资源限制、隔离运行时、最小挂载 |
| Secret 泄露 | Secret Store 引用、日志脱敏、短期凭证、轮换 |
| MCP / Skill 供应链 | 来源、摘要、扫描、版本锁定、生产发布门禁 |
| API Key 滥用 | 单向哈希、scope、过期、撤销、限流、审计 |
| Webhook 重放 | 签名、时间窗、nonce / event id 幂等 |
| 审计改写 | append-only、独立数据库权限、外部归档 |
| 资源耗尽 | Org / Principal / Run 限制、超时、队列背压、Sandbox quota |
| 上游漏洞 | 固定基线、CVE SLA、同步与回归流程 |

完整 STRIDE、Abuse Case、控制追踪与残余风险见[威胁模型](threat-model.md)；本文仍是生产强制控制基线，威胁模型不能降低本文件的不可豁免项。

---

## 3. 身份认证

### 3.1 OIDC

- 生产必须启用 OIDC 或经安全评审的企业本地身份方案；
- 验证 `iss`、`aud`、签名、`exp`、`nbf` 和允许的算法；
- 禁止接受 `alg=none`，禁止信任客户端提交的 claims；
- JWKS 缓存必须有过期与刷新策略；未知 `kid` 触发受控刷新，不回退跳过验签；
- IdP 不可用时，已有未过期 Session 可按策略继续；新登录 fail-closed；
- `issuer + subject` 是外部身份唯一键，Email 不作为授权主键；
- OIDC groups 到 Role 的映射必须 allowlist，不允许任意 group 名自动成为权限；
- 登录成功、失败异常和高风险映射变更产生安全日志；成功登录按 AuditEvent 目录记录。

### 3.2 浏览器 Session

- Cookie 必须 `Secure`、`HttpOnly`、合理 `SameSite`；
- Session ID 至少 128 bit 随机熵，不在 URL 中传递；
- MVP 默认绝对时长不超过 12 小时，空闲超时不超过 30 分钟；企业策略可以更严格；
- 登出、用户禁用、Membership / 关键角色撤销、ServiceAccount 禁用和 API Key 撤销必须在 **60 秒内**使相关 Session / 权限缓存失效；这是 MVP 基线上限，[ADR-0003](../adr/0003-rbac-and-service-accounts.md)不得放宽；
- 所有使用 Session 凭证的写操作启用 CSRF 防护，登录、权限提升、API Key 创建和发布必须纳入专项测试；
- 敏感管理操作建议要求近期认证或 IdP MFA 结果。

### 3.3 API Key

- 只绑定 ServiceAccount 和单一 Org；
- 明文只在创建成功时展示一次；
- 数据库保存 prefix 与强单向哈希/HMAC 结果；
- 必须有 scope、创建者、过期时间、最后使用时间和撤销时间；
- MVP 默认最长有效期 90 天；更长周期需安全豁免；
- 支持重叠轮换，但旧 Key 的重叠期默认不超过 24 小时；
- Key 校验使用恒定时间比较或成熟密码库的等价安全实现；
- Key 不通过 query string、日志、Audit payload 或前端持久化存储传递；
- 创建、轮换、撤销和异常使用必须审计。

### 3.4 Workload Identity

- Internal API 使用 mTLS、短期工作负载身份或部署平台身份；
- 禁止共享长期静态“内部万能 Token”；
- 身份限定服务、环境和调用范围；
- 开发、预发、生产使用不同信任根或至少不同身份与 Secret；
- Internal API 网络上不可从公网直接访问。
- MVP 同进程调用可以不经过 HTTP；任何实际监听的 `/api/internal/v1/*` 均必须使用工作负载身份或 mTLS，禁止仅凭自定义 Header 建立信任。
- Break-glass 身份默认封存或禁用，启用时限时、最小权限、强审计并按生产 Runbook 定期演练。

---

## 4. 授权与租户隔离

### 4.1 强制规则

- Organization 是硬隔离边界；
- 只有 `active` Membership 能绑定该 Org 的 TenantContext；`invited` / `removed` 视为无活动成员资格并返回 404，`suspended` 返回 403 `permission_denied`；
- API Key 的 Org 不能由 Header、路径或请求体覆盖；
- 资源按 ID 查询仍必须使用 `org_id + resource_id`；
- 跨 Org 资源访问返回 404；当前 Org 内权限不足返回 403；
- Workspace 必须验证属于当前 Org；MVP 不引入 Workspace 级 RBAC；
- system-admin 使用独立权限和仓储接口，所有跨 Org 操作审计；
- `suspended` Org 拒绝新 Run 和发布，只保留受控读取、审计与导出；
- `deleting` Org 冻结新 Run、发布及普通写操作，只允许删除流程、受控导出、审计和取消现有 Run；`deleted` Org 对普通主体不可见。

### 4.2 纵深防御

应用层 `org_id` 过滤是必选。PostgreSQL RLS 可作为纵深防御，但不能替代应用过滤。若启用 RLS：

- 每个事务设置 tenant session context；
- 连接归还池前清理；
- system job 使用独立数据库角色；
- 连接池串租户测试必须进入 CI；
- 迁移、备份和审计归档路径需单独验证。

### 4.3 缓存与异步任务

- Redis Key 必须带 environment 和 `org_id` namespace；
- Worker 从可信 RunEnvelope 重建 TenantContext；
- 跨信任边界消息必须验证 EnvelopeIntegrity；
- Scheduler、IM、Webhook 未映射 Org 时拒绝执行；
- 禁止后台任务隐式“遍历所有租户”；必须显式 system scope 或逐 Org fan-out；
- ContextVar 在请求和任务结束时恢复，防止协程或线程复用污染。

---

## 5. Secret 与密钥管理

### 5.1 存储

生产 Secret 必须存放在：

- 云 Secret Manager / KMS；
- Vault；
- Kubernetes Secret + 静态加密与严格 RBAC；
- 经安全评审的等价系统。

数据库和配置文件只保存 `secret_ref`。`.env` 仅允许本地开发，不能提交版本库或作为生产长期 Secret 来源。

### 5.2 分类

至少包括：

- OIDC client secret；
- 数据库与 Redis 凭证；
- 对象存储凭证；
- 模型供应商 API Key；
- MCP / Connector OAuth Token；
- Webhook signing secret；
- Session signing / encryption key；
- RunEnvelope 完整性密钥（同进程 / 同库 MVP 模式可不启用；一旦跨消息队列、跨主机 Worker 或其他信任边界传输即为必配 Secret）；
- 镜像仓库和部署身份。

### 5.3 生命周期

- 创建：使用密码学安全随机源；
- 分发：通过工作负载身份或 Secret 挂载，不复制到工单和聊天；
- 使用：进程内最短保留，禁止写日志；
- 轮换：默认不超过 90 天；供应商支持短期令牌时优先短期令牌；
- 撤销：人员离职、主体禁用、疑似泄露时立即执行；
- 删除：确认所有消费者已切换，并保留必要审计元数据；
- 紧急轮换必须有 Runbook 和演练记录。

### 5.4 日志脱敏

至少识别并脱敏：

```text
Authorization
Cookie / Set-Cookie
api_key / token / secret / password
client_secret
connection string
signed URL query
OAuth code / refresh_token
```

禁止仅依赖正则作为唯一保护；日志调用点应默认不接收敏感对象。

---

## 6. 传输与静态加密

### 6.1 传输

- 外部流量必须 HTTPS；
- 最低 TLS 1.2，优先 TLS 1.3；
- 禁止弱密码套件和明文回退；
- Gateway 到托管 PostgreSQL、Redis、对象存储和外部模型优先使用 TLS；
- Internal 跨主机通信使用 mTLS 或受信网络加工作负载身份；
- 证书到期需提前告警，自动轮换失败视为高优先级事件。

### 6.2 静态

- PostgreSQL 磁盘、备份、对象存储和 Secret Store 必须加密；
- Redis 如承载可恢复 Stream / Run ownership 数据，其持久化卷和备份必须加密；
- 对象存储禁用公共访问；
- 签名下载 URL 短期有效，默认不超过 15 分钟；
- 客户管理密钥需求在 MVP 后评估，不影响平台默认加密要求。

---

## 7. 网络与 SSRF 防护

### 7.1 默认策略

- Sandbox、Tool、MCP 和 Connector 出站默认拒绝；
- 通过租户或工具级 allowlist 放行域名、协议和端口；
- 只允许 `https`，确需其他协议必须显式安全评审；
- 禁止直接访问控制面、数据库、Redis、Secret Store 和集群管理 API；
- DNS、代理和网络策略应共同执行，不只靠应用 URL 检查。

### 7.2 URL 校验

每次请求和重定向后都必须：

1. 解析规范化 URL；
2. 验证 scheme、host、port；
3. 解析 DNS；
4. 拒绝 loopback、link-local、private、multicast、unspecified 和保留地址，除非明确 allowlist；
5. 阻断云元数据地址和集群内部敏感域；
6. 限制重定向次数，并对新目标重新检查；
7. 防止 DNS rebinding：连接 IP 必须与校验结果一致或由受控代理完成；
8. 设置连接、读取和总超时以及响应大小上限。

典型阻断范围包括但不限于：

```text
127.0.0.0/8
::1/128
169.254.0.0/16
fe80::/10
RFC1918 private ranges（除非显式业务 allowlist）
云厂商 metadata endpoint
Kubernetes service / pod CIDR（除非显式内部 Connector）
```

### 7.3 Webhook

- 验证供应商签名；
- 时间偏差默认不超过 5 分钟；
- 使用 event id / nonce 幂等；
- Body 验签前不得修改；
- 请求体大小受限；
- 未识别 installation / channel binding 不落入默认 Org。

---

## 8. Sandbox 安全基线

### 8.1 禁止

- 生产启用 `allow_host_bash`；
- 以宿主 root 直接执行 Agent 命令；
- 挂载 Docker socket、Kubernetes 管理凭证或宿主敏感目录；
- 多 Org 复用同一可写工作目录；
- 无超时、无资源限制或无限出站网络；
- 把平台 Secret 全量注入 Sandbox；
- Sandbox 直接连接控制面数据库。

### 8.2 必须

- 使用 AIO、BoxLite、Kubernetes Provisioner 或经评审的等价隔离实现；
- 容器 / VM 非 root，删除不需要的 Linux capabilities；
- 只读根文件系统；临时写目录按 Run 或 Org 隔离；
- 明确 CPU、内存、PID、磁盘、执行时间和输出大小上限；
- 默认无出站网络，按 Tool / Connector 放行；
- 每个 Run 使用最小所需 Secret，优先短期凭证；
- Run 结束后回收 Sandbox、挂载、临时凭证和网络策略；
- 超时、OOM、逃逸迹象和回收失败产生指标与安全日志；
- 基础镜像固定 digest，定期扫描并重建。

### 8.3 池化

如使用 Sandbox Pool：

- 同一实例跨 Org 复用前必须证明完全重置；
- MVP 默认不跨 Org 复用有状态 Sandbox；
- Pool Key 至少包含隔离等级、镜像 digest、网络策略和 Secret profile；
- 回收失败实例进入 quarantine，不返回可用池。

---

## 9. Tool、Skill、MCP 与 Connector

### 9.1 注册与执行

- 每个扩展声明 Owner、来源、版本、digest、权限、风险等级和网络需求；
- 参数和返回值使用 Schema 校验；
- 高风险写操作、外部网络和凭证访问逐次 Policy 评估；
- 调用方不能降低工具声明的风险等级；
- Tool deny、require_approval 和策略异常产生 AuditEvent；
- 未启用审批中心时，`require_approval` 安全中断，不能降级为 allow。
- Policy 评估不可用时所有风险等级均按[运行时契约 §7.4](../architecture/runtime-contracts.md#74-超时和失败) fail-closed，MVP 不允许 low / medium fail-open。

### 9.2 MCP

- MCP Server 配置按 Org 隔离；
- 凭证只通过 `secret_ref` 注入；
- 远程 MCP 地址执行 SSRF 防护；
- 本地 MCP 进程在 Sandbox 或等价隔离环境运行；
- 限制可调用工具集合，不默认暴露全部能力；
- 记录 Server digest / 版本与调用关联 ID；
- Server 返回内容视为不可信输入。

### 9.3 Skill / Agent 包

- 文件导入后计算 SHA-256 digest；
- prod 只执行已发布 digest；
- prod 不得 admit、恢复或继续执行 `legacy_unpinned=true` 的 Run；此类存量 Run 只允许读取、取消和归档；
- Manifest 列出依赖、入口、工具、网络和 Secret 需求；
- 禁止制品覆盖已发布版本；
- 检查路径穿越、符号链接逃逸、可执行二进制和危险脚本；
- 依赖必须锁定版本；生产安装不从未固定的分支或 latest 拉取。

---

## 10. 数据分类与隐私

### 10.1 最低分类

| 级别 | 示例 | 控制 |
| --- | --- | --- |
| Public | 公开文档、公开模型元数据 | 完整性保护 |
| Internal | 非公开配置、普通运行指标 | 认证访问、加密 |
| Confidential | Prompt、Run 内容、Artifact、企业知识 | Org 隔离、最小权限、加密、受控导出 |
| Restricted | Secret、Token、敏感个人信息、密钥材料 | 专用 Secret Store、严格审计、禁止普通日志 |

### 10.2 处理

- Prompt、模型响应和 Artifact 默认视为 Confidential；
- 不因调用外部模型而默认允许发送所有上下文；
- Connector / Tool 只获得完成动作所需最小数据；
- Audit payload 记录事实，不复制完整敏感内容；
- 日志、Trace、错误平台使用采样和字段白名单；
- 删除、保留、legal hold、数据主体请求和备份中的延迟删除见[数据治理](../compliance/data-governance.md)；Audit 保留以 ADR-0005 为权威。

---

## 11. 审计安全

MVP 最低要求：

- `audit_events` append-only；
- 应用普通账号无 UPDATE / DELETE 权限；
- 高风险管理事务使用同事务 outbox 或失败回滚；
- 事件有 `event_id`、`idempotency_key`、Org、Actor、Action、Resource、Outcome、关联 ID 和时间；
- 时间源同步并监控漂移；
- 审计查询本身受 `admin:audit:read` 控制；
- 跨 Org 审计查询仅 system-admin 可用且再次审计；
- MVP 最低要求 PostgreSQL 中至少保留 90 天可查询热数据，并有可运行、可监控的归档作业；
- 生产总保留默认至少 365 天；法规或合同更严格时从严；
- 定期导出到权限隔离的对象存储，启用版本保留；Object Lock 在平台支持时启用；
- 更正使用追加 correction event，不修改原事件。

MVP 阻断验收的最小必报事件以[运行时契约 §10](../architecture/runtime-contracts.md#10-auditevent)为权威；API Key、Connector、Organization 等管理写路径按[测试策略 §14.1](../engineering/testing-strategy.md#141-auditevent)执行“关键写路径 100% 覆盖或具名豁免”。新增事件必须同步修改契约、测试策略和路线图，不能由本基线单独扩展枚举。[ADR-0005](../adr/0005-audit-event.md)负责扩展防篡改强度、legal hold 和最终保留策略，不得删减既有必报事件。

---

## 12. API 与前端

### 12.1 API

- 所有输入使用严格 Schema 和大小上限；
- 文件上传校验类型、大小、摘要和路径；
- Admin / Studio 写操作支持幂等与并发控制；
- CORS 使用明确 Origin allowlist；
- 带凭证写操作启用 CSRF 防护；
- 错误响应不暴露堆栈、SQL、内部路径、策略全文和资源所属 Org；
- 登录、Run 创建、发布、API Key 等关键接口限流；
- `429` 返回 `Retry-After`；
- Internal API 不以自定义 Header 作为唯一信任依据。
- Compatibility API 必须经过统一认证、TenantContext、RBAC 和审计中间件，不得绕过 prod ReleaseRef 门禁；
- 稳定错误码以[运行时契约 §12](../architecture/runtime-contracts.md#12-稳定错误模型)为准，HTTP 映射以[API 边界 §11.1](../architecture/api-boundaries.md#111-http-映射)为准。

### 12.2 前端

- 不把 API Key、Refresh Token 或 Connector Secret 保存到 localStorage；
- 不使用 `dangerouslySetInnerHTML` 渲染不可信模型输出，除非经过可靠消毒；
- Markdown、链接、图片和下载地址按不可信内容处理；
- 外部链接使用安全的 `rel` 设置；
- 管理 UI 的按钮隐藏不构成授权，后端仍强制检查；
- Source Map 的生产发布策略需避免泄露内部配置和 Secret。

---

## 13. 供应链与 CI/CD

### 13.1 代码与依赖

- 依赖版本锁定；
- PR 执行 SAST、依赖漏洞扫描和 Secret 扫描；
- 禁止忽略 Critical / High 漏洞而无具名豁免；
- 生成或保存 SBOM；
- 上游 DeerFlow 基线使用 tag / commit 固定；
- 许可证和 NOTICE 随上游同步更新。

### 13.2 镜像

- 基础镜像固定 digest；
- 生产镜像不包含编译密钥、测试凭证和本地 `.env`；
- 使用最小镜像并以非 root 运行；
- 构建与部署身份分离；
- 镜像进入生产前扫描并建议签名；
- 生产只允许来自受控 Registry 的制品。

### 13.3 修复 SLA

默认从可利用漏洞确认或供应商修复可用时起：

| 严重度 | 修复 / 缓解时限 |
| --- | --- |
| Critical，已利用或外网可达 | 24 小时内缓解，72 小时内修复或隔离 |
| Critical | 72 小时 |
| High | 7 天 |
| Medium | 30 天 |
| Low | 90 天或进入常规升级 |

无法按时修复必须降低暴露面、记录 Owner、到期日和补偿控制。

---

## 14. 可观测性安全

- 结构化日志必须包含 request / trace / run 关联 ID；
- `org_id` 可用于受控日志检索，但不作为无界公共 Metrics 标签；
- 日志访问按最小权限，生产日志与开发日志隔离；
- 日志、Trace、错误事件设置保留和删除策略；
- 认证失败激增、跨 Org 探测、API Key 异常、策略拒绝、Sandbox 逃逸迹象和审计写失败必须告警；
- 禁止把完整 Prompt / Response 默认写入 APM Span；
- Debug 日志不得在生产长期启用。

---

## 15. 备份与恢复安全

- 备份加密，密钥与备份分离；
- 备份账号只具备所需读写权限；
- 恢复环境访问受控并清理临时数据；
- 恢复演练使用脱敏或受控生产数据；
- 恢复后验证 Org 隔离、权限、审计、ReleaseRef 和 Secret 引用；
- 备份删除遵循保留与 legal hold；
- RPO / RTO 和具体流程见[生产 Runbook](../ops/production-runbook.md)。

---

## 16. 安全事件响应

### 16.1 分级

| 级别 | 示例 | 初始响应目标 |
| --- | --- | --- |
| P1 | 跨 Org 泄露、密钥大规模泄露、Sandbox 逃逸、审计大面积失效 | 15 分钟确认，立即遏制 |
| P2 | 单租户凭证泄露、持续未授权尝试、关键安全控制降级 | 30 分钟确认 |
| P3 | 无直接利用的中风险配置、扫描发现 | 1 个工作日 |
| P4 | 低风险改进项 | 常规排期 |

### 16.2 最小流程

1. 确认并建立事件记录；
2. 遏制：撤销 Key / Session、暂停 Org、禁用 Connector、隔离 Sandbox 或回滚版本；
3. 保全日志、AuditEvent、镜像、Release digest 和相关配置；
4. 确定影响 Org、数据、时间窗和主体；
5. 修复并验证隔离、凭证与审计；
6. 按法规和合同执行通知；
7. 完成复盘、控制改进与回归测试。

禁止为了“调查方便”继续让已确认泄露的凭证有效。

---

## 17. 安全豁免

豁免必须包含：

- 具体条款与无法满足的原因；
- 受影响环境、Org、组件和数据分类；
- 风险等级和最坏影响；
- 补偿控制；
- Owner、批准人和到期日；
- 验证与撤销条件。

规则：

- 跨 Org 隔离、明文 Secret、生产 host bash、未认证 Admin API 不允许常规豁免；
- Critical 豁免需安全负责人和平台负责人共同批准；
- 到期自动失效，不允许无期限豁免；
- 豁免不能只存在于聊天或口头决定中。

---

## 18. 生产准入清单

### 身份与授权

- [ ] OIDC / Session 验签、过期和 CSRF 测试通过
- [ ] API Key 单向存储、scope、过期、轮换和撤销可用
- [ ] 双 Org 隔离套件通过
- [ ] system-admin 跨 Org 操作具备独立权限与审计
- [ ] Membership / 用户 / 关键角色 / ServiceAccount / API Key 撤销后 60 秒内旧 Session、权限和 SSE 失效
- [ ] suspended / deleting Org 无法新建 Run 或发布，deleting Org 普通写入被冻结
- [ ] Compatibility 路由经过统一认证、TenantContext 和 RBAC，不信任客户端 `org_id`
- [ ] 实际监听的 Internal API 非公网且不使用 Header-only 信任

### Secret 与网络

- [ ] 仓库、镜像、日志和数据库无明文 Secret
- [ ] 生产 Secret 来自受控 Secret Store
- [ ] 外部和数据服务连接使用 TLS
- [ ] Sandbox / Tool 出站默认拒绝
- [ ] SSRF、重定向、DNS rebinding 和元数据地址测试通过
- [ ] 登录、Run 创建、发布和 API Key 接口限流生效，429 包含 `Retry-After`

### Sandbox 与扩展

- [ ] `allow_host_bash=false`
- [ ] 非 root、只读根文件系统和资源限制生效
- [ ] Sandbox 不挂载宿主敏感目录和管理凭证
- [ ] MCP / Skill / Agent 制品有来源、版本和 digest
- [ ] prod 只执行已发布 ReleaseRef
- [ ] prod 拒绝 admit / resume / continue `legacy_unpinned` Run

### 审计与供应链

- [ ] 关键写路径 AuditEvent 覆盖率 100% 或有具名豁免
- [ ] 普通应用账号不能修改或删除 AuditEvent
- [ ] 审计热数据保留 90 天，归档作业可运行并满足 365 天总保留策略
- [ ] CI 执行 Secret、依赖、镜像和代码扫描
- [ ] Critical / High 漏洞满足 SLA
- [ ] 备份加密并完成恢复演练

任何阻断项未通过时，不得宣称满足 DeerNexus 生产安全基线。
