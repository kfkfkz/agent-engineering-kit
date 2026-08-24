# repo-memory-kit

仓库级跨 Agent 记忆系统：**pitfalls 坑库 + playbooks 流程手册 + 巡检命令**。
让 Claude Code、Codex（以及任何读取 `CLAUDE.md` / `AGENTS.md` 的 AI Agent）在同一套项目记忆上工作。

## 为什么需要

多工具/多模型的工作流里，经验是锁死的：

- 每个 Agent 各学各的坑——Claude Code 踩过的坑，Codex 从零再踩一遍
- 会话里的调试发现、返工原因，会话结束就消失
- 环境操作步骤、测试数据散落在个人笔记里，无法交接

而集中式记忆服务（向量库、记忆平台）对多数团队是过度设计。本套件用**最朴素的机制**解决问题：git 版本化的 Markdown + 两个 Agent 原生读取的入口文件 + 一个巡检命令。

三个设计决策：

1. **单一来源**：`docs/memory/` 是唯一记忆区，`CLAUDE.md` / `AGENTS.md` 只做指针，双端零重复维护
2. **人工把关**：条目状态分「待验证 / 已确认 / deprecated」，未经确认的坑不得作为修改依据（LLM 蒸馏的错误教训会静默毒化后续输出）
3. **可矫正**：每个条目带「校验点」（可机器核验的事实清单），配合 `/memory-check` 巡检，防止接口改了、版本升了而记忆还停留在过去——**过期的记忆比没有记忆更危险**

## 前置依赖

巡检（Claude Code `/memory-check` 与 Codex `memory-check` 技能）**默认强制依赖结构化代码检索工具**：代码知识图谱类（如 codebase-memory MCP），或提供符号级定位、调用关系、索引覆盖/新鲜度信号的等效 codegraph 工具。

为什么 grep 不能作为兜底：

1. 服务路由前缀通常配置在 application 配置文件的 `context-path`——字面量搜索直接漏掉
2. 类/方法改名后，记忆条目里的旧关键字 grep 不到——**静默漏检比误报更危险**
3. 没有索引覆盖信号，无法区分"搜不到"是"不存在"还是"搜法不对"

工具不可用时巡检报告缺失并终止；用户可显式要求降级为 grep 巡检，报告须标注"降级巡检，置信度低"。

## 快速接入

```bash
git clone <this-repo> repo-memory-kit
cd repo-memory-kit
./install.sh /path/to/your-project
```

脚本会：

1. 创建 `docs/memory/{pitfalls,playbooks}/` 并放入 README（含条目模板与全部规则）
2. 安装 `/memory-check` 巡检命令到 `.claude/commands/`（Claude Code）
3. 检测到 `~/.codex/skills` 时安装 Codex 技能 `memory-check`（双端巡检）
4. 向 `CLAUDE.md` 与 `AGENTS.md`（存在时）追加「项目记忆」指引段落（幂等，已存在则跳过）

之后首次沉淀一条记忆：把最近一个 bug 的原因/影响范围/修复方案写成一条 pitfall（模板见 `docs/memory/README.md`）。

## 动态更新

kit 升级后，目标仓库同步最新机制：

```bash
cd repo-memory-kit && git pull
./install.sh --update /path/to/your-project
```

文件所有权约定：

| 文件 | 归属 | --update 行为 |
| --- | --- | --- |
| `.claude/commands/memory-check.md`、Codex 技能、`_TEMPLATE.md` | kit 管辖 | 始终刷新为最新版 |
| `docs/memory/README.md` | 用户所有（含索引与巡检记录） | 不覆盖，有差异时提示手动合并 |
| `CLAUDE.md` / `AGENTS.md` 段落 | 用户所有 | 不动 |

## 核心机制

### 写入触发（记忆怎么来）

| 触发 | 动作 |
| --- | --- |
| Bug 修复（人工审核通过后） | 修复方案分析直接落一条 pitfall，零额外成本 |
| 功能验收通过（功能蒸馏） | 按「功能蒸馏清单」沉淀：核心流程、环境信息、可复用测试数据 → playbooks；过程中的坑 → pitfalls |

### 生命周期（记忆怎么管）

```text
待验证 ──(人工确认)──▶ 已确认 ──(失效)──▶ deprecated（标记，不删除）
```

### 压缩与矫正（记忆怎么保鲜）

- 大小约束：pitfall ≤ 60 行、playbook ≤ 150 行，超限压缩（细节下沉权威文档留指针）
- 用后即校：引用条目发现与现状不符，当场矫正
- 定期巡检：`/memory-check`——补齐校验点 → 逐项核验代码 → 矫正失配 → 压缩超限 → 输出报告（只改记忆文档，不改代码）

详细规则全部在 `templates/memory-README.md`（即目标仓库的 `docs/memory/README.md`）。

## 安全边界

- 凭证类信息（token、密钥、内网地址）**永不入记忆区**——存指针不存值，注明获取途径
- 记忆若接入自动注入系统（见下），凭证进注入层等于每次对话都携带凭证

## 演进路线：人工蒸馏 → 自动蒸馏

本套件是记忆系统的**人工阶段**，条目格式（状态/触发/根因/校验点）刻意与自动蒸馏管线的产物对齐：

| 人工阶段（本套件） | 自动阶段（记忆服务） |
| --- | --- |
| 大小约束 + 压缩下沉 | 召回字符预算（如 maxCharsPerMemory） |
| 校验点 + 代码核验 | 向量去重 + 冲突检测 |
| 用后即校 + 人工确认 | 人工审核后才升为共享资产 |

当坑条目的产量超过人工整理能力时，这些结构化条目就是平移到记忆服务的种子数据——格式不用重整理。

## 目录结构

```text
repo-memory-kit/
├── install.sh                  # 接入/更新脚本（--update）
├── templates/
│   ├── memory-README.md        # → 目标仓库 docs/memory/README.md
│   ├── claude-md-section.md    # → 追加进 CLAUDE.md 的段落
│   ├── agents-md-section.md    # → 追加进 AGENTS.md 的段落
│   └── pitfall-entry.md        # 单条 pitfall 模板
├── commands/
│   └── memory-check.md         # → .claude/commands/memory-check.md（Claude Code）
└── codex/skills/memory-check/
    └── SKILL.md                # → ~/.codex/skills/（Codex）
```

## License

MIT
