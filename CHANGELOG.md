# Changelog

本项目使用语义化版本号。变更按用户可观察能力归类；完整实现证据以测试、设计文档与交付回执为准。

## 1.0.2 — 2026-09-29

### Added

- 新增组合身份与 identity-bound receipt：按 subject、artifact、policy、context、evidence 组件精确判断 `VALID / STALE / INVALID`。
- 新增 Review Issue 生命周期、稳定 fingerprint、Author FIXED claim、Reviewer VERIFIED 与 PASS gate 派生 CLOSED；问题可重开或在迭代上限进入 BLOCKED。
- 新增七态 WorkUnit、writer epoch/CAS 接管、原子状态仓和 STARTED/COMMITTED 非幂等恢复语义。
- 新增 Codebase freshness barrier、代码身份、dirty epoch、coverage observation 和 `codebase-context` 辅助入口。
- 新增代码评审证据计划，固定 Memory → codebase freshness → graph → targeted source → bounded text 顺序。
- 新增 Delivery Gate Composer；每项 requirement 输出 policy IDs、fact IDs、evidence refs 和状态，并可绑定完整 release-candidate identity。
- 新增跨平台 AtomicFile Adapter，并在 Windows CI 中覆盖全部新增模块导入和 Python 行为套件。
- 新增稳定 `VERSION` 文件，源码发布包在没有 `.git` 时也能报告 `1.0.2`。

### Changed

- Task Route 仅裁决工作量与流程深度；Governance 独立追加 checks、reviews、receipts 和 artifacts。
- 旧 `min_route:*` 治理动作改为显式 requirement 迁移；未知 legacy 值 fail-closed，不再隐式提升路线。
- 项目知识检索采用 MCP → 等价 CLI → 有界文本的单通道降级链，并传播 route/stage/purpose/subject/session 上下文与成本证据。
- 结构检索、代码评审和安全评审优先使用新鲜 codebase-memory 图谱；Git/SVN 或代码写入后必须先重验索引新鲜度。
- 根 `AGENTS.md` / `CLAUDE.md` 缺失时由安装器安全创建受管容器；doctor/reminder 分别报告 configured、visible、indexed、fresh，不再把“已配置”冒充“可用且新鲜”。
- CI 改为自动发现根目录 `tests/*.test.py`，避免新增行为测试未登记而漏跑。
- MCP server 与 Application Service 版本更新为 1.0.2；最低兼容服务版本仍为 1.0.1。
- README 与 Overview 更新为 1.0.2 的产品模型和使用约束。

### Fixed

- 修复 Agent 在查项目历史知识、理解代码结构或执行代码评审时直接 grep、绕过 Memory/codebase MCP 的工作流缺陷。
- 修复代码生成、Git/SVN 更新、切分支或合并后未判断图谱需要刷新而继续使用旧索引的问题。
- 修复风险规则与任务复杂度耦合，导致小改动无理由进入重流程的问题。
- 修复安装到空白仓库后根指引不可达，以及能力自检把配置存在误判为索引已就绪的问题。
- 修复 Windows 使用系统 ACP 读取 UTF-8 受管根指引而导致安装失败的问题，并增加文本 I/O 编码守卫。
- 修复 Windows `msvcrt` 固定等待窗口在高并发 telemetry 追加时可能抛出 deadlock 错误的问题。
- 修复评审问题可被非授权参与者关闭、增量评审证明不足仍可能继续，以及中断恢复可能重复非幂等动作的问题。

### Compatibility and notices

- 保持 Python 3.10+、离线基础安装、既有 CLI 参数/退出码、MCP 工具名与成功响应字段兼容。
- POSIX 默认 symlink，Windows 默认 copy；Windows 不要求开发者模式或符号链接权限。
- 本版本未引入新的第三方运行时或许可证范围；第三方归属见 `THIRD_PARTY_NOTICES.md`。

## 1.0.1

- 引入四路线 Route Card、动态 ArtifactPlan、Context Capsule、增量 Review、上下文成本基准和单路径 Application Service 调度。
- 完整历史说明见 [v1.0.1 的 README](https://github.com/kfkfkz/agent-engineering-kit/blob/v1.0.1/README.md#101-架构升级)。

## 1.0.0

- 首个稳定版本：提供 Skills、需求/设计文档流水线、确定性门禁、项目记忆、跨平台安装生命周期与 MCP 入口。
