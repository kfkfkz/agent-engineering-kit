# repo-memory-kit

仓库级跨 Agent 记忆系统——让 Claude Code、Codex 及任何读取 `CLAUDE.md` / `AGENTS.md` 的 AI Agent 在同一套项目记忆上工作。

**git 版本化的 Markdown 记忆 + 结构化代码图谱锚点，不引入任何新服务。**

## 解决什么问题

多工具/多模型的工作流里，经验是锁死的：每个 Agent 各学各的坑（Claude Code 踩过的，Codex 从零再踩一遍），调试发现随会话消失，环境操作步骤散落在个人笔记里无法交接。而集中式记忆服务（向量库、记忆平台）对多数团队是过度设计。

本套件的答案：把经验沉淀做成和代码一样的**工程资产**——版本化、白盒、可审计、随仓库走、双端 Agent 原生可读。

## 架构总览

```text
写入 ──┐  触发：口头（"记一下"）· 任务收尾 · bug 修复 · 功能蒸馏
       │  流程：候选提取 → 分类起草（frontmatter）→ 用户确认 → 入库 → memory-build
       ▼
分层 ──┤  L3  PROFILE.md              项目经验画像（风险域/稳定约定/反模式，条目 ≥10 时建立，会话开始先读）
       │  L2  playbooks/              场景流程：某功能/环境怎么跑通（含验证/回滚）
       │  L1  pitfalls/ + decisions/  原子经验：坑（根因/修复）与决定（背景/取舍/后果）
       │  L0  权威文档+代码            证据：spec / 接口文档 / 源码
       │  （上层只存结论与指针，下钻链必须闭合到证据）
       ▼
保鲜 ──┤  全量巡检（每迭代）：校验点比对代码 → 矫正漂移 → 压缩超限 → 刷新锚点表
       │  增量巡检（改码前）：查锚点表，只核验受波及条目    （/memory-check <符号>）
       │  生命周期：待验证 → 已确认 → deprecated（须人工确认）
       ▼
联动 ──┘  .anchors.json   类型化定位符 → 条目精确映射（memory-build 从条目 frontmatter 自动生成；
                          repo@ 跨仓库锚点；unresolved = 漂移信号）
          .cbmignore 托管区块：记忆条目纳入代码图谱索引，与代码/规格同图可检索
```

**规则的唯一来源**是目标仓库的 `docs/memory/RULES.md`（kit 管辖，`--update` 自动升级；`README.md` 是用户索引）；技能文件只做指针。本文件只讲系统全貌。

## 快速接入

```bash
git clone <this-repo> && cd repo-memory-kit
./install.sh /path/to/your-project          # 首次接入（幂等）
./install.sh --update <目标仓库>             # 升级 kit 管辖文件
./install.sh --uninstall <目标仓库>          # 卸载（只清 kit 产物，不碰用户数据）
```

install.sh 在目标仓库安装：

| 安装物 | 位置 | 所有权 |
| --- | --- | --- |
| 规则 RULES.md、用户索引 README.md、四类条目模板 | `docs/memory/{pitfalls,decisions,playbooks}/` | RULES 与模板 kit 管辖；条目归用户；README 索引区块由 memory-build 生成 |
| 双端技能（同一份 SKILL.md，仓库级零跨项目污染） | `.claude/skills/` + `.agents/skills/` | kit 管辖 |
| 「项目记忆」指引区块（旧版段落自动迁移，定制内容只提示） | `CLAUDE.md` / `AGENTS.md` 托管区块 | kit 管辖 |
| 图谱索引否定规则（逐层否定规避父子裁剪） | `.cbmignore` 托管区块 | kit 管辖 |
| 校验器 + 生成器 memory-build + 安装清单 | `.repo-memory-kit/` | kit 管辖 |

**前置依赖**：巡检默认强制依赖结构化代码检索工具（代码知识图谱类，参考实现 [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)，MIT）。grep 有三个结构性盲区，不能作为兜底：路由前缀配置在 context-path（字面量搜不到）、符号改名后关键字静默失效、无索引覆盖信号无法判断结果可信度。工具不可用时巡检默认终止，用户显式要求方可降级（报告须标注"置信度低"）。

**非 git 仓库（SVN / 仅本地）**：记忆区无法随代码版本化，git 系自动巡检（`--changed` / detect_changes）不可用。建议 `docs/memory/` 单独 `git init` 推私有仓库备份，主仓库 ignore 该目录即可。

## 安全与可信

- **指纹删除**：所有清理（旧全局技能、旧命令、卸载）只删"内容指纹匹配 kit 当前或 git 历史版本"的文件——与 kit 产物不一致的一律保留并提示；历史指纹经 `git cat-file -e` 守卫，杜绝空文件误判。
- **卸载三重防线**：路径白名单（拒绝绝对路径 / `..` / 清单外路径）+ 删除前指纹比对（漂移内容保留）+ 安装清单（`.repo-memory-kit/manifest`，kit 版本与受管文件哈希，覆盖前漂移提示）。
- **校验器** `validate-memory.sh` + **生成器** `memory-build`（均随仓库安装，可直接接入目标仓库 CI）：条目 frontmatter 校验（状态枚举/真实日期/类型化锚点/关系引用/supersedes 双向一致）、README 索引与 `.anchors.json` 与条目一致性（生成物被手改即报过期）。需要 python3——缺解析器明确失败，不做"假绿"跳过；哈希兼容 sha256sum / shasum（macOS）。
- **测试与 CI**：`tests/install.test.sh`（52 断言：幂等、空格路径、残缺配置、用户文件保护、安全清理、段落迁移、卸载保护、篡改清单，临时 HOME 隔离）+ `tests/build.test.sh`（24 断言：frontmatter 校验、索引/锚点生成、--check 新鲜度、--migrate 迁移、--changed/--files 影响、--report 报告、--strict 退出码、supersedes 一致性）；GitHub Actions 每次 push 运行 sh -n + ShellCheck + 两套测试。

## 日常使用

| 场景 | 动作 |
| --- | --- |
| 会话开始 | 读 `PROFILE.md`（存在时）→ 查 `README.md` 索引（自动生成） |
| 想沉淀经验 | 说"记一下"，或功能验收后确认 Agent 的提议 → 候选列表 → 确认入库（Agent 自动跑 memory-build） |
| 改代码前（知道符号） | `/memory-check <类名>`：查锚点表看波及条目 |
| 改代码前（只知道文件） | `memory-build --changed [ref]` / `--since <ref>`（自动探测 git/svn）→ 波及条目 |
| 每迭代一次 | `/memory-check`：全量巡检——矫正漂移、压缩超限、刷新锚点表 |
| 条目增删改后 | `memory-build`：刷新索引与锚点表（生成物，勿手改） |
| 合入前 / PR / CI | `<VCS 变更命令> \| memory-build --files <仓库> --report impact.md`：Markdown 影响报告（VCS 无关；`--strict` 有波及条目时退出码 2 供阻断） |
| 目标仓库 CI | `.repo-memory-kit/bin/validate-memory.sh .` 校验记忆区完整性 |

## 设计原则

1. **人类把关**：条目经确认才入库，`deprecated` 须人工确认——LLM 蒸馏的错误教训会静默毒化后续所有输出。
2. **单一口径**：规则只写在 `docs/memory/RULES.md`，技能文件是指针；上层不复制下层内容。
3. **过期记忆比没有记忆更危险**：校验点比对（条目级）→ 巡检矫正（批次级）→ 锚点表 unresolved（符号级）→ 校验器（提交级）四层防漂移。
4. **凭证永不入库**：token / 密钥 / 内网地址存指针不存值；误入库有应急流程（删除 + 清历史 + 轮换凭证）。
5. **门槛驱动升级**：L3 要 10 条起步、标签路由要 50 条、向量检索要 200 条——每级升级由可测量信号触发，不由想象触发。

## 已实现

PR 记忆影响报告已实现（VCS 无关）：`--files` 接收任意来源的变更清单（stdin），`--report` 输出 Markdown 影响报告（波及条目/状态/最近验证/命中锚点/跨仓库待核验），`--strict` 供 CI 阻断（默认仅 warning）。`--changed` 自动探测 git（diff+未跟踪）/svn（status / diff --summarize）。

已实现（原 v3 三件）：条目 frontmatter 单一事实源 + `memory-build` 自动生成索引与锚点表；`--changed`/`--since` 自动变更影响巡检（文件级启发式，方法级精度走图谱增量巡检）；`superseded` 状态与 `supersedes/conflicts_with/related/verified/verified_commit/evidence` 冲突与替代字段。跨仓库锚点见协议 `repo@` 键前缀。

## 目录结构

```text
repo-memory-kit/
├── install.sh                  # 接入/更新/卸载（指纹安全清理）
├── memory-build                # 索引/锚点生成器 + frontmatter 校验 + 变更影响（随仓库安装）
├── validate-memory.sh          # 记忆区校验器（随仓库安装，委托 memory-build --check）
├── skills/                     # 双端技能唯一来源
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
├── tests/
│   ├── install.test.sh         # 52 断言（临时 HOME 隔离）
│   └── build.test.sh           # 24 断言（frontmatter/生成/迁移/变更影响/报告）
└── .github/workflows/ci.yml    # sh -n + ShellCheck + 两套测试
```

## License

MIT
