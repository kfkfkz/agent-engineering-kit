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

## 快速接入

```bash
git clone <this-repo> repo-memory-kit
cd repo-memory-kit
./install.sh /path/to/your-project
```

脚本会：

1. 创建 `docs/memory/{pitfalls,playbooks}/` 并放入 README（含条目模板与全部规则）
2. 安装 `/memory-check` 巡检命令到 `.claude/commands/`
3. 向 `CLAUDE.md` 与 `AGENTS.md`（存在时）追加「项目记忆」指引段落（幂等，已存在则跳过）

之后首次沉淀一条记忆：把最近一个 bug 的原因/影响范围/修复方案写成一条 pitfall（模板见 `docs/memory/README.md`）。

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
├── install.sh                  # 一键接入脚本
├── templates/
│   ├── memory-README.md        # → 目标仓库 docs/memory/README.md
│   ├── claude-md-section.md    # → 追加进 CLAUDE.md 的段落
│   ├── agents-md-section.md    # → 追加进 AGENTS.md 的段落
│   └── pitfall-entry.md        # 单条 pitfall 模板
└── commands/
    └── memory-check.md         # → .claude/commands/memory-check.md
```

## License

MIT
