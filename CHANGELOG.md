# Changelog

本项目使用语义化版本号。变更按用户可观察能力归类；完整实现证据以测试、设计文档与交付回执为准。

## Unreleased

## 1.0.3 — 2026-10-10

本轮聚焦工具可靠性、性能边界与兼容性。新增评测接缝是开发侧离线诊断，不包含在线模型实验或未经验证的 Token/竞品收益结论；正常安装不依赖模型服务。

### Fixed

- 修复干净 checkout 遗漏公开历史场景文档的问题，保持内部文档默认不分发；修复 SQLite 验证器连接未关闭导致 Windows 清理覆盖原始失败分类，以及 Windows 保留文件名/UTF-8 诊断测试夹具兼容性。

- 修复 Benchmark 空验证命令被误计成功；新增 PASS/FAIL/NOT_EVALUATED/INFRA_ERROR 结果状态，保留旧评测入口及 success 字段，配对报告区分未评测与失败。
- 非幂等 WorkUnit 进入 FAILED_RETRYABLE 或 STALE 后仍核验提交回执，避免恢复建议绕过 COMMITTED/STARTED 保护；已提交但所需身份变化时转人工核对，旧提交不授权当前任务。
- 统一 Overview、repo-delivery 与 Bounded 路线的工作量/风险分离规则，移除以风险标签直接升径的旧描述。
- 明确已实现的工作流恢复、未来宿主会话恢复与 MCP 进程内回执去重的能力边界。
- 修复 Memory MCP 用途契约与预算层不一致导致自然语言 purpose 在检索后抛 traceback；统一枚举、前置校验和可修复诊断，并更新 Skill/根指引。会话 ID 与摘要也在文件访问前校验，防止非法参数越过记录目录或触发账本截断。
- 运行报告接入 manifest-bound 流程证据与独立汇总，陈旧/跨运行/不完整证明不产生完成结论；公开/隐藏检查匹配和重复样本检测改为索引/单次计数，避免平方级扫描。
- 修复身份 Envelope/Bound Receipt 把布尔或浮点 schema_version 当整数 1 接受的问题，严格解码与运行时对象校验采用一致的整数约束。

### Added

- 1.0.3 开发侧评测基础：严格可信 JSON 场景、参数数组与固定脚本摘要的独立检查、当前 ArtifactPlan/回执证据核验；旧 evaluate.py 增加 --scenario-file 诊断入口，真实实验与成本未观测时明确保持未知。
- 开发侧 Benchmark CLI 只读诊断入口，核对 Codex/Claude 版本及公开参数，保留有界摘要；不启动模型，也不把 CLI 参数可用冒充认证或隔离就绪。
- 开发侧固定 Git fixture 文件基线与独立目录、显式 fake 配对及带外 manifest 摘要恢复；复用 WorkUnit/CAS 防止重复执行，执行完成不推导 Outcome 或真实 Token。
- 开发侧六个公开合成场景（局部 Bug、CRUD、并发防重、SQLite 迁移、历史约束、外部提交恢复）及独立正/负参考检查；公开样例不作为隐藏验证或真实 Agent 收益证据。
- 开发侧 Codex/Claude single-shot JSONL 事件与用量解析及只读 collect 入口：明确计数范围/来源、费用估算与 unknown，拒绝截断/重放终态，不把累计会话、主循环或崩溃零值冒充本轮完整成本。
- 开发侧严格配对身份、顺序轮换计划与 JSON/Markdown 汇总：按控制条件分组，保留无效/未知/基础设施失败，拒绝重复样本和矛盾的 PASS；外部锚缺失时不自动信任报告。
- 开发侧产物凭证连接到单次和汇总报告：固定代码/场景/计划/文档/策略/运行与验证身份，复用现有 bound receipt 校验；不以产物 PASS 代替代码正确性。两 CLI 参数构造与命令绑定的能力探测支持离线检查，不代表 live/auth/隔离就绪。
- 开发侧 WA-001～007 有界流程建议：基于完整语义证据、代码/材料/用途身份分析，缺证据不猜测；仅输出稳定 finding 与建议，不改源码。新增可选 Linux bubblewrap 离线 canary 自检，不作为真实 CLI 隔离或运行授权。
- doctor 增加团队治理规则与当前默认规则的只读差异提示；历史来源不明时标记 unknown，不自动改写策略或改变安装健康退出码。

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
