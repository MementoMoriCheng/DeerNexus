# AGENTS.md

DeerNexus 仓库的 agent 操作约定。本文件由 ZCode 在每个会话自动读取，
**优先级高于**通用 `.github/copilot-instructions.md`（后者为上游 DeerFlow 英文指南）。

## PR 与提交语言（强制）

**所有 Pull Request 与 Git 提交必须用中文描述。** 这是 DeerNexus 工程文档
（`docs/engineering/progress.md`、`docs/architecture/runtime-contracts.md`、
`docs/adr/`、`docs/engineering/pr-split-guide.md` 等）的既有语言，PR 与之保持一致。

具体要求：

- **PR 标题**：中文。可保留必要的技术标识前缀（如 `feat(stream-bridge):`、`fix(ci):`），
  冒号后的摘要用中文。示例：`feat(stream-bridge): Redis StreamBridge 实现跨副本 SSE 恢复 (PR-073)`。
- **PR 正文**：中文。Summary / Changes / Tests / Rollback 等章节标题与说明均用中文；
  代码标识符、文件路径、命令、配置键名保持原样（不翻译）。
- **Commit message**：中文。首行摘要中文，正文解释中文。

> 历史上的英文 PR（#108–#115 等）保持原样不回填；**自此约定生效后**的新 PR 全部用中文。

## 既定工程约定（与本仓库一致）

- **单一职责 PR**：一个 PR 只做一件事（见 `docs/engineering/pr-split-guide.md`）。
  跨子系统的改动拆成多个 PR（例：PR-073 把「Redis StreamBridge」与
  「persisted cancel intent」拆为 PR-073 + PR-077）。
- **进度账本**：交付后在 `docs/engineering/progress.md` 标记状态 + 落地 commit；
  详细设计写入 `docs/architecture/runtime-contracts.md` 对应章节（Track G 见 §16.x）。
- **提交/推送纪律**：仅在用户明确要求时 commit / push；若在默认分支（`main`）上工作，
  先开分支再提交。外发或不可逆操作先确认。
- **安全边界**：不自动重放副作用（TM-028）；Redis 非权威，PostgreSQL 终态优先（TM-029）；
  CAS（`row_version`）决定 cancel 与 completion 竞争（TM-027）。

## 工具链与测试命令

- 后端测试：`backend/` 下 `PYTHONPATH=. PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run pytest`。
- 后端 lint/format：`backend/` 下 `uv run ruff check` + `uv run ruff format`。
- **务必跑完整后端测试套件**（`tests/`），不要只跑受影响子集——缩减子集会漏掉跨模块回归
  （如 PR-073 的 503 回归就是只跑子集时漏掉的）。

## 已知 pre-existing flakes（非回归，勿误判）

以下测试在干净的 `main` 上也会失败（Windows 文件系统/符号链接/权限相关），
确认回归前先用 `git stash` 在干净树上复现：

- `tests/test_run_manager.py::test_list_by_thread`（时间戳并列排序）
- `tests/test_doctor_probes.py::TestAuditProbe::test_unreachable_db_fails_without_raising`（测试顺序污染）
- `tests/blocking_io/test_channel_runtime_config_store.py::test_runtime_config_store_file_is_owner_only`（Unix 文件权限模式）
- `tests/test_mcp_file_migration.py`（Windows 沙箱/符号链接）
- `tests/test_uploads_router.py` / `tests/test_wechat_channel.py` /
  `tests/test_tool_output_budget_middleware.py` / `tests/test_skill_permissions.py` /
  `tests/test_sandbox_search_tools.py` 中的符号链接/磁盘写入/权限用例
