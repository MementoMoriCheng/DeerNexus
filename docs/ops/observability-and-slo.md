# DeerNexus 可观测性与 SLO

> 状态：MVP 运维规范  
> 关联：[生产 Runbook](production-runbook.md) · [测试策略](../engineering/testing-strategy.md) · [安全基线](../security/baseline.md) · [运行时契约](../architecture/runtime-contracts.md) · [ADR-0005](../adr/0005-audit-event.md)

本文定义 DeerNexus 日志、指标、Trace、Dashboard、告警、SLI / SLO 和错误预算。实现优先采用 OpenTelemetry 或等价开放标准，不绑定特定观测厂商。

---

## 1. 目标

1. 一次 Run 可从 API 请求追踪到 Agent、模型、Tool、MCP、Sandbox 和 Audit；
2. 租户隔离问题可按 request / trace / run 定位；
3. SLO 有明确分子、分母、窗口和排除项；
4. 告警指向可执行 Runbook，不以“有人看 Dashboard”代替；
5. 观测数据不泄露 Secret、Prompt 和跨 Org 内容；
6. 成本、容量、发布版本和 Release digest 可关联；
7. 单 / 双副本声明有真实一致性指标。

---

## 2. 关联 ID

所有服务、事件和 Trace 统一：

```text
request_id
trace_id
span_id
org_id
workspace_id?
principal_type
principal_id
thread_id?
run_id?
release_digest?
policy_version?
deployment_version
environment
```

规则：

- request_id 由入口生成或验证后接受；
- trace_id 遵循 Trace 标准；
- run_id 创建后贯穿模型、Tool、Sandbox、Audit 和 Usage；
- org_id 可进入结构化日志和 Trace 属性，但不作为公共 Metrics 的无界标签；
- principal_id 不进入公开 Dashboard；
- release_digest 与应用 deployment_version 分开；
- 客户端提交的关联 ID 必须校验长度和字符，不能造成日志注入。

---

## 3. 结构化日志

### 3.1 格式

生产使用 JSON 或等价结构化格式：

```text
timestamp
level
service
environment
deployment_version
message
event_name
request_id
trace_id
org_id
workspace_id
principal_type
principal_id
thread_id
run_id
release_digest
error_code
duration_ms
outcome
```

### 3.2 级别

| Level | 使用 |
| --- | --- |
| DEBUG | 本地 / 临时诊断；生产默认关闭 |
| INFO | 生命周期与成功状态摘要 |
| WARN | 可恢复降级、重试、接近阈值 |
| ERROR | 请求 / 任务失败，需要调查 |
| CRITICAL | 跨 Org、安全控制失效、数据完整性或全局不可用 |

禁止把 Policy deny、普通 404 等预期结果全部记为 ERROR。

### 3.3 禁止字段

- Authorization / Cookie / API Key；
- Secret / Token / DSN；
- 完整 Prompt / Response；
- 文件正文；
- 签名 URL query；
- OIDC 完整 claims；
- Connector 原始敏感结果。

调试敏感内容必须限定环境 / Org、审批、自动过期和短保留。

### 3.4 日志事件

至少：

```text
gateway.request.completed
auth.login.result
tenant.context.bound
run.created
run.status.changed
run.owner.changed
run.reconcile.result
policy.evaluated
tool.call.completed
mcp.call.completed
sandbox.lease.changed
model.call.completed
release.resolved
audit.outbox.result
```

AuditEvent 是独立证据，不通过复制日志实现。

---

## 4. Metrics

### 4.1 标签规则

允许低基数标签：

```text
service
environment
deployment_version
route_template
method
status_class
error_code
run_status
tool_name（受控注册表）
model
provider
channel
outcome
```

默认禁止：

```text
org_id
user_id
principal_id
request_id
trace_id
run_id
thread_id
raw_url
artifact_name
```

按 Org 运营数据通过 UsageRecord、受控日志 / Trace 查询和 Console 聚合，不在公共时序库制造无界标签。

### 4.2 Gateway

```text
http_requests_total
http_request_duration_seconds
http_request_size_bytes
http_response_size_bytes
active_sse_connections
sse_reconnect_total
sse_first_business_event_seconds
oidc_login_total
rate_limit_total
```

### 4.3 Run

```text
runs_created_total
runs_status_total
run_duration_seconds
run_admission_duration_seconds
run_cancel_total
run_resume_total
run_terminal_convergence_seconds
run_ownership_acquire_total
run_ownership_conflict_total
run_lease_expired_total
run_heartbeat_failure_total
run_reconcile_total
run_reconcile_backlog
run_dispatch_backlog
run_dispatch_oldest_age_seconds
worker_active
worker_claim_total
worker_dead_letter_total
```

### 4.4 Model / Tool / MCP

```text
model_calls_total
model_call_duration_seconds
model_tokens_total
model_cost_amount
tool_calls_total
tool_call_duration_seconds
policy_decisions_total
mcp_calls_total
mcp_call_duration_seconds
```

Tool / MCP 名必须来自受控 Catalog；未知名字归一为 `other`，避免用户输入成为标签。

### 4.5 Sandbox

```text
sandbox_acquire_duration_seconds
sandbox_active
sandbox_pending
sandbox_timeout_total
sandbox_oom_total
sandbox_cleanup_failure_total
sandbox_quarantine_total
```

### 4.6 数据与审计

```text
db_pool_in_use
db_query_duration_seconds
db_transaction_failure_total
redis_command_duration_seconds
redis_stream_lag_seconds
redis_memory_bytes
audit_outbox_pending
audit_outbox_oldest_age_seconds
audit_publish_failure_total
audit_archive_lag_seconds
usage_ingest_lag_seconds
object_digest_mismatch_total
backup_last_success_timestamp
```

---

## 5. Trace

### 5.1 根 Span

HTTP：

```text
HTTP <method> <route_template>
```

Run：

```text
run <run_id>
```

Scheduler / Channel：

```text
scheduled_run
channel_event
```

### 5.2 Span 层级

```text
gateway request
  → authenticate
  → resolve tenant
  → authorize
  → resolve release
  → policy admission
  → persist run
  → run execution
      → graph node
      → model call
      → policy tool evaluation
      → tool / mcp call
      → sandbox acquire / execute / release
  → audit / usage enqueue
```

### 5.3 属性

允许：

```text
org_id
run_id
thread_id
release_digest
policy_version
route
model
provider
tool_registry_name
decision
error_code
```

禁止 Secret、Prompt、Response、文件正文和高敏感 Tool 参数。

### 5.4 采样

- 错误、Policy deny、Sandbox 安全违规、system-admin 跨 Org 操作 100% 保留；
- 普通成功请求可按流量采样；
- 采样决策应尽量在知道错误 / 风险后做 tail-based 或等价处理；
- 不因采样丢失 AuditEvent；
- Trace 保留期与日志、Audit 分离。

---

## 6. SLI / SLO

所有 SLI 分子和分母来自 §4 的全量计数 Metrics 或明确的合成探针，不得用采样 Trace / 日志替代。Trace 只用于诊断。

### 6.1 Gateway 可用性

```text
good = 非计划维护期间，服务端返回非 5xx 的有效请求
total = 所有进入 Gateway 的有效请求
exclude = 明确健康探针、客户端取消、Policy deny、认证 / 授权 4xx
SLO = 99.5% / 30 天
```

99.5% 月错误预算约 3 小时 36 分钟。

### 6.2 Run 创建成功率

```text
good = 创建 Run 成功，或在契约时间内返回明确可归因结果
total = 通过认证、授权和基本校验的创建请求
exclude = Policy deny、配额 / 限流拒绝、无效 Release、客户端取消
SLO = 99% / 7 天
```

需同时监控：

- 平台失败；
- Policy / RBAC 拒绝；
- 客户端验证失败；
- 资源背压。

不能把平台 5xx 归为 Policy deny 排除。

### 6.3 Run admission 延迟

```text
start = Gateway 接受有效创建请求
end = Run 进入 running，或返回明确失败 / pending 原因
SLO = P95 < 3 秒 / 7 天
```

模型首 token 不包含在 admission。

### 6.4 SSE 首业务事件

```text
start = Run 创建成功
end = 客户端收到第一条非 keepalive 业务事件
SLO = P95 < 5 秒 / 7 天
```

重连场景单独统计。

### 6.5 Console

```text
范围 = stats / runs / usage 默认查询
探针 = 默认 24 小时时间窗、page limit=50
数据量 = staging 合成 Org 至少 100,000 Runs、1,000,000 UsageRecords
SLO = P95 < 500ms / 7 天
```

超大导出和自定义长时间范围不计入同步查询 SLO。Fork 后如实测容量基线改变，必须同 PR 修改本条件、压测数据和告警。

### 6.6 Redis Stream

```text
SLI = 生产事件时间到消费者可见时间
SLO = P95 < 2 秒 / 7 天
```

### 6.7 Run 一致性

Profile H：

```text
可达 Owner cancel propagation ≤60 秒
Owner 故障后的 terminal convergence ≤300 秒
滚动重启僵尸 Run <0.1%
```

Profile S 不宣称 HA；进程恢复并通过 readiness 后，Reconciler 仍须在 ≤300 秒内使非终态 Run 被继续、明确失败或进入人工处理。

Profile W 额外监控 dispatch backlog / oldest age、Worker claim / heartbeat / version、Envelope 校验失败、dead letter 和 Gateway→Worker cancel propagation；Profile W 不改变 PostgreSQL 作为 Run / terminal 权威源。

### 6.8 安全与正确性零容忍

以下目标为 0，不使用普通错误预算：

- 跨 Org 数据泄露；
- prod 执行未发布 / 错误 digest；
- 强审计写路径静默丢失；
- 生产 host bash；
- Secret 进入普通日志。

发生即进入 P1 / 安全事件流程。

### 6.9 备份

```text
backup job success = 100% / 30 天
RPO = 日备 ≤24h；启用 PITR ≤15min
RTO = ≤4h
```

以恢复演练而不是 Job 绿色状态作为最终证据。

---

## 7. 错误预算

### 7.1 消耗

按 30 天滚动窗口计算 Gateway 可用性；按 7 天窗口计算 Run 创建。Gateway 预算为 0.5%，Run 创建预算为 1%。任一预算在窗口前半段消耗超过 50% 时暂停高风险发布，耗尽时进入变更冻结，只允许安全和稳定性修复。

建议多窗口燃烧率：

| 条件 | 级别 | 动作 |
| --- | --- | --- |
| 1h 快速窗口高燃烧 + 6h 持续 | P1 | 立即响应 |
| 6h 高燃烧 + 3d 趋势 | P2 | 当班处理 |
| 预算消耗 >50% 且窗口未过半 | P2 | 暂停高风险发布 |
| 预算耗尽 | 变更冻结 | 只允许稳定性 / 安全修复 |

具体燃烧倍率在观测平台落地时按全量 SLI Metrics 和请求量校准，不使用 Trace 采样数据计算预算，也不在缺少真实基线时伪设精确告警。

### 7.2 不进入普通预算

- 安全零容忍项；
- 数据完整性；
- Audit 静默丢失；
- 备份无法恢复；
- 合规保留失败。

这些事件按 Incident 处理。

---

## 8. Dashboard

### 8.1 平台总览

- Gateway availability / latency / traffic / errors；
- Run create、状态、时长、失败；
- SSE；
- PostgreSQL / Redis；
- Sandbox；
- Audit / Usage lag；
- 当前 deployment version；
- 有效安全豁免和维护窗口。

### 8.2 Runtime

- pending / running / interrupted / terminal；
- lease、heartbeat、reconcile；
- Tool / MCP / Model；
- checkpoint；
- cancel / resume；
- Profile S / H / W 声明；Profile W 同时显示 Gateway 接入层采用 S 或 H。

### 8.3 Control Plane

- Auth / Membership / RBAC 失败；
- Admin / Studio 写；
- Release promote / rollback；
- Audit outbox / archive；
- Console 查询；
- API Key 异常。

### 8.4 Tenant 运营

按 Org 的 Run、token、cost、failure 使用受控 Console / Usage 聚合，不在共享 Metrics Dashboard 暴露 Org 标签。

---

## 9. 告警

| 条件 | 级别 |
| --- | --- |
| 跨 Org 泄露证据 | P1，立即遏制 |
| prod digest 错误 / 缺失 | P1 |
| PostgreSQL 不可达 | P1 |
| Audit Class A outbox / 同事务本地持久化不可写 | P1 |
| Audit 归档超过 24h 无成功 | P2 |
| Audit 归档摘要校验失败 | P1 |
| Audit dead letter >0 | P2 |
| Sandbox 逃逸迹象 | P1 |
| Gateway 5xx >5% 持续 5m | P1 |
| Gateway 5xx >1% 持续 5m | P2 |
| Redis 不可达且 Profile H | P1 |
| Run create P95 >5s 持续 15m | P2 |
| SSE first-event P95 >5s 持续 15m | P2 |
| Console 默认查询 P95 >500ms 持续 15m | P2 |
| Redis Stream lag P95 >2s 持续 15m | P2 |
| Reconcile backlog >100 或 oldest >5m | P2 |
| Audit outbox oldest >5m | P2 |
| Sandbox Pool 耗尽 >10m | P2 |
| Backup 超声明 RPO | P1 |
| Object digest mismatch >0 | P1 |
| Certificate / Secret ≤14 天到期 | P2 |
| Certificate / Secret ≤7 天到期 | P2，升级 On-call |
| Certificate / Secret ≤1 天到期或已过期 | P1 |

每条告警必须有：

```text
owner
severity
summary
impact
dashboard
runbook
silence_rule
escalation
```

禁止告警链接要求接收者拥有不必要的跨 Org 权限。

---

## 10. 合成与 Smoke

### 10.1 外部

- 登录入口可达；
- Runtime API 认证行为；
- 创建轻量 Run；
- SSE 首事件；
- Admin Console 默认查询；
- 不使用生产客户内容。

### 10.2 内部

- PostgreSQL 最小事务；
- Redis Stream 写读；
- Object Store 小对象写读删；
- Sandbox acquire / execute / release；
- Audit outbox；
- Release resolve。

### 10.3 Release SLO Smoke

发布证据必须保存逐项结果，不只保存一个布尔值：

```text
gateway_availability_probe
run_create_probe
run_admission_latency_probe
sse_first_business_event_probe
console_default_query_probe
redis_stream_lag_probe
result
query_or_probe_id
observed_value
threshold
```

Smoke 用于发现明显回归，不替代 7 / 30 天 SLO 窗口。

深度 Smoke 不作为高频 readiness probe，避免依赖故障被放大。

---

## 11. 发布标记

每次应用部署和 Agent 发布写观测标记：

```text
deployment_id
git_commit
image_digest
database_revision
feature_flags
agent_release_event_id?
release_digest?
operator
occurred_at
```

Dashboard 和 Trace 可以按版本比较发布前后错误与延迟。应用回滚与 Agent Channel 回滚分别标记。

---

## 12. 保留

建议 MVP：

| 数据 | 热保留 |
| --- | --- |
| 应用日志 | 30 天 |
| Trace | 7–14 天 |
| Metrics | 90 天或平台可用范围 |
| AuditEvent | ≥90 天热、≥365 天总保留 |
| UsageRecord | 按运营与合同要求，默认 ≥365 天 |

高成本字段先采样 / 聚合，不通过缩短 Audit 保留解决。

---

## 13. Owner 与运行流程

- Gateway：平台后端；
- Runtime / RunManager：运行时；
- PostgreSQL / Redis / Backup：SRE / DBA；
- Sandbox：平台 + 安全；
- Audit：Control Plane + 安全；
- Release：Studio + Runtime；
- Tenant Console：Control Plane。

值班交接至少包含：

- 当前 P1 / P2；
- 错误预算；
- 最近发布；
- Audit / Usage / Reconcile backlog；
- 备份状态；
- 有效豁免；
- 容量风险。

---

## 14. 验收

- [ ] 日志具备关联 ID 且无 Secret
- [ ] HTTP、Run、Model、Tool、MCP、Sandbox Trace 可串联
- [ ] 高基数 ID 不进入公共 Metrics 标签
- [ ] Gateway / Run / SSE / Console / Redis SLI 可计算
- [ ] Profile H consistency 指标可计算
- [ ] Profile W dispatch、Worker、dead letter 与 cancel propagation 指标可计算
- [ ] 安全零容忍项具备 P1 告警
- [ ] Backup / Audit / Usage lag 可监控
- [ ] ADR-0005 §14 的 archive verify / dead letter 告警可触发
- [ ] P1 / P2 告警到达责任人并链接 Runbook
- [ ] 发布标记可在 Dashboard 与 Trace 查询
- [ ] 错误预算和变更冻结流程可执行
- [ ] 合成探针不使用真实客户内容
- [ ] 观测保留符合本规范和 ADR-0005

---

## 15. Fork 后实现映射

补充：

1. OpenTelemetry SDK / Collector 配置；
2. 日志平台、Metrics、Trace 后端；
3. Dashboard 与 Alert 链接；
4. 实际 metric / span 名；
5. Sampling 配置；
6. SLO 计算查询；
7. On-call 与升级渠道；
8. 观测成本基线；
9. 数据访问权限；
10. 生产发布标记实现。

### 15.1 PR-062 已交付（基础层 + HTTP + Run span）

- **Item 1（OTel SDK / Collector 配置）**：`deerflow/observability/tracing.py::init_tracing` 装配 `TracerProvider` + OTLP/HTTP exporter（`OTLPSpanExporter(endpoint=observability.otel.exporter_endpoint)`）+ `BatchSpanProcessor`。默认 `exporter_endpoint=None` → API 层 no-op tracer，零 SDK 成本；操作者经 `config.yaml` 的 `observability.otel.exporter_endpoint` 显式开启。Resource attributes：`service.name`（来自 `observability.service_name`）、`service.namespace`（`observability.otel.service_namespace`，默认 `deernexus`）、`deployment.environment`（`observability.environment`）、`service.version`（`observability.deployment_version` 非空时）。
- **Item 4（实际 span 名）**：HTTP 根 span = `HTTP <method> <route_template>`（如 `HTTP GET /api/threads/{thread_id}`，§5.1）；Run 根 span = `run <run_id>`（§5.1）。span 属性用 §5.3 allow-list（`org_id` / `run_id` / `thread_id` / `release_digest` / `policy_version` / `route` / `model` / `provider` / `tool_registry_name` / `decision` / `error_code`）+ HTTP 语义约定（`http.method` / `http.route` / `http.status_code` / `http.response.status_class` / `http.url`）+ `duration_ms` / `event_name`。
- **Item 5（Sampling 配置）**：默认 `ParentBased(TraceIdRatioBased(rate=observability.otel.sampler_ratio))` head sampler（默认 ratio 0.1）。**§5.4 tail-based 规则（errors / Policy deny / Sandbox 违规 100% 保留）延后**——需 Track C / Track E 的 deny / violation 代码路径，`tracing.init_tracing` 内 TODO 标注依赖。在 tail sampler 落地前，需要 100% 采样的环境可设 `sampler_ratio: 1.0`。

JSON 日志格式（§3.1）由 `deerflow/observability/logging_setup.py::JsonFormatter` 实现，canonical 19 字段顺序与 §3.1 一致。关联 ID（§2）由 `deerflow/observability/correlation.py` 的 ContextVar 承载，`app/gateway/correlation_middleware.py` 在最外层中间件绑定。命名事件（§3.4）经 `deerflow/observability/events.py::emit_event` 单一 choke-point 发出（便于 PR-063 / 未来 outbox 替换 sink），PR-062 只埋 `gateway.request.completed` 一条，其余 13 条由各自功能 PR 埋点（见 runtime-contracts §16.24）。

### 15.2 PR-063 已交付（Metrics / Dashboard / Alerts）

- **Item 2（Metrics 后端）**：`prometheus_client` + `/metrics` 端点（`app/gateway/routers/metrics.py`，公网，canonical content-type）。注册表 `deerflow/observability/metrics.py` 用 lazy singleton accessors（per-registry `@lru_cache`）+ §4.1 标签基数 allow-list 护栏（`ALLOWED_LABELS` + 构造时 raise，非白名单标签直接拒绝）+ fail-open wrapper。常量标签 `service` / `environment` / `deployment_version` 由 lifespan seed（镜像 OTel Resource attributes）。已连线指标覆盖 §4.2 Gateway（HTTP + SSE + rate-limit）、§4.3 Run core（create/status/duration/admission/cancel/reconcile/worker_active）、§4.4 Model/Tool/MCP（minus cost + policy）、§4.5 Sandbox acquire/active、§4.6 DB pool/query/transaction。延后清单见 runtime-contracts §16.26（Profile-W HA / Sandbox OOM-quarantine / Redis / Audit outbox / cost / Policy / OIDC）。
- **Item 3（Dashboard / Alert 链接）**：`deploy/dashboards/*.json`（4 个 Grafana dashboard：§8.1 平台总览 / §8.2 Runtime / §8.3 Control Plane / §8.4 Tenant Ops）+ `deploy/alerts/prometheus-rules.yaml`（§9 P1/P2 告警，每条带八字段 annotation）。部署说明 `deploy/README.md`（Grafana provisioning + `kubectl apply`）。每面板写真实 PromQL 引用本 PR 注册的指标；Tenant Ops 按 §8.4 / §4.1 不暴露 org 标签（按 Org 数据走 UsageRecord / Tenant Console）。
- **Item 6（SLO 查询）**：§6.1 Gateway 可用性、§6.2 Run 创建成功率、§6.3 admission 延迟 P95、§6.4 SSE 首事件 P95、§6.7 reconcile backlog 的 PromQL 均落在 dashboard 面板 + 告警阈值（`run_admission P95>5s`、`reconcile backlog>100`、`sse_first_business_event P95>5s`、`gateway 5xx>5%/1%`）。`emit_event` §3.4 → §4 fan-out（`_EVENT_METRIC_FANOUT`）让未来 PR 埋的命名事件自动驱动 counter。
