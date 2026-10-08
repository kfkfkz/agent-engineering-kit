# Agent Engineering Kit（AEK）

让 Claude Code、Codex 等 Coding Agent 在现有仓库中按工程规则完成开发：先理解项目，再按任务复杂度选择流程，最后用审查和验证证据交付。

AEK 将 Skills、项目记忆、代码图谱接入和可重复执行的门禁安装到你的项目。适合持续维护的存量系统，也能用于新项目；日常通常只需要一个入口：`repo-delivery`。

[快速开始](#快速开始) · [使用指南](USAGE.md) · [设计与架构](overview.md) · [版本记录](CHANGELOG.md)

## 能做什么

- **按任务大小控制流程**：局部修复走轻量路径，跨模块或长期任务再进入设计与任务拆分；安全、数据和兼容风险独立追加审查。
- **先取得项目事实**：历史决策和经验优先查 Memory MCP；符号、调用链和影响范围优先查代码图谱，代码更新后重新确认索引。
- **让设计和实现接得上**：按需生成需求、设计、任务和测试文档，追踪评审问题与上游变化；业务流程图由 archify 生成。
- **凭证随版本变化**：代码、文档、策略或证据变化后，重新检查相关结论；中断任务先核验进度和提交状态再继续。

当前稳定版为 **1.0.2**，版本变化见 [Changelog](CHANGELOG.md)。

## 快速开始

需要 **Python 3.10+**。获取稳定版：

```bash
git clone --branch v1.0.2 --depth 1 https://github.com/kfkfkz/agent-engineering-kit.git
cd agent-engineering-kit
```

### 1. 安装到项目

Linux / macOS / WSL：

```bash
./install.sh /path/to/your-project
```

原生 Windows，在 AEK 目录下用 PowerShell：

```powershell
python -m installer "D:\code\your-project"
```

基础安装在取得 AEK 源码后可离线执行。安装器管理自己的资源，并保留用户记忆和根指引的非受管内容。

如果 Codex 通常从父级 workspace 启动，安装时指定 workspace 根目录：

```bash
./install.sh --codex-root /path/to/workspace /path/to/workspace/your-project
```

Windows 的等价方式是 `python -m installer --codex-root "D:\code" "D:\code\your-project"`。
POSIX 默认使用受管技能链接；Windows 默认复制，不要求符号链接权限。

### 2. 按需启用增强能力

| 能力 | 依赖与接入 |
| --- | --- |
| 代码结构、调用链与影响分析 | `--with-codebase-memory` 安装或配置 codebase-memory MCP；此步骤需要联网 |
| 项目文档与记忆的语义检索 | 安装可选依赖 `zvec`；索引和查询细节见[使用指南](USAGE.md#项目记忆与代码图谱) |
| archify 交互图表 | Node.js ≥18；自动截图还需要 Chrome/Chromium |

例如，安装时同时接入代码图谱：

```bash
./install.sh --with-codebase-memory /path/to/your-project
```

安装后重新打开目标项目的 Agent 会话，让 Skills 和 MCP 配置生效。安装检查只能证明配置状态；首次使用时，Agent 还会核对工具是否可见、仓库是否已索引以及图谱是否覆盖当前代码。

### 3. 开始一个任务

Claude Code：

```text
/repo-delivery 为订单列表增加 CSV 导出，沿用当前筛选和数据权限，最多导出 5 万条。
```

Codex：

```text
$repo-delivery 为订单列表增加 CSV 导出，沿用当前筛选和数据权限，最多导出 5 万条。
```

也可以描述缺陷，例如“支付回调重试时偶发重复扣减库存，请定位并修复”。只想做设计时，在请求中加上“本轮不要编码”。

Agent 会按范围完成取证、实现和验证。需要人工确认时，给出结论与签核人即可；随后由 Agent 记录并继续流程。

## 任务会走多深

| 路线 | 典型范围 | 工作深度 |
| --- | --- | --- |
| Direct | 文案、格式等局部无行为修改 | 修改与针对性验证 |
| Bounded | 明确、局部、可逆的小功能或缺陷 | 简短验收、必要测试与快速审查 |
| Standard | 跨模块、公共契约变化或实质不确定性 | 影响分析、适用设计、计划与审查 |
| Initiative | 多仓库、长期或多团队协作 | 完整 SDD、工作单元拆分与集成验证 |

工作量与风险分别判断。例如，一行认证修改可以保持轻量流程，同时完成必要的安全审查。SQL/ORM 变化先做适配目标数据库的低成本初查，有规模或热点风险再深入。

## 专项入口

通常让 `repo-delivery` 自动调度；目标明确时也可单独使用：

| 目的 | Skill |
| --- | --- |
| 理解代码结构与影响范围 | `codebase-memory` |
| 生成、评审与推进设计文档 | `design-pipeline` |
| 定位故障 / 测试先行实现 | `systematic-debugging` / `tdd` |
| 技术选型 / 安全审查 | `reuse-research` / `security-review` |
| 审查与验证交付结果 | `delivery-gate` |
| 交接任务 / 迁移 Spec Kit 文档 | `task-handoff` / `spec-migrate` |
| 巡检 / 沉淀项目记忆 | `memory-check` / `memory-capture` |

Claude Code 使用 `/技能名`，Codex 使用 `$技能名`。CLI 命令由 Agent 在内部执行；用户继续任务的入口仍是 Skill。

## 维护安装

在 AEK 源码目录运行：

```bash
./install.sh --doctor /path/to/your-project     # 检查安装
./install.sh --update /path/to/your-project     # 从当前 AEK 源码更新
./install.sh --uninstall /path/to/your-project  # 移除受管资源，保留用户记忆
```

Windows 将 `./install.sh` 替换为 `python -m installer`。升级源码版本、SVN 接入、修复与中断恢复见[使用指南](USAGE.md#更新检查与卸载)。

## 进一步阅读

- [使用指南](USAGE.md)：安装选项、检索能力、文档流程、文件所有权、团队协作与故障处理。
- [AEK Overview](overview.md)：设计目标、架构和适用边界。
- [Changelog](CHANGELOG.md)：各版本能力与兼容性变化。
- [CI](https://github.com/kfkfkz/agent-engineering-kit/actions)：Ubuntu 与原生 Windows 验证，包括安装生命周期、CLI/MCP、文档门禁和行为回归。

MIT License，见 [LICENSE](LICENSE)。第三方组件与思想来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
