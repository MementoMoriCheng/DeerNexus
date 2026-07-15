# DeerNexus 数据治理与合规基线

> 状态：MVP 数据治理规范 v0.1  
> 适用范围：平台元数据、身份、Run / Thread、Agent 制品、Artifact、Memory、Connector、Audit、Usage、日志、Trace、备份与导出  
> 关联：[数据模型](../architecture/data-model.md) · [ADR-0002](../adr/0002-tenant-workspace-keys.md) · [ADR-0005](../adr/0005-audit-event.md) · [安全基线](../security/baseline.md) · [生产 Runbook](../ops/production-runbook.md) · [威胁模型](../security/threat-model.md)

本文定义 DeerNexus MVP 的数据分类、用途、归属、访问、保留、删除、导出、驻留和合规证据要求。本文是工程控制基线，不构成法律意见，也不表示平台已通过 SOC 2、ISO 27001、等保或其他认证。

---

## 1. 原则

1. **目的限定**：只收集和使用提供 Agent 平台、治理、安全和运营所需的数据；
2. **数据最小化**：不因“以后可能有用”默认保存完整 Prompt、Response、Tool 内容或 Secret；
3. **租户隔离**：Organization 是数据访问、导出和默认保留策略边界；
4. **分类从严**：不确定的数据按更高等级处理；
5. **可追踪**：关键数据集有 Owner、来源、用途、存储、保留和删除方式；
6. **删除可证明**：删除请求有状态、范围、结果和例外原因；
7. **备份不逃逸治理**：备份中的数据按恢复隔离、过期和延迟删除规则管理；
8. **Secret 单独管理**：Secret Store 生命周期不由普通业务表代替；
9. **审计不可混同业务内容**：AuditEvent 记录事实，不复制完整敏感正文；
10. **不虚假承诺合规**：控制映射只表示工程证据，不表示认证结论。

---

## 2. 角色与责任

| 角色 | 责任 |
| --- | --- |
| Data Owner | 决定数据用途、分类、访问和保留；通常为对应产品 / 业务 Owner |
| Data Steward | 维护数据目录、Schema、质量和血缘 |
| System Owner | 实现访问、加密、备份、删除和监控 |
| Security | 安全分类、Threat Model、事件响应和例外评审 |
| Privacy / Legal | 适用法规、合同、跨境、DSR 和 legal hold 决策 |
| SRE / DBA | 存储、备份、恢复、保留 Job 和访问控制 |
| Org Admin | 管理本 Org 数据策略、成员、导出和删除请求 |
| System Admin | 受控平台运维；不能替代 Org Data Owner 作普通业务决定 |

代码 Fork 后，每个数据集必须在数据目录登记具体 Owner 和升级渠道。

---

## 3. 数据分类

| 等级 | 定义 | 示例 | 最低控制 |
| --- | --- | --- | --- |
| Public | 可公开，泄露影响有限 | 公开文档、公开模型名称 | 完整性、来源 |
| Internal | 平台内部，非客户敏感正文 | Feature、聚合指标、非敏感配置 | 认证、加密、受控日志 |
| Confidential | 客户或用户业务数据 | Prompt、Response、Thread、Run、Artifact、Agent 配置、Usage | Org 隔离、最小权限、加密、受控导出 |
| Restricted | 泄露可导致直接接管、重大隐私或安全影响 | Secret、Token、API Key、连接凭证、敏感个人数据、密钥 | Secret Store、严格访问、短期、强审计、禁止普通日志 |

### 3.1 分类继承

- 包含 Restricted 字段的数据对象整体按 Restricted 处理，除非字段已安全分离；
- Artifact 分类至少继承其来源 Thread / Tool 输入；
- 模型响应不因由模型生成而自动成为 Public；
- Agent Manifest 为 Confidential；其中 Secret 需求只能是引用，不能含 Secret；
- 日志 / Trace 一旦包含 Confidential 正文，必须视为安全缺陷并执行清理；
- 摘要本身通常为 Internal，但如可用于关联敏感对象，按 Confidential 访问。

---

## 4. 数据目录

每个数据集登记：

```text
dataset_id
name
owner
steward
purpose
legal_or_contract_basis
classification
org_scoped
workspace_scoped
subjects
sources
destinations
storage_locations
schema_version
encryption
access_roles
retention
deletion_method
backup_behavior
residency
external_processors
audit_events
last_reviewed
```

MVP 最低目录：

- Organization / Workspace；
- User / ExternalIdentity / Membership / RoleBinding；
- ServiceAccount / API Key metadata；
- Thread / Run / Checkpoint / Memory；
- Artifact / Workspace Snapshot；
- AgentPackage / AgentVersion / Release；
- Skill / MCP / Connector metadata；
- Secret references；
- AuditEvent / ReleaseEvent；
- UsageRecord；
- 日志 / Metrics / Trace；
- 数据库、对象存储和审计归档备份；
- 导出 Job 与临时导出文件。

---

## 5. 数据用途与限制

### 5.1 平台运行

允许：

- 执行用户明确请求的 Agent / Tool；
- 保存 Run 状态、Checkpoint 和输出；
- 执行授权、Policy、审计和安全控制；
- 计算用量、性能和可靠性指标；
- 生产故障诊断和恢复。

禁止默认：

- 用客户 Prompt / Response 训练通用模型；
- 把一个 Org 的内容用于另一个 Org；
- 为调试无限期保留完整内容；
- 将 Restricted 数据复制到工单、聊天或非生产环境；
- 使用客户数据进行未声明的产品实验。

### 5.2 外部模型与 Connector

发送前必须：

1. 确认供应商、区域和数据处理条款；
2. 按 Tool / Agent 目的选择最小上下文；
3. 移除不必要的 Secret、身份和其他 Org 内容；
4. 执行 Policy 和数据分类检查；
5. 记录 provider、model、region（可得时）、Run 和 Usage；
6. 明确供应商保留 / 训练设置；
7. 高敏数据需要租户配置或额外批准。

Connector 只获得完成动作所需的字段和短期凭证。

---

## 6. 数据访问

### 6.1 租户访问

- Org 用户只访问当前 TenantContext 范围；
- Workspace 仅作资源分组，MVP 授权仍为 Org 级；
- 跨 Org 资源返回 404；
- 当前 Org 内权限不足返回 403；
- API Key 固定单 Org；
- system-admin 使用专用接口、reason 和强审计。

### 6.2 内部访问

生产数据访问遵循：

- 最小权限；
- 具名身份，不共享账号；
- MFA 或工作负载身份；
- 时间和环境范围；
- 敏感查询与导出审计；
- 定期访问复核；
- 紧急访问有到期和复盘。

开发人员默认不直接访问生产客户正文。需要诊断时优先使用关联 ID、聚合指标和脱敏数据。

### 6.3 数据库角色

至少分离：

- Application Runtime；
- Control Plane；
- Audit Writer / Reader；
- Migration；
- Backup；
- Archive / Retention Job；
- Break-glass DBA。

普通应用角色无 Audit UPDATE / DELETE 和 DDL 权限。

---

## 7. 默认保留

以下是平台 MVP 默认值。合同、法规、legal hold 或 Org 更严格策略从严；缩短保留必须验证业务、恢复和审计影响。

| 数据集 | 热保留 / 活跃期 | 总保留 / 删除条件 |
| --- | --- | --- |
| Organization / Membership / RoleBinding | Org 生命周期内 | 删除后保留最小审计元数据；业务行按删除流程 |
| User / ExternalIdentity | 活跃关系期间 | 无活动 Org 后按账户删除与法定义务处理 |
| API Key 明文 | 不保存 | 创建响应后立即不可恢复 |
| API Key hash / metadata | Key 生命周期内 | 撤销后至少 90 天安全元数据，之后按策略删除 |
| Thread / Prompt / Response | 默认 90 天在线 | Org 可配置 30–365 天；legal hold 例外 |
| Run / Checkpoint | 默认 90 天在线 | terminal 后按 Thread / 合同策略；执行证据摘要可更久 |
| Memory | 直到用户 / Org 删除或 365 天无使用 | 不等同企业知识库；支持显式清除 |
| Artifact | 默认 90 天 | 已发布制品、审计证据或 legal hold 引用除外 |
| AgentPackage | Package 活跃期 | 无 Version / Release / Run 引用后可归档 |
| published / revoked AgentVersion | 引用期间 | 默认至少 365 天且无 Run / Audit / legal hold 引用后才评估清理 |
| ReleaseEvent | ≥365 天 | 与 Audit / 合同从严 |
| AuditEvent | PostgreSQL 热数据 ≥90 天 | 对象归档总保留 ≥365 天 |
| UsageRecord | ≥365 天 | 合同、计费争议和财务要求从严 |
| 应用日志 | 默认 30 天 | 安全事件保全副本除外 |
| Trace | 7–14 天 | 安全事件保全副本除外 |
| Metrics | 默认 90 天或平台能力 | 长期趋势使用聚合 |
| 临时导出 | 默认 ≤24 小时 | 下载到期后自动删除 |
| Backups | 日备至少 7 个恢复点，建议 30 天 | 依 Runbook 与 legal hold |

### 7.1 配置要求

- 保留策略必须版本化；
- Job 按数据集和 Org 记录水位、删除数量、失败和耗时；
- 删除不能破坏仍被 Release、Run、Audit 或 legal hold 引用的数据；
- 归档成功和摘要验证是删除 Audit 热分区的前置条件；
- 改变默认值必须评估存储成本、客户合同、恢复和 DSR。

---

## 8. 删除

### 8.1 删除类型

| 类型 | 语义 |
| --- | --- |
| Soft Delete | 普通查询不可见，保留恢复和依赖检查 |
| Logical Erasure | 内容移除或不可访问，保留最小非内容元数据 |
| Cryptographic Erasure | 删除独立加密密钥使数据不可恢复 |
| Physical Delete | 从在线存储和到期备份 / 归档中移除 |
| Legal Hold | 暂停普通删除，直到授权解除 |

### 8.2 Org 删除

```text
request
→ verify authority / re-authentication
→ inventory
→ legal hold / contract check
→ freeze writes
→ optional export
→ revoke identities and secrets
→ logical deletion
→ async physical cleanup
→ backup expiry
→ completion report
```

要求：

- 进入 `deleting` 后拒绝新 Run、发布和普通写入；
- 取消或安全终止非终态 Run；
- 删除 Connector / API Key / Session；
- 不因 Org 删除提前删除未到期 Audit；
- 已发布制品和历史 Run 引用按保留策略处理；
- 删除 Job 可重入且按 Org；
- 完成报告列出已删、延迟、保留和法律例外。

### 8.3 用户删除

- 先确认其所有 Org Membership；
- 移除 Session、Identity link 和个人 Memory；
- 业务内容按 Org 数据 Owner 和合同策略处理，不自动删除共享 Thread；
- AuditEvent 中稳定 actor ID 可保留，展示字段可脱敏；
- 最后管理员不能通过普通删除路径移除；
- DSR 请求记录身份验证、范围、决定、结果和时间。

### 8.4 Secret 删除

- 先撤销并停止消费者；
- 删除 Secret Store 版本按供应商能力执行；
- 普通数据库只保留无敏感值的 `secret_ref` 状态和审计；
- Secret 不进入导出和备份恢复日志。

---

## 9. 备份中的删除

备份通常不可原地修改。MVP 采用：

1. 在线系统在承诺时间内完成逻辑 / 物理删除；
2. 备份按固定保留自动过期，不为普通删除重写完整备份；
3. 从旧备份恢复时，恢复环境保持隔离；
4. 恢复后、开放流量前重放 deletion ledger / tombstone；
5. 验证已删除主体和 Org 不重新可见；
6. legal hold 覆盖普通备份过期；
7. 记录备份中延迟删除的最长窗口并对合同透明。

Deletion ledger 至少包含：

```text
request_id
scope_type
scope_id
requested_at
effective_at
reason_code
legal_hold_state
completed_stores[]
pending_stores[]
expires_from_backups_at
```

Deletion ledger 本身不保存被删正文。

---

## 10. Legal Hold

Legal hold：

- 由 Legal / 合规授权创建和解除；
- 指定 Org、主体、资源、时间范围和原因；
- 覆盖普通保留与删除；
- 不扩大普通用户读取权限；
- 创建、修改、解除必须审计；
- Retention Job 每次执行前检查；
- 导出和调查使用受控副本；
- 解除后恢复普通策略并记录删除计划。

禁止用无限期“暂不删除”代替正式 legal hold。

---

## 11. 数据导出

### 11.1 Org 导出

- 仅具备对应权限的 Org Admin；
- 大导出使用异步 Job；
- 导出范围、时间、分类和格式明确；
- 文件加密；
- 短期签名 URL，默认不超过 15 分钟；
- 临时导出默认 24 小时内删除；
- 下载时重新鉴权；
- 导出创建、完成、下载和删除审计；
- 不包含 Secret、Key Hash、内部策略全文和其他 Org 数据。

### 11.2 Audit 导出

遵循 ADR-0005：

- 包含 Schema version、Org、时间范围、记录数和 digest；
- system-admin 跨 Org 导出使用专用接口和 reason；
- 归档对象与临时用户导出分离；
- 导出不修改在线 Audit。

### 11.3 格式

建议：

- JSONL：事件和结构化运行数据；
- CSV：受控运营聚合；
- Manifest：文件列表、Schema、摘要、时间和生成主体；
- 二进制 Artifact 保持原媒体类型并有独立摘要。

---

## 12. 数据驻留与跨境

MVP：

- 每个部署声明主数据区域；
- PostgreSQL、对象存储、备份、日志、Trace、Audit 归档区域进入发布清单；
- 不自动跨区域复制客户内容；
- 外部模型、MCP、Connector 的处理区域和供应商进入数据目录；
- 需要跨境时由合同 / Legal 决定，并有数据最小化与供应商控制；
- system-admin 不因跨区域运维自动获得业务正文访问；
- 跨区域多活不是 MVP。

数据驻留是“实际存储和处理位置”，不能只看控制面 Region 标签。

---

## 13. 非生产数据

- 默认使用合成数据；
- 生产数据进入非生产需 Data Owner + Security 批准；
- 先脱敏、最小化，并设置到期；
- 非生产环境不得拥有生产 Secret；
- 脱敏必须覆盖 Prompt、Response、Artifact、PII、Token、URL query 和内部标识；
- 测试日志和报告不得重新暴露数据；
- 清理失败告警；
- 不把生产备份直接恢复到开发人员共享环境。

---

## 14. 数据质量与完整性

关键质量规则：

- 租户资源 `org_id` 非空；
- Workspace 与 Org 一致；
- Run、Thread、Checkpoint 归属一致；
- Run 固定 Release digest；
- Audit event id 唯一；
- Usage attempt 和幂等键防重复；
- Agent digest 与对象内容一致；
- ReleaseChannel 只指向合法 Version；
- Secret 引用可解析但值不落普通数据库；
- 时间使用 UTC；
- 删除后无普通查询旁路；
- 导出记录数与 Manifest 一致。

质量失败按影响分级。跨 Org、Release digest、Audit 和 Secret 完整性错误为 P1。

---

## 15. 数据血缘

最低血缘：

```text
Client / Channel Event
→ TenantContext
→ Thread / Run
→ ReleaseRef / Policy Version
→ Model / Tool / MCP / Sandbox
→ Artifact / Checkpoint
→ Usage / Audit / Observability
```

每个 Run 可以回答：

- 谁、在哪个 Org 发起；
- 使用哪个 Agent digest；
- 调用了哪些受控外部系统；
- 产生哪些 Artifact；
- 用量归因到哪里；
- 哪些安全和发布事件与其关联。

血缘使用 ID、摘要和受控元数据，不复制完整敏感正文。

---

## 16. 数据主体与租户请求

MVP 流程：

1. 接收请求；
2. 验证请求者身份和权限；
3. 确定适用 Org、主体、数据集和法律范围；
4. 发现和汇总数据；
5. 应用他人权利、商业秘密、Audit 和 legal hold 例外；
6. 执行访问、更正、导出或删除；
7. 验证在线、对象、缓存和未来备份恢复行为；
8. 在约定期限回复；
9. 保存最小请求处理证据。

平台必须支持工程执行，但具体法定时限由适用地区和合同决定，不能在本文统一假定。

---

## 17. 安全与隐私事件

疑似泄露时：

- 识别数据分类、Org、主体、时间和外部接收方；
- 冻结相关导出、Key、Connector、Agent 或 Sandbox；
- 保全 Audit、日志、Release digest 和配置；
- 不把调查副本传播到普通协作工具；
- 由 Legal / Privacy 判断通知义务；
- 修复后验证隔离、删除、凭证和保留；
- 更新 Threat Model、数据目录和回归测试。

---

## 18. 合规控制映射

下表仅为工程证据索引，不代表认证：

| 控制主题 | DeerNexus 证据 |
| --- | --- |
| 访问控制 | ADR-0002、ADR-0003、API Boundaries、IAM 测试 |
| 变更管理 | CI/CD、ADR、PR 证据、发布记录 |
| 安全开发 | Threat Model、SAST / SCA、测试策略、Security Review |
| 日志与监控 | Observability / SLO、ADR-0005、告警演练 |
| 数据保护 | 安全基线、Secret Store、加密、分类 |
| 保留与删除 | 本文、ADR-0005、Retention / Deletion Job |
| 备份与恢复 | Production Runbook、恢复演练 |
| 供应链 | CI/CD、SBOM、上游同步 |
| 事件响应 | 安全基线 §16、Incident 记录 |
| 第三方风险 | 数据目录、外部处理方记录、合同评审 |

实际 SOC 2 / ISO 27001 / 等保 / GDPR 映射由合规 Owner 基于适用范围建立。

---

## 19. 例外

数据治理例外必须包含：

- 数据集和字段；
- Org / 环境；
- 目的与法律 / 合同依据；
- 分类与影响；
- 保留或访问差异；
- 补偿控制；
- Owner、批准人、到期日；
- 删除和退出计划。

不允许常规例外：

- 跨 Org 共享未授权内容；
- Secret 进入普通日志或导出；
- 绕过 legal hold；
- 无期限保留且无目的；
- 把生产客户正文用于未声明训练。

---

## 20. 验收

- [ ] MVP 数据集全部进入数据目录
- [ ] 每个数据集有 Owner、用途、分类、存储、保留和删除方式
- [ ] Org 级保留配置不弱于适用合同 / 法规
- [ ] Retention Job 可观测、可重入并按 Org
- [ ] Org / User 删除流程经过 staging 演练
- [ ] 从旧备份恢复后 deletion ledger 可重放
- [ ] Audit 90 天热、365 天总保留与 ADR-0005 一致
- [ ] 临时导出按时删除且下载重新鉴权
- [ ] 非生产默认不使用生产客户正文
- [ ] 外部模型 / MCP / Connector 进入处理方目录
- [ ] legal hold 阻断普通删除并有审计
- [ ] 合规映射明确标注“证据索引，不代表认证”
