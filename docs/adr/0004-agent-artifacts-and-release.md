# ADR-0004：Agent 不可变制品、发布通道与回滚

- **状态**：Accepted
- **日期**：2026-07-15
- **决策者**：DeerNexus 项目组
- **关联**：[运行时契约](../architecture/runtime-contracts.md) · [数据模型](../architecture/data-model.md) · [API 边界](../architecture/api-boundaries.md) · [安全基线](../security/baseline.md) · [90 天 MVP](../roadmap/90-day-mvp.md)

---

## 1. 背景

DeerFlow 的文件态 Agent / Skill 适合开发，但生产运行若读取“磁盘最新版本”，无法证明一次 Run 使用了什么配置，也无法可靠回滚。DeerNexus MVP 需要：

1. 不可变 AgentVersion；
2. dev / staging / prod 通道；
3. Run 创建时固定 ReleaseRef；
4. 并发安全的晋升与回滚；
5. 文件态资产向 Catalog / 制品库导入；
6. 已撤销、缺失或存量未固定版本的处理。

---

## 2. 决策

- `AgentPackage` 表示稳定逻辑身份；
- `AgentVersion` 表示不可变内容版本；
- `ReleaseChannel` 是指向某个 AgentVersion 的可变指针；
- `ReleaseRef` 是 Run 创建时解析并持久化的不可变执行引用；
- `digest` 是执行身份，SemVer 是人类可读版本；
- prod 只允许 `published` Version；
- 文件系统只作开发态或导入源，不是 prod 权威源；
- 通道变化只影响之后新建的 Run；
- 发布和回滚通过 CAS、幂等键、ReleaseEvent 和 AuditEvent 完成。

---

## 3. 领域模型

### 3.1 AgentPackage

```text
AgentPackage(
  id,
  org_id,
  workspace_id?,
  name,
  display_name,
  description,
  status
)
```

不变量：

- `name` 在 Org 内唯一；
- Package 不跨 Org 移动；
- 已有 Version / Release 后不能硬删除；
- Workspace 只作分组，不改变 Org 权限。

### 3.2 AgentVersion

```text
AgentVersion(
  id,
  org_id,
  package_id,
  version,
  digest,
  status,
  manifest,
  content_inline | object_key,
  size_bytes,
  created_by,
  created_at,
  published_at?,
  revoked_at?
)
```

不变量：

- `version` 遵循 SemVer 2.0；
- `digest` 初始使用 `sha256:<hex>`；
- digest 对存储的精确制品字节计算；
- 同一 Package 的 version 唯一；
- 已进入 `published` 的内容、manifest、version、digest 不可修改；
- 内容变化必须创建新 Version；
- `content_inline` 和 `object_key` 必须且只能有一个；
- Version、Package、对象存储前缀必须属于同一 Org。

### 3.3 Manifest

MVP Manifest 至少包含：

```text
schema_version
agent_entry
soul_or_prompt_ref
model_requirements
skills[]
tools[]
mcp_servers[]
dependencies[]
network_requirements[]
secret_requirements[]
runtime_limits
source_metadata
```

Manifest：

- 不包含 Secret 明文；
- 引用 Skill / MCP 时记录稳定 ID、版本或 digest；
- 依赖和网络要求必须显式；
- 通过 Schema 校验；
- 作为制品内容的一部分参与 digest 或由摘要安全绑定。

---

## 4. 状态机

```text
draft → reviewed → published → revoked
draft | reviewed → archived
```

### 4.1 `draft`

- 可编辑；
- 可在受控 dev 环境运行；
- 不能进入 prod；
- 修改后重新计算 digest。

### 4.2 `reviewed`

- 已通过 MVP 静态检查和必要人工评审；
- staging 可以使用；
- 如内容变化，回到 draft 并产生新 digest；
- 企业 Approval 是独立证据，不增加 `approved` Version 状态。

### 4.3 `published`

- 内容不可变；
- 可以进入 prod；
- 必须有 published_at、发布主体和审计证据；
- 发现问题只能撤销或发布新版本，不能原地修复。

### 4.4 `revoked`

- 不能创建新 Run；
- ReleaseChannel 不能新指向该 Version；
- 历史 Run 和审计仍保留引用；
- 默认不自动终止已运行 Run，避免未知副作用重放；
- 紧急安全撤销由 Policy / 运维流程显式取消受影响 Run；
- 撤销原因和影响范围必须审计。

### 4.5 `archived`

- 未发布草稿的归档状态；
- 不可用于新 Run；
- 可以按保留策略清理内容；
- 不等同 revoked。

---

## 5. 通道策略

| Channel | 可指向 Version 状态 | 用途 |
| --- | --- | --- |
| `dev` | draft / reviewed / published | 开发验证 |
| `staging` | reviewed / published | 预发、E2E、性能与安全检查 |
| `prod` | published | 生产 |

规则：

- 每个 Org / Workspace / Package / Channel 只有一个当前指针；
- PostgreSQL 15+ 使用 `UNIQUE NULLS NOT DISTINCT` 处理空 Workspace；
- 更新使用 `row_version` / If-Match；
- 通道不能跨 Org 指向 Version；
- prod 解析失败不回退 dev、文件系统或“最近版本”；
- staging 不自动晋升 prod；
- 环境与 Channel 映射写入部署配置，不由客户端随意覆盖。

---

## 6. ReleaseRef

字段以运行时契约为准：

```text
ReleaseRef(
  schema_version,
  org_id,
  workspace_id?,
  package_id,
  agent_name,
  version,
  digest,
  channel,
  resolved_at
)
```

创建 Run 的原子流程：

1. 绑定 TenantContext；
2. 验证 `runtime:run:create`；
3. 验证 Org 状态；
4. 解析目标 Package 和 Channel；
5. 校验 Version 状态、Org、digest 和对象存在；
6. 执行 Run admission Policy；
7. 在同一一致性边界内把完整 ReleaseRef、policy_version 和 Run 写入 PostgreSQL；
8. 生成 RunEnvelope；
9. 执行阶段只消费持久化 ReleaseRef，不重新读取 Channel。

Run 不因以下变化而漂移：

- Channel 晋升或回滚；
- 文件系统内容变化；
- Catalog 同步；
- Package display_name 变化；
- Runtime 进程重启。

---

## 7. 晋升

请求：

```text
channel_id
target_version_id
expected_channel_version
idempotency_key
reason
```

同事务执行：

1. dev 通道校验 `studio:release:promote_dev` 或 `studio:release:promote`；staging / prod 通道必须校验 `studio:release:promote`；
2. 校验 Org、Workspace、Package；
3. 校验目标 Version 状态；
4. 校验 digest 对象存在；
5. 运行通道门禁；
6. CAS 更新 ReleaseChannel；
7. 写 ReleaseEvent；
8. 写 Audit outbox；
9. 提交。

并发：

- 同 expected version 只有一个更新成功；
- 其他请求返回 409 `release_conflict`；
- 相同 Idempotency-Key + 相同请求返回原结果；
- 相同 Key + 不同请求返回 `idempotency_conflict`。

---

## 8. 回滚

Rollback 是把 Channel 指向一个历史有效 Version，不修改 Version 内容。

要求：

- 权限 `studio:release:rollback`；
- 目标 Version 属于同 Package / Org；
- 目标 Version 为允许状态，prod 必须 published 且未 revoked；
- 使用 If-Match / row_version；
- reason 必填；
- 写 ReleaseEvent(action=rollback)；
- 写 `release.agent.rolled_back`；
- 只影响回滚后新 Run；
- 不删除被回滚版本；
- 回滚失败不尝试读取文件系统兜底。

紧急回滚可以缩短普通发布流程，但不能跳过身份、Org、digest、CAS 和审计。

---

## 9. 门禁

### 9.1 Reviewed 门禁

- Manifest Schema；
- 路径穿越和符号链接；
- 危险二进制 / 脚本；
- 依赖锁定；
- Tool / MCP 风险声明；
- 网络与 Secret 需求；
- 制品大小；
- digest 校验；
- 基本加载测试。

### 9.2 Prod 门禁

- Version `published`；
- 静态和安全检查通过；
- Package / Version / Channel 同 Org；
- digest 对象存在且匹配；
- 没有阻断级撤销或漏洞；
- 权限与 Policy allow；
- ReleaseEvent / Audit outbox 可写；
- E2E v1 → v2 → rollback 最近通过；
- 生产配置不允许 legacy unpinned。

MVP 不强制完整签名供应链，但必须保留 digest、来源和扫描证据；公共市场前再引入签名、证明和供应商信任链。

---

## 10. 文件态导入

流程：

```text
discover file asset
→ validate path / schema
→ materialize exact artifact
→ calculate digest
→ create Package if needed
→ create draft Version
→ write Catalog entry(source=file_import)
→ review / publish
```

规则：

- 导入是单向快照，不持续双向同步；
- 文件变化不自动修改已导入 Version；
- 重复 digest 导入幂等；
- source_metadata 记录来源路径、上游 commit 和导入时间，但不把路径作为执行身份；
- Secret 从文件中剥离为 `secret_ref`；
- prod 只读取制品存储；
- Catalog 是发现索引，不是执行权威。

---

## 11. 存储

### 11.1 小制品

可存 PostgreSQL inline，阈值由生产配置定义并进入压测。

### 11.2 大制品

对象键：

```text
org/{org_id}/workspace/{workspace_id-or-_default}/agent-version/{version_id}/artifact
```

要求：

- 私有 Bucket；
- 静态加密；
- 数据库保存 object_key、digest、size；
- 上传完成后再使 Version 可用；
- 下载前授权；
- 签名 URL 短期有效；
- Object 与数据库定期对账；
- 缺失或 digest 不匹配时拒绝执行并告警。

### 11.3 删除

- published / revoked Version 默认不物理删除；
- archived 草稿按保留策略清理；
- Object 删除先验证无 Release / Run / Audit 引用；
- 法律保留优先；
- 清理 Job 按 Org 审计。

---

## 12. Legacy Run

存量没有 ReleaseRef 的 Run 标记：

```text
legacy_unpinned=true
```

生产策略：

- 可以读取、导出、取消和归档；
- 不能新 admission；
- 不能 resume；
- 不能继续执行；
- 返回 409 `release_unpinned`；
- 迁移能确定精确版本和 digest 时，可以一次性补写并记录迁移证据；
- 不能通过“当前磁盘版本”猜测历史 digest；
- prod 门禁启用前，可执行范围内 unpinned 计数必须为 0。

---

## 13. 撤销与安全事件

Version 撤销必须记录：

```text
reason_code
reason
actor
occurred_at
affected_channels
affected_non_terminal_runs
security_incident_id?
```

流程：

1. 标记 Version revoked；
2. 阻止 Channel 新指向；
3. 找出当前指向该 Version 的 Channel；
4. 回滚或置空受影响 Channel；
5. 找出非终态 Run；
6. 按风险决定继续、取消或人工处理；
7. 记录 AuditEvent；
8. 通知受影响 Owner。

紧急流程仍不得修改历史 digest 或删除证据。

---

## 14. 权限与审计

| 动作 | Permission | Audit |
| --- | --- | --- |
| 创建 Package / draft | `studio:package:write` | 必须 |
| 上传 Version | `studio:package:write` | 必须 |
| Review | `studio:package:write` 或后续专用权限 | 必须 |
| Publish Version | MVP 由 `studio:release:promote` 控制 | `catalog.agent_version.published` |
| Promote dev | `studio:release:promote_dev` 或 `studio:release:promote` | `release.agent.published` |
| Promote staging / prod | `studio:release:promote` | `release.agent.published` |
| Rollback | `studio:release:rollback` | `release.agent.rolled_back` |
| Revoke | `studio:release:promote` 或后续专用权限 | 必须 |
| 删除草稿 | `studio:package:write` | 必须 |

`catalog.agent_version.published` 表示 Version 状态进入 published；`release.agent.published` 表示 Channel CAS 成功，两者不能互换。若产品把 Publish 与 Promote 合并为一次请求，必须同时写两个事件。事件命名最终由[ADR-0005](0005-audit-event.md)统一。Prod 操作的审计不可用时 fail-closed。

---

## 15. 测试

- [ ] draft 可变、published 不可变
- [ ] digest 与内容匹配
- [ ] dev / staging / prod 状态门禁正确
- [ ] Developer 可 promote dev，但不能 promote staging / prod 或 rollback prod
- [ ] OrgA Channel 不能引用 OrgB Version
- [ ] 并发 promote 只有一个成功
- [ ] Idempotency-Key 重放安全
- [ ] v1 → v2 → rollback 新 Run digest 正确
- [ ] 在途 Run 不随 Channel 变化
- [ ] revoked 不能创建新 Run
- [ ] 缺失 / 篡改对象拒绝执行
- [ ] 文件变化不影响已导入 Version
- [ ] prod 不读取文件系统草稿
- [ ] legacy_unpinned 被 409 拒绝
- [ ] ReleaseEvent 与 AuditEvent 同事务 / outbox 一致
- [ ] 对象存储与数据库引用对账

---

## 16. 后果

### 正向

- Run 可重现；
- 发布和回滚原子、可审计；
- 文件态资产可渐进迁移；
- Catalog、Channel 和执行权威职责清楚。

### 代价

- 需要制品存储、摘要和对账；
- dev / staging / prod 行为有显式差异；
- 存量 Run 不能无证据恢复执行。

---

## 17. 非目标

- 公共 Registry 市场；
- 完整签名证明 / SLSA 供应链；
- 自动 Canary / 多环境编排平台；
- 评测平台完整产品；
- 动态读取文件系统最新配置；
- 修改已发布制品。
