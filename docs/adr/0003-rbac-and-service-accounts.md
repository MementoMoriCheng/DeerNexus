# ADR-0003：RBAC、服务账号与权限撤销

- **状态**：Accepted
- **日期**：2026-07-15
- **决策者**：DeerNexus 项目组
- **关联**：[ADR-0002](0002-tenant-workspace-keys.md) · [API 边界](../architecture/api-boundaries.md) · [数据模型](../architecture/data-model.md) · [安全基线](../security/baseline.md) · [测试策略](../engineering/testing-strategy.md)

---

## 1. 背景

DeerFlow 现有 flat permissions 无法表达企业组织内角色、机器身份、撤权和跨租户隔离。DeerNexus MVP 必须回答：

1. 用户和服务账号如何获得权限；
2. Org 角色与 system-admin 如何隔离；
3. API Key scope 与角色权限如何组合；
4. Membership、角色或 Key 撤销多久生效；
5. 跨 Org 与当前 Org 权限不足分别返回什么；
6. OIDC group 如何安全映射。

---

## 2. 决策

MVP 采用 **Org 级 RBAC + ServiceAccount + Scoped API Key**：

- Organization 是授权作用域；
- Workspace 只作资源分组，MVP 不做 Workspace 级 RBAC；
- 权限由 `RoleBinding` 授予 User 或 ServiceAccount；
- API Key 不直接拥有角色，只继承其 ServiceAccount 的有效权限并再与 Key scopes 取交集；
- `system:admin` 是平台级独立身份，不属于 Org 角色；
- 默认拒绝，未知权限字符串和未知角色不自动放行；
- 权限撤销在 60 秒内对后续请求生效；
- Router 只调用统一授权服务，不手写角色判断。

---

## 3. 权限命名

权限使用：

```text
<domain>:<resource>:<action>
```

MVP 权限：

```text
runtime:thread:read
runtime:thread:write
runtime:run:create
runtime:run:read
runtime:run:cancel
runtime:run:resume

admin:org:read
admin:org:manage
admin:iam:read
admin:iam:manage
admin:audit:read
admin:console:read

studio:package:read
studio:package:write
studio:release:promote_dev
studio:release:promote
studio:release:rollback

connector:read
connector:manage

system:org:create
system:org:read_all
system:org:operate_all
```

约束：

- 权限字符串是稳定契约；
- 新增权限必须更新本文、API 边界、角色矩阵和正反向测试；
- 不使用 `*` 作为普通租户角色的持久权限；
- system 权限不允许写入 Org 自定义角色；
- UI 功能名不能直接作为权限字符串。

---

## 4. MVP 角色

### 4.1 `org:admin`

默认权限：

```text
runtime:thread:read
runtime:thread:write
runtime:run:create
runtime:run:read
runtime:run:cancel
runtime:run:resume
admin:org:read
admin:org:manage
admin:iam:read
admin:iam:manage
admin:audit:read
admin:console:read
studio:package:read
studio:package:write
studio:release:promote_dev
studio:release:promote
studio:release:rollback
connector:read
connector:manage
```

### 4.2 `org:developer`

默认权限：

```text
runtime:thread:read
runtime:thread:write
runtime:run:create
runtime:run:read
runtime:run:cancel
runtime:run:resume
studio:package:read
studio:package:write
studio:release:promote_dev
connector:read
```

默认不允许：

- IAM 管理；
- Audit 查询；
- Org Console；
- staging / prod promote 和 rollback；
- Connector 凭证管理。

### 4.3 `org:viewer`

默认权限：

```text
runtime:thread:read
runtime:run:read
studio:package:read
connector:read
```

默认不允许：

- 创建、取消和恢复 Run；
- Admin Console；
- IAM、发布和 Connector 管理；
- Audit 查询。

### 4.4 `system:admin`

- 独立于 Org RoleBinding；
- 可创建 Org；
- 跨 Org 操作必须调用专用 system API / repository；
- 每次跨 Org 写、导出、身份模拟或紧急访问产生 AuditEvent；
- 不允许通过普通 Org API 隐式扩大为所有 Org；
- 必须使用 MFA 能力或 Break-glass 流程；
- 日常 Org 管理优先使用 Org 角色，不长期使用 system-admin。

---

## 5. 角色可定制范围

MVP：

- 交付上述三个内置 Org 角色；
- 角色权限由版本化 seed / migration 创建；
- Org 可以给主体绑定多个内置角色；
- 有效权限取并集；
- 不交付自定义角色 UI；
- 如 API 暴露自定义角色，只允许从已登记 Org 权限中选择，不能包含 system 权限。

角色模板升级：

- 新增权限不自动赋予现有自定义角色；
- 内置角色变更必须有迁移、变更记录和回归测试；
- 权限放宽需要安全与产品共同签收；
- 权限收紧需评估自动化和 ServiceAccount 影响。

---

## 6. 授权计算

```text
effective_permissions =
  active_membership
  ∩ active_principal
  ∩ non_expired_role_bindings
  ∩ union(role.permissions)
  ∩ api_key.scopes_if_present
  ∩ organization_state
  ∩ policy_decision
```

说明：

- User 必须有 `active` Membership；
- ServiceAccount 必须为 `active` 且属于当前 Org；
- RoleBinding 过期或删除后不再生效；
- API Key scope 只能收窄，不能扩大 ServiceAccount 权限；
- Suspended Org 禁止新 Run 和发布；
- Deleting Org 只允许删除流程、受控导出、审计和取消；
- RBAC allow 后仍可能被 Policy deny / require_approval；
- Policy 不能授予主体本来没有的 RBAC 权限。

统一接口：

```text
authorize(
  tenant_context,
  permission,
  resource_ref=None
) -> None | raises ContractError
```

授权服务返回 allow 或抛稳定错误，不把完整角色或策略细节返回客户端。

---

## 7. Membership 状态

| 状态 | 可绑定 TenantContext | HTTP 语义 |
| --- | --- | --- |
| `invited` | 否 | 视为无活动 Org 范围，404 |
| `active` | 是 | 继续 RBAC |
| `suspended` | 否 | 403 `permission_denied` |
| `removed` | 否 | 视为无活动 Org 范围，404 |

规则：

- 接受邀请后才进入 active；
- suspended 保留审计和恢复可能性；
- removed 不形成当前 Org 选择项；
- 状态变更产生 AuditEvent；
- 最后一个 `org:admin` 不得通过普通请求被删除、暂停或解绑；
- 紧急移除最后管理员需 system-admin 专用流程和双人审批记录。

---

## 8. RoleBinding

RoleBinding：

```text
org_id
principal_type: user | service_account
principal_id
role_id
created_by
created_at
expires_at?
```

不变量：

- Principal、Role 和 Binding 属于同一 Org；
- MVP 无 workspace scope；
- 重复 Binding 幂等；
- 过期 Binding 不生效；
- 创建和删除产生 AuditEvent；
- 不能把 `system:admin` 作为普通 RoleBinding；
- 列表与查询强制 Org 过滤。

---

## 9. ServiceAccount

### 9.1 生命周期

```text
active → disabled
disabled → active
active|disabled → deleted
```

- disabled 后新的鉴权返回 `principal_disabled`；
- 删除前撤销全部 API Key；
- ServiceAccount 不具有人类 Session；
- Owner 是管理责任人，不意味着自动拥有该账号权限；
- 每个账号用途、系统、环境和到期评审日期必须可追踪。

### 9.2 Key

- Key 只属于一个 ServiceAccount 和 Org；
- 明文只展示一次；
- 数据库存 prefix + 强单向哈希 / HMAC；
- 校验使用恒定时间比较或成熟密码库；
- 默认有效期 ≤90 天；
- scopes 必须是 ServiceAccount 有效权限子集；
- 轮换重叠期默认 ≤24 小时；
- revoked / expired Key 不可恢复；
- 创建 Key 时 scopes 必填且非空；没有 API Key 的人类 Session 不应用 Key scope 交集；
- 创建、轮换、撤销和异常使用必须审计；
- Key 不进入 URL、日志、Trace、Audit payload 和前端持久化。

### 9.3 限流

至少按：

```text
org_id + service_account_id
org_id + api_key_id
source_ip（认证失败）
```

429 返回 `Retry-After`。限流不替代权限和配额。

---

## 10. OIDC Group 映射

配置模型：

```text
issuer
group_claim
group_value
target_org_id
target_role_id
mode: additive | authoritative
```

MVP 默认 `additive`：

- 仅 allowlist 中的 issuer / group 可映射；
- 未识别 group 不自动创建角色；
- group 不能映射 system 权限；
- 多个 group 的角色权限取并集；
- 映射变更产生审计；
- IdP group 删除不自动删除手工 Binding；
- `authoritative` 模式需单独启用，先 dry-run 并防止删除最后管理员；
- Email domain 不能自动授予 admin。

至少选择一个 IdP 完成 E2E。

---

## 11. 缓存与撤权

允许缓存：

- Membership 状态；
- Role / RoleBinding；
- 计算后的 permission set。

要求：

- 缓存键含 `org_id + principal_type + principal_id`；
- 最大 TTL 60 秒；
- Membership、RoleBinding、ServiceAccount、API Key 和关键角色变更主动失效；
- 主动失效失败时仍不得超过 60 秒；
- 跨 Org 不共用权限缓存项；
- system-admin 权限使用独立缓存 namespace；
- 连接池和 ContextVar 清理纳入测试；
- 高风险操作可以强制读取最新授权状态。
- 已建立 SSE 至少每 60 秒或在发送下一条业务事件前重新确认主体、Membership 和 Key 状态；发现撤权后关闭流，不继续发送业务事件。

撤权 SLO：从 Membership、RoleBinding、ServiceAccount 或 API Key 变更成功提交，到新请求被拒绝或已有 SSE 关闭，P99 ≤60 秒。

---

## 12. 403 / 404 与错误码

| 场景 | 响应 |
| --- | --- |
| 无效 / 过期凭证 | 401 `authentication_invalid` |
| User / ServiceAccount disabled | 403 `principal_disabled` |
| revoked / expired API Key，或已删除 ServiceAccount 的 Key | 401 `authentication_invalid` |
| invited / removed Membership | 404；响应不得使用 `permission_denied` 暴露 Org 范围 |
| suspended Membership | 403 `permission_denied` |
| 跨 Org 资源 | 404 |
| 当前 Org 内资源存在但权限不足 | 403 `permission_denied` |
| Suspended Org 新 Run / 发布 | 403 `org_suspended` |
| Deleting Org 普通写 / 新 Run / 发布 | 403 `org_deleting` |

错误响应不包含角色详情、另一个 Org 的资源存在性或策略全文。

鉴权顺序先验证 API Key 存在、哈希、过期和撤销；Key 无效返回 401。Key 有效后再检查 ServiceAccount，disabled 返回 403。ServiceAccount 删除必须与全部 Key 撤销在同一受控事务完成。

---

## 13. 审计

必须审计：

- Membership invite / activate / suspend / remove；
- RoleBinding create / delete / expire；
- 内置角色权限版本变更；
- ServiceAccount create / enable / disable / delete；
- API Key create / rotate / revoke；
- OIDC group mapping create / update / delete；
- system-admin 跨 Org 写、导出和紧急访问；
- 最后管理员保护触发；
- repeated authentication failure / suspicious key use（安全事件）。

事件命名由[ADR-0005](0005-audit-event.md)统一。高风险 IAM 事务必须与 Audit outbox 同事务或在 Audit 不可用时失败回滚。

---

## 14. 迁移

1. 创建内置角色和权限；
2. 创建默认 Org；
3. 为存量用户创建 active Membership；
4. 按现有管理员配置创建首个 org:admin Binding；
5. 禁止把所有已认证用户自动设为 admin；
6. 保留旧 flat permission 的只读兼容；
7. 双写或比较旧 / 新授权结果；
8. 运行 RBAC 正反向和双 Org 测试；
9. 切换新授权为权威；
10. 删除 `_ALL_PERMISSIONS` 等旧放行路径。✅（PR-033：`authz.py` 整体删除，
    `require_permission`/`require_auth`/`AuthContext`/`_ALL_PERMISSIONS`
    全部移除；所有 runtime + admin router 走 `@require_rbac` + AuthorizeService）

迁移期任何差异默认拒绝高风险写操作。

---

## 15. 验收

- [ ] 三个内置 Org 角色权限与本文一致
- [ ] Viewer 无法创建 Run 或打开 Admin Console
- [ ] Developer 无法 IAM、Audit 或 prod promote
- [ ] API Key scope 只能收窄 ServiceAccount 权限
- [ ] invited / suspended / removed Membership 语义通过
- [ ] Membership、角色、主体和 Key 撤销 P99 ≤60 秒
- [ ] 最后 org:admin 保护通过
- [ ] OIDC allowlist group 映射通过
- [ ] system-admin 跨 Org 操作走专用接口并审计
- [x] Router 无手写角色判断（PR-033：9 个 `require_admin_user` call site 全切到
      `@require_rbac`，无内联 `system_role == "admin"` 判断残留）
- [x] 旧 flat permission 放行路径已删除或被 Feature Flag 完全关闭（PR-033：
      `authz.py` 整体删除，`_ALL_PERMISSIONS` flat-grant 不再存在）
- [ ] RBAC 正反向矩阵和双 Org 测试进入 CI

---

## 16. 后果

### 正向

- 用户和机器身份使用同一授权模型；
- API Key 不再是隐式全权限；
- 撤权、跨 Org 和 system-admin 语义明确；
- Workspace 级 RBAC 可后续演进而不阻塞 MVP。

### 代价

- 需要统一授权服务和权限缓存失效；
- 需要迁移存量 flat permissions；
- 发布、Connector、Console 等功能必须声明细粒度 permission。

---

## 17. 非目标

- Workspace 级 RBAC；
- ABAC / ReBAC 通用策略语言；
- 自定义角色 UI；
- 跨 Org 角色继承；
- 自动 JIT 管理员；
- 把 Policy Engine 当作 RBAC 替代品。
