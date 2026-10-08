# AEK 使用指南

先从 [README](README.md) 安装并运行第一个任务。本文供需要配置、维护或排查 AEK 的使用者查阅；设计理念与架构见 [Overview](overview.md)。

## 安装选项

以下命令在 AEK 源码目录执行，需要 Python 3.10+。原生 Windows 将 `./install.sh` 替换为 `python -m installer`。

```bash
# 只预览安装计划
./install.sh --dry-run /path/to/project

# Codex 从父级 workspace 启动时，让项目技能在该目录可见
./install.sh --codex-root /path/to/workspace /path/to/workspace/project

# 明确使用受管副本，适用于不能使用 symlink 的环境
./install.sh --codex-root /path/to/workspace --link-strategy copy /path/to/project

# 使用 lightweight 治理 profile
./install.sh --lightweight /path/to/project

# 安装或配置 codebase-memory MCP（需要联网）
./install.sh --with-codebase-memory /path/to/project

# SVN 项目同时初始化版本控制规则
./install.sh --svn /path/to/project
```

基础安装可离线执行；`--with-codebase-memory` 使用 [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) 的安装或配置入口。

POSIX 默认使用 workspace 技能链接，Windows 默认使用副本。Windows compatible 文件后端采用 reparse-point 预检；若需要针对恶意本机并发进程的 POSIX 文件安全保证，使用 WSL。macOS 使用 POSIX 后端，自动 CI 平台为 Ubuntu 和 Windows。

安装后重新打开目标项目的 Agent 会话。Codex 从 workspace 根目录启动时，应在更新时继续传 `--codex-root`，保证新技能链接或副本也被创建和核验。

## 项目记忆与代码图谱

AEK 的记忆 MCP 与 codebase-memory MCP 处理不同的信息：

| 查询内容 | 优先通道 | 回退条件 |
| --- | --- | --- |
| 历史决策、坑点、业务域知识、SDD、旧回执 | AEK `memory_recall` MCP | 调用前确认不可见或不兼容，才使用等价 CLI；两者不可用才做有界文本检索 |
| 符号、调用方、依赖、数据流、影响范围 | codebase-memory 图谱 | 对索引报告的缺口读精确源码；能力不足时披露原因与结论限制 |
| 字面量、错误文案、配置值、非代码文件 | 有界文本检索 | 不需要为了文本搜索刷新结构图谱 |

代码编辑、生成器写入、Git/SVN 更新、切分支或合并后，Agent 在下一次结构查询或交付前重新核对索引。后台已追平时复用；否则同一变更批次最多显式刷新一次，再检查覆盖情况。配置文件存在只证明 configured，不能证明工具 visible、仓库 indexed 或图谱 fresh。

AEK 注册的 MCP 工具为 `memory_recall`、`memory_build`、`domain_check`、`kit_status`，均绑定安装目标仓库。codebase-memory 的工具由其独立运行时提供。

语义检索的可选依赖是 `zvec`。将它安装到 MCP 使用的 Python 环境后，在项目目录重建索引：

```bash
python -m pip install zvec
.repo-memory-kit/bin/memory-build .
```

正常任务由 Agent 调用 MCP。需要本地诊断时，可以使用下列 CLI；Windows 用 `python` 显式启动这些无扩展名 Python 脚本。

```bash
.repo-memory-kit/bin/memory-recall "任务描述" .
.repo-memory-kit/bin/memory-recall --list .
.repo-memory-kit/bin/memory-build --changed .
.repo-memory-kit/bin/domain-check .
```

记忆保存在 `docs/memory/<业务模块>/`，类型由 frontmatter 声明：pitfall、decision、playbook。frontmatter 是事实源；本地索引、符号锚点和语义索引由工具生成。使用 `memory-capture` 提出候选，经维护者确认后入库；`memory-check` 巡检条目与当前代码的一致性。业务域地图登记能力入口与业务链路，playbook 以链接引用它。

## 工作流与设计文档

通常由 `repo-delivery` 从任务开始推进到交付。Claude Code 使用 `/repo-delivery`，Codex 使用 `$repo-delivery`；其余专项 Skill 使用同样的客户端前缀。

```text
任务与项目约束
  → 选择工作量路线 + 独立判断风险要求
  → 按需取证、澄清和设计
  → 实现与测试
  → 审查、验证与交付回执
  → 回写实际结果、保留可恢复状态
```

Direct/Bounded 不默认生成完整 SDD。Standard/Initiative 根据 ArtifactPlan 的 required/optional/skipped 决定产物；无 UI 或数据库变化时，可以通过可信事实跳过对应设计。

需要完整设计时，目录按需求与产物归属组织：

```text
docs/01-需求/
  README.md                       需求指派索引
  NNN-名称/原始需求.md              维护者提供的需求原文

docs/03-SDD/NNN-名称/
  需求分析.md                      场景、验收项与人工确认
  概要设计.md                      功能设计
  UI设计.md                        按需产出
  业务流程设计.md                   人工评审与 archify 流程图
  详细设计.md / API设计.md / 数据库设计.md
  任务清单.md / 测试方案.md
  线上联调功能验证清单.md / 终稿.md   真实验证与 As-Built
  reviews/                         评审输入、问题与门禁凭证
  diagrams/                        图表源、HTML 与验证证据
```

`design-pipeline` 编排概要设计 → 按需 UI → 业务流程 → 详细设计 → 计划。AI 评审提交问题与证据，确定性门禁检查结构、追踪链、阈值和凭证；需求确认与业务流程签核由维护者给出。通常只需回复评审结论和签核人，由 Agent 记录并推进。

问题修复后重新验证；只有匹配当前版本的 PASS gate 才能关闭问题。上游变化使相关下游证据失效；影响边界无法证明时进行完整复核。达到配置的迭代上限后保留问题历史并转人工处理。

计划首次通过时保存执行前基线。执行期可更新 checkbox 和执行记录；任务定义、预计范围或验证方式变化仍需重新评审。完成后统一 closeout，绑定任务清单和测试方案的完整终态哈希。

### 图表与验证

archify 随 AEK 分发，需要 Node.js ≥18。业务流程图的交付链为类型化 JSON → 校验 → 自包含交互 HTML 与哈希回执 → 视觉检查。自动截图需要 Chrome/Chromium；图表内容仍要核对业务事实。

仅在 Node.js 缺失或版本不足、doctor 明确报告 DEGRADED 时允许披露后回退 Mermaid；路径、权限或渲染失败需要排查。图表校验通过不能代替架构或业务正确性评审。

## 安装产物与团队协作

| 位置 | 用途与所有权 |
| --- | --- |
| `.claude/skills/`、`.agents/skills/` | 项目 Skills，两棵树由 AEK 同步 |
| `.repo-memory-kit/bin/`、`lib/` | 项目内工具与运行代码 |
| `.repo-memory-kit/governance` | strict/lightweight profile 权威标记 |
| `.repo-memory-kit/governance.json` | 首次写入的团队规则，后续由团队维护 |
| `AGENTS.md`、`CLAUDE.md` | AEK 维护受管区块，用户保留其余内容 |
| `docs/memory/` | 规则与模板由 AEK 管理，条目和 README 用户笔记归项目 |
| `.mcp.json`、`.codex/config.toml` | MCP 配置片段，含本机路径 |
| `.repo-memory-kit/` 中的 manifest、事务、索引与状态 | 每机派生物，不提交版本库 |

更新和卸载依据 manifest 与内容血统操作。用户记忆不会被删除；被手工修改的受管文件会按冲突保护。卸载通常只移除容器中的 AEK 片段；若 manifest 证明根指引容器由 AEK 创建，且移除区块后没有用户内容，才删除该空容器。

strict/lightweight 控制治理与回执形态，任务路线控制工作深度。团队 `governance.json` 可以追加检查和凭证要求；非法规则会阻断，升级不会覆盖已有团队规则。SQL/ORM 变化先做数据库无关初查，确认目标方言、N+1、结果边界、索引和锁风险；涉及规模、热点或迁移风险时再要求专项性能证据。

团队共享 Skills、记忆条目与设计文档，每台机器各自安装本地工具/MCP。维护者统一升级 AEK 版本，其余成员重跑同版本安装。Git 使用受管 ignore 区块；SVN 使用 `--svn` 合并 ignore 属性、登记确定性产物及设置文本换行属性。

## 已有 Spec Kit 文档

基础安装只发现并报告 `.specify/specs`，不自动修改文档。使用 `spec-migrate` 检查和整理，显式迁移前先预览：

```bash
./install.sh --dry-run --migrate-specify /path/to/project
./install.sh --migrate-specify /path/to/project
```

迁移保留原文，补缺失结构并标为 `migration-pending`；Agent 仍需结合代码、测试和业务判断完成语义核验。附件不自动改写。

要将单个 feature 移入 SDD 布局，必须明确 feature 名与版本；该操作会移动原目录：

```bash
./install.sh --migrate-specify=001-demo --sdd-layout --sdd-version V1.0 /path/to/project
```

## 更新检查与卸载

从稳定版标签克隆的 AEK 处于 detached HEAD。新版本发布后先获取标签，切换到已核对的发布版本，再更新目标项目。下例以当前稳定版为例；升级时替换标签：

```bash
git fetch --tags
git switch --detach v1.0.2
./install.sh --update /path/to/project
./install.sh --doctor /path/to/project
```

从 main 分支克隆的开发副本可用 `git pull` 更新源码。存在本地改动时先检查并处理，避免覆盖。

doctor 为只读检查，可报告 HEALTHY、OUTDATED、DRIFTED、DEGRADED、CONFLICT、INCOMPLETE。宿主 MCP 的 visible/indexed/fresh 信息可能为 unknown，需要 Agent 实际调用工具核验。

```bash
# 按报告修复有 AEK 血统的缺失/漂移产物
./install.sh --repair /path/to/project

# 清单丢失时显式登记可识别的 legacy 安装
./install.sh --adopt-legacy /path/to/project

# 仅在报告要求人工处理未完成事务时，选择恢复策略
./install.sh --recover=rollback /path/to/project
# 或：./install.sh --recover=roll-forward /path/to/project

# 卸载受管资源，保留用户记忆和用户内容
./install.sh --uninstall /path/to/project
```

`--adopt-legacy` 拒绝覆盖已有 manifest；恢复策略需依据事务报告选择。卸载不会移除机器级 codebase-memory MCP。

## 开发与验证

[CI 工作流](.github/workflows/ci.yml) 是自动验证清单的来源，覆盖 Ubuntu 全量回归、ShellCheck、archify 实际执行和原生 Windows 生命周期。新增 Python 行为套件由测试运行器自动发现。

```bash
python3 tests/run-python-tests.py
sh tests/skill-sync.test.sh
sh tests/doc-gate.test.sh
sh tests/installer.test.sh
sh tests/benchmark.test.sh
```

测试依赖按 CI 环境准备。性能与上下文 fixture 用于验证行为和成本约束；真实 Agent 配对的正确率、Token 与时延收益实验计划在 1.0.3 开展。
