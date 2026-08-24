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
3. **可矫正**：每个条目带「校验点」（可机器核验的事实清单），配合巡检防止记忆停留在过去

## 前置依赖

巡检（Claude Code `/memory-check` 与 Codex `memory-check` 技能）**默认强制依赖结构化代码检索工具**：代码知识图谱类（如 codebase-memory MCP），或提供符号级定位、调用关系、索引覆盖/新鲜度信号的等效 codegraph 工具。规则条文以 `templates/memory-README.md` 为唯一来源，此处只讲设计依据——为什么 grep 不能作为兜底：

1. 服务路由前缀通常配置在 application 配置文件的 `context-path`——字面量搜索直接漏掉
2. 类/方法改名后，记忆条目里的旧关键字 grep 不到——**静默漏检比误报更危险**
3. 没有索引覆盖信号，无法区分"搜不到"是"不存在"还是"搜法不对"

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
| 口头触发（"记一下/沉淀记忆"） | 记忆捕获（`/memory-capture`）：候选提取 → 起草 → 用户确认 → 入库 |
| 任务收尾（验收/修复完成时） | 主动提议本次值得沉淀的经验，走同一捕获流程 |

### 生命周期（记忆怎么管）

```text
待验证 ──(人工确认)──▶ 已确认 ──(失效)──▶ deprecated（标记，不删除）
```

### 分层与下钻（吸收自 L0-L3 分层架构）

```text
L3 PROFILE.md（项目经验画像）→ L2 playbooks（场景流程）→ L1 pitfalls（原子坑）→ L0 权威文档/代码（证据）
```

- L3 是宏观引导层（风险域地图/稳定约定/反模式），**条目 ≥ 10 时才建立**——上层只存结论与指针，下钻链闭合
- 刻意不吸收：短期会话压缩、自动 L0 捕获、向量检索——那些属于自动蒸馏阶段，人工体系引入只增维护成本

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
| L3 PROFILE 手工蒸馏 | L2/L3 自动蒸馏管线 |
| 用后即校 + 人工确认 | 人工审核后才升为共享资产 |

当坑条目的产量超过人工整理能力时，这些结构化条目就是平移到记忆服务的种子数据——格式不用重整理。

### 检索演进阶梯（何时引入向量检索）

| 档位 | 触发信号 | 方案 |
| --- | --- | --- |
| 现在 | L1/L2 条目 < 50 | README 索引 + L3 风险域路由 + grep（校验点即结构化锚点） |
| 第一档 | 条目 ≥ 50，或出现 ≥ 2 次"查了索引但漏了相关条目" | 条目 frontmatter 加标签（域/模块/类型），标签路由，仍 git 原生 |
| 第二档 | 条目 ≥ 200，或开始接入自动捕获 | sqlite-vec 文件级嵌入索引，巡检时顺带重建，仍是文件不是服务 |
| 第三档 | 平移到记忆服务（阶段二） | 真正的向量库 + BM25 + RRF 混合检索，随蒸馏管线一起上 |

原则：每档只在实际触发信号后升级，不提前建设；向量库与自动蒸馏管线天然耦合，不单独引入。

### 路线 B：记忆进代码图谱（codebase 扩充）

与接入记忆服务（路线 A）并行的演进方向：**不新增服务，把记忆条目纳入代码知识图谱**（前提：所用图谱工具可扩展——开源可贡献或自有代码）。

| 步骤 | 内容 | 状态 |
| --- | --- | --- |
| 1 | 记忆文件入索引：markdown 原生可被图谱索引，但 `docs/` 属启发式跳过层，需 `.cbmignore` 否定规则解除（install.sh 自动写入；实测 codebase-memory-mcp 有效，记忆条目成为可检索的一等图节点） | ✅ 已验证 |
| 2 | "决定"类记忆走图谱工具自带的 ADR 通道持久化 | 部分工具已支持 |
| 3 | 扩展记忆节点类型与 CITES 边（校验点 → 代码符号），detect_changes 影响分析覆盖记忆条目——**巡检从定期全量变成事件驱动增量** | 阶段二 |

参考实现：[codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)（MIT，`.cbmignore` 支持 gitignore 语法与 `!` 否定，注意父子裁剪——需逐层否定：`!docs/` → `docs/*` → `!docs/memory/`）。

与路线 A 不互斥：图谱化解决"记忆与代码的联动"，记忆服务解决"自动捕获与蒸馏"。

## 目录结构

```text
repo-memory-kit/
├── install.sh                  # 接入/更新脚本（--update）
├── templates/
│   ├── memory-README.md        # → 目标仓库 docs/memory/README.md
│   ├── profile.md              # → docs/memory/PROFILE.md（L3，条目 ≥10 时创建）
│   ├── claude-md-section.md    # → 追加进 CLAUDE.md 的段落
│   ├── agents-md-section.md    # → 追加进 AGENTS.md 的段落
│   └── pitfall-entry.md        # 单条 pitfall 模板
├── commands/
│   ├── memory-check.md         # → .claude/commands/（Claude Code 巡检）
│   └── memory-capture.md       # → .claude/commands/（Claude Code 捕获/蒸馏）
└── codex/skills/
    ├── memory-check/SKILL.md   # → ~/.codex/skills/（Codex 巡检）
    └── memory-capture/SKILL.md # → ~/.codex/skills/（Codex 捕获/蒸馏）
```

## License

MIT
