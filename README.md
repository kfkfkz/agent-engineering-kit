# repo-memory-kit

仓库级跨 Agent 记忆系统——让 Claude Code、Codex 及任何读取 `CLAUDE.md` / `AGENTS.md` 的 AI Agent 在同一套项目记忆上工作。

**git 版本化的 Markdown 记忆 + 结构化代码图谱锚点，不引入任何新服务。**

## 解决什么问题

多工具/多模型的工作流里，经验是锁死的：每个 Agent 各学各的坑（Claude Code 踩过的，Codex 从零再踩一遍），调试发现随会话消失，环境操作步骤散落在个人笔记里无法交接。而集中式记忆服务（向量库、记忆平台）对多数团队是过度设计。

本套件的答案：把经验沉淀做成和代码一样的**工程资产**——版本化、白盒、可审计、随仓库走、双端 Agent 原生可读。

## 架构总览

```text
写入 ──┐  触发：口头（"记一下"）· 任务收尾 · bug 修复 · 功能蒸馏
       │  流程：候选提取 → 分类起草 → 用户确认 → 入库    （/memory-capture）
       ▼
分层 ──┤  L3  PROFILE.md      项目经验画像（风险域/稳定约定/反模式，条目 ≥10 时建立）
       │  L2  playbooks/      场景流程：某功能/环境怎么跑通
       │  L1  pitfalls/       原子坑：单个缺陷/陷阱的根因与修复
       │  L0  权威文档+代码    证据：spec / 接口文档 / 源码
       │  （上层只存结论与指针，下钻链必须闭合到证据）
       ▼
保鲜 ──┤  全量巡检（每迭代）：校验点比对代码 → 矫正漂移 → 压缩超限 → 刷新锚点表
       │  增量巡检（改码前）：查锚点表，只核验受波及条目    （/memory-check <符号>）
       │  生命周期：待验证 → 已确认 → deprecated（须人工确认）
       ▼
联动 ──┘  .anchors.json   符号→条目精确映射（unresolved = 漂移/跨仓库信号）
          .cbmignore 否定规则：记忆条目纳入代码图谱索引，与代码/规格同图可检索
```

**规则的唯一来源**是目标仓库的 `docs/memory/README.md`（由 `templates/memory-README.md` 安装）；命令与技能文件只做指针。本文件只讲系统全貌。

## 快速接入

```bash
git clone <this-repo> && cd repo-memory-kit
./install.sh /path/to/your-project
```

install.sh 幂等，做四件事：创建 `docs/memory/{pitfalls,playbooks}/`，安装规则与用户索引（**规则 RULES.md 由 kit 管辖自动升级，README.md 索引归用户所有绝不覆盖**）；安装双端技能到**仓库级**目录（Claude → `.claude/skills/`，Codex → `.agents/skills/`，两端同一份 SKILL.md，随仓库版本化、零跨项目污染）；向 `CLAUDE.md` / `AGENTS.md` 追加「项目记忆」指引段落（已有则跳过）；写入 `.cbmignore` 托管区块（带 start/end 标记，整体校验替换；逐层否定 `!docs/` → `docs/*` → `!docs/memory/` 规避父子裁剪）。

另有 `validate-memory.sh <目标仓库>`：校验记忆区结构完整性与 `.anchors.json` 协议（schema_version / 排序 / 去重）；`tests/install.test.sh` 覆盖安装器全部场景（幂等、空格路径、残缺配置、用户文件保护），CI 在每次推送时运行（sh -n + ShellCheck + 测试）。

**前置依赖**：巡检默认强制依赖结构化代码检索工具（代码知识图谱类，参考实现 [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)，MIT）。grep 有三个结构性盲区，不能作为兜底：路由前缀配置在 context-path（字面量搜不到）、符号改名后关键字静默失效、无索引覆盖信号无法判断结果可信度。工具不可用时巡检默认终止，用户显式要求方可降级（报告须标注"置信度低"）。

**升级**：`./install.sh --update <目标仓库>`。文件所有权约定：

| 归 kit 管辖（自动刷新） | 归用户所有（只提示差异，不覆盖） |
| --- | --- |
| `docs/memory/RULES.md`（全部规则）、双端技能（`.claude/skills` + `.agents/skills`）、条目/画像模板、`.cbmignore` 托管区块 | `docs/memory/README.md`（索引与巡检记录）、指令文件段落、全部记忆条目、`.anchors.json` |

## 日常使用

| 场景 | 动作 |
| --- | --- |
| 想沉淀经验 | 说"记一下"，或功能验收后确认 Agent 的提议 → 候选列表 → 确认入库 |
| 改代码前 | `/memory-check <类名>`：查锚点表看本次变更波及哪些记忆条目 |
| 每迭代一次 | `/memory-check`：全量巡检——矫正漂移、压缩超限、刷新锚点表 |
| 动手排障前 | 查 `docs/memory/README.md` 索引，命中条目必须先读全文 |

## 设计原则

1. **人类把关**：条目经确认才入库，`deprecated` 须人工确认——LLM 蒸馏的错误教训会静默毒化后续所有输出。
2. **单一口径**：规则只写在 `docs/memory/README.md`，命令文件是指针；上层不复制下层内容。
3. **过期记忆比没有记忆更危险**：校验点比对（条目级）→ 巡检矫正（批次级）→ 锚点表 unresolved（符号级）三层防漂移。
4. **凭证永不入库**：token / 密钥 / 内网地址存指针不存值，注明获取途径。
5. **门槛驱动升级**：L3 要 10 条起步、标签路由要 50 条、向量检索要 200 条——每级升级由可测量信号触发，不由想象触发。

## 演进路线

当前是人工阶段，但条目格式（结论 + 指针 + 校验点）刻意与自动蒸馏产物对齐，平移时零重整理：

| 方向 | 内容 | 触发信号 |
| --- | --- | --- |
| 路线 A：记忆服务 | 自动捕获 + L2/L3 蒸馏管线 + 混合检索（BM25+向量+RRF） | 条目产量超过人工整理能力 |
| 路线 B：图谱原生边 | fork 图谱工具（Go，MIT）加 CITES 边 + detect_changes 联动 | 锚点表撞到精度/联动天花板 |
| 检索升级 | 标签路由（50 条）→ sqlite-vec 文件级嵌入（200 条）→ 混合检索 | 对应条目数，或索引漏检 ≥ 2 次 |

三条演进互相独立，各自等信号，不提前建设。

## 目录结构

```text
repo-memory-kit/
├── install.sh                  # 接入/更新脚本（--update）
├── validate-memory.sh          # 记忆区结构与 .anchors.json 协议校验器
├── skills/                     # 双端技能唯一来源（安装到 .claude/skills 与 .agents/skills）
│   ├── memory-check/SKILL.md   # 巡检（全量/增量）
│   └── memory-capture/SKILL.md # 捕获/蒸馏
├── templates/
│   ├── memory-RULES.md         # → docs/memory/RULES.md（规则，kit 管辖）
│   ├── memory-README.md        # → docs/memory/README.md（用户索引，仅首次创建）
│   ├── profile.md              # → docs/memory/_PROFILE_TEMPLATE.md（L3 模板）
│   ├── claude-md-section.md    # → 追加进 CLAUDE.md 的段落
│   ├── agents-md-section.md    # → 追加进 AGENTS.md 的段落
│   └── pitfall-entry.md        # → docs/memory/pitfalls/_TEMPLATE.md
├── tests/install.test.sh       # 安装器测试（8 组场景，25 断言）
└── .github/workflows/ci.yml    # sh -n + ShellCheck + 测试
```

## License

MIT
