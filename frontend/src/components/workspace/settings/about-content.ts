/**
 * About DeerNexus markdown content. Inlined to avoid raw-loader dependency
 * (Turbopack cannot resolve raw-loader for .md imports).
 */
export const aboutMarkdown = `# 关于 DeerNexus

> **企业级多租户 Agent 操作系统**

DeerNexus 是面向企业的多租户 Agent 操作系统（Agent OS），在开源超级智能体能力之上，
构建组织隔离、操作审计与版本发布的生产级控制平面，安全编排每一个 Agent 生产场景。

---

## 🛡️ 三大命脉

* **组织隔离（Org Isolation）**：TenantContext 贯穿 HTTP、异步任务、Scheduler、IM 与 Worker，
  跨 Org 资源在任何已知入口均不可见、不可操作。
* **操作可审计（Auditable Operations）**：关键管理写入、权限变更、Agent 发布与回滚全部产生
  AuditEvent，append-only、可查询、可导出，OrgA 查询不返回 OrgB。
* **版本可发布（Versioned Releases）**：生产 Run 只执行不可变 ReleaseRef（digest 锁定），
  v1 → v2 → rollback 全链路可重现，历史 Run 引用永不漂移。

---

## 🔧 核心能力

* **Skills & Tools**：内置与可扩展的技能与工具，覆盖研究、编码、生成与部署。
* **Sub-Agents**：子智能体分担复杂多步骤任务，并行编排。
* **Sandbox & File System**：在安全 Docker 沙箱中执行代码与操作文件。
* **RBAC & ServiceAccount**：角色矩阵、API Key 范围收窄、默认拒绝。
* **Run Lease & Reconcile**：多副本 Run 所有权协调，Redis 非权威、PostgreSQL 终态优先。

---

## 🌐 仓库

探索 DeerNexus：[github.com/MementoMoriCheng/DeerNexus](https://github.com/MementoMoriCheng/DeerNexus)

---

## 📜 License

DeerNexus 基于 **MIT License** 开源。

---

## 🙌 致谢

DeerNexus fork 自开源超级智能体框架 [DeerFlow](https://github.com/bytedance/deer-flow)
（bytedance/deer-flow），感谢上游作者与开源社区的卓越贡献。我们站在巨人的肩膀上，
将超级智能体能力推向企业级多租户生产场景。
`;
