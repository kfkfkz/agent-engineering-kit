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
       │  L1  pitfalls/ + decisions/   原子经验：坑（根因/修复）与决定（背景/取舍/后果）
       │  L0  权威文档+代码    证据：spec / 接口文档 / 源码
       │  （上层只存结论与指针，下钻链必须闭合到证据；PROFILE 存在时会话开始先读）
       ▼
保鲜 ──┤  全量巡检（每迭代）：校验点比对代码 → 矫正漂移 → 压缩超限 → 刷新锚点表
       │  增量巡检（改码前）：查锚点表，只核验受波及条目    （/memory-check <符号>）
       │  生命周期：待验证 → 已确认 → deprecated（须人工确认）
       ▼
联动 ──┘  .anchors.json   符号→条目精确映射（unresolved = 漂移/跨仓库信号）
          .cbmignore 否定规则：记忆条目纳入代码图谱索引，与代码/规格同图可检索
```

**规则的唯一来源**是目标仓库的 `docs/memory/RULES.md`（kit 管辖，`--update` 自动升级；`README.md` 是用户索引）；技能文件只做指针。本文件只讲系统全貌。

## 快速接入

```bash
git clone <this-repo> && cd repo-memory-kit
./install.sh /path/to/your-project
```

install.sh 幂等，做四件事：创建 `docs/memory/{pitfalls,decisions,playbooks}/`，安装规则与用户索引（**规则 RULES.md 由 kit 管辖自动升级，README.md 索引归用户所有绝不覆盖**）；安装双端技能到**仓库级**目录（Claude → `.claude/skills/`，Codex → `.agents/skills/`，两端同一份 SKILL.md，随仓库版本化、零跨项目污染）；向 `CLAUDE.md` / `AGENTS.md` 写入**托管区块**（带标记自动刷新；旧版段落内容匹配 kit 历史版本时自动迁移，被手工定制的内容只提示不动）；写入 `.cbmignore` 托管区块（带 start/end 标记，整体校验替换；逐层否定 `!docs/` → `docs/*` → `!docs/memory/` 规避父子裁剪）。

**安全约定**：所有清理（旧全局技能、旧命令文件）只删除"内容指纹匹配 kit 当前或历史版本"的文件——任何与 kit 产物不一致的文件一律保留并提示，杜绝误删用户定制内容。安装清单 `.repo-memory-kit/manifest` 记录 kit 版本与受管文件哈希，支持漂移检测与 `--uninstall`（只清 kit 产物，不触碰用户数据）。

另有 `validate-memory.sh`（随仓库安装到 `.repo-memory-kit/bin/`，可直接接入目标仓库 CI）：校验记忆区结构、`.anchors.json` 协议（schema_version=2、类型化键、排序去重、路径防逃逸）、README 索引与实际条目双向一致、条目语义（状态合法、playbook 含「最后有效」日期）。`tests/install.test.sh` 覆盖安装器全部场景（幂等、空格路径、残缺配置、用户文件保护、安全清理、段落迁移、卸载），CI 在每次推送时运行。

**前置依赖**：巡检默认强制依赖结构化代码检索工具（代码知识图谱类，参考实现 [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)，MIT）。grep 有三个结构性盲区，不能作为兜底：路由前缀配置在 context-path（字面量搜不到）、符号改名后关键字静默失效、无索引覆盖信号无法判断结果可信度。工具不可用时巡检默认终止，用户显式要求方可降级（报告须标注"置信度低"）。

**升级**：`./install.sh --update <目标仓库>`。文件所有权约定：

| 归 kit 管辖（自动刷新） | 归用户所有（只提示差异，不覆盖） |
| --- | --- |
| `docs/memory/RULES.md`（全部规则）、双端技能、四类条目模板、校验器、`CLAUDE.md`/`AGENTS.md` 托管区块、`.cbmignore` 托管区块、安装清单 | `docs/memory/README.md`（索引与巡检记录）、全部记忆条目、`.anchors.json` |

## 日常使用

| 场景 | 动作 |
| --- | --- |
| 想沉淀经验 | 说"记一下"，或功能验收后确认 Agent 的提议 → 候选列表 → 确认入库 |
| 改代码前 | `/memory-check <类名>`：查锚点表看本次变更波及哪些记忆条目 |
| 每迭代一次 | `/memory-check`：全量巡检——矫正漂移、压缩超限、刷新锚点表 |
| 动手排障前 | 查 `docs/memory/README.md` 索引，命中条目必须先读全文 |

## 设计原则

1. **人类把关**：条目经确认才入库，`deprecated` 须人工确认——LLM 蒸馏的错误教训会静默毒化后续所有输出。
2. **单一口径**：规则只写在 `docs/memory/RULES.md`，技能文件是指针；上层不复制下层内容。
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

### v3 规划（已评审，待实施）

| 功能 | 内容 | 为什么 |
| --- | --- | --- |
| 条目 frontmatter 单一事实源 | 状态/日期/模块/锚点/验证证据进条目 frontmatter，`memory-build` 自动生成 README 索引与 `.anchors.json` | 消灭"条目/索引/锚点"三处人工同步——长期必然漂移 |
| 自动变更影响巡检 | `/memory-check --changed`（读 git diff）与 `--since <ref>`，图谱影响分析定位波及条目，PR 报告模式 | 用户经常只知道"改了哪些文件"，不知道完整符号 |
| 冲突与替代关系 | `superseded` 状态与 `supersedes/conflicts_with/related/verified_at/verified_commit/evidence` 字段 | 结论冲突、重复记录、新旧替代目前无表达 |

跨仓库锚点已先行落地（锚点协议 v2 的 `repo` 字段，前端条目引用后端接口即此用例）。

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
│   ├── pitfall-entry.md        # → pitfalls/_TEMPLATE.md
│   ├── decision-entry.md       # → decisions/_TEMPLATE.md
│   ├── playbook-entry.md       # → playbooks/_TEMPLATE.md
│   ├── profile.md              # → _PROFILE_TEMPLATE.md（L3 模板）
│   ├── claude-md-section.md    # → CLAUDE.md 托管区块
│   └── agents-md-section.md    # → AGENTS.md 托管区块
├── tests/install.test.sh       # 安装器测试（14 组场景，40+ 断言，临时 HOME 隔离）
└── .github/workflows/ci.yml    # sh -n + ShellCheck + 测试
```

## License

MIT
