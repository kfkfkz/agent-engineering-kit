# repo-memory-kit

仓库级跨 Agent 记忆系统——让 Claude Code、Codex 及任何读取 `CLAUDE.md` / `AGENTS.md` 的 AI Agent 在同一套项目记忆上工作。

**版本化的 Markdown 记忆 + 代码图谱锚点，不引入任何新服务。**

## 解决什么问题

多工具/多模型的工作流里，经验是锁死的：每个 Agent 各学各的坑，调试发现随会话消失，环境操作步骤散落在个人笔记里无法交接。集中式记忆服务（向量库、记忆平台）对多数团队又是过度设计。

本套件把经验沉淀做成和代码一样的**工程资产**：版本化、白盒、随仓库走、双端 Agent 原生可读。

## 架构

```text
写入 ──┐  触发：口头（"记一下"）· 任务收尾 · bug 修复 · 功能蒸馏
       │  流程：候选提取 → 起草（frontmatter）→ 用户确认 → 入库 → memory-build
       ▼
分层 ──┤  L3  PROFILE.md              项目经验画像（confirmed 有效条目 ≥10 时建立，会话开始先读）
       │  L2  playbooks/              场景流程：怎么跑通（含验证与回滚）
       │  L1  pitfalls/ + decisions/  原子经验：坑与决定
       │  L0  权威文档+代码            证据
       │  （上层只存结论与指针，下钻链必须闭合到证据）
       ▼
保鲜 ──┤  全量巡检（每迭代）· 增量巡检（改码前查波及条目）
       │  生命周期：unverified → confirmed → deprecated / superseded
       ▼
联动 ──┘  条目 frontmatter 是唯一事实源；README 索引与锚点表由 memory-build 生成
```

规则的唯一来源是目标仓库的 `docs/memory/RULES.md`（kit 管辖，`--update` 自动升级）；技能文件只做指针。

## 接入

```bash
git clone <this-repo> && cd repo-memory-kit
./install.sh /path/to/your-project    # 幂等；--update 升级；--uninstall 卸载
```

一条命令完成：创建记忆区（`docs/memory/{pitfalls,decisions,playbooks}/`）、安装双端技能到仓库级目录（`.claude/skills/` + `.agents/skills/`）、向 `CLAUDE.md`/`AGENTS.md`/`.cbmignore` 写入托管区块、安装校验器与生成器到 `.repo-memory-kit/`。**所有清理与卸载只删内容指纹匹配 kit 产物的文件——用户数据（条目、索引、锚点表）绝不触碰。**

**前置依赖**：巡检强制依赖结构化代码检索工具（代码知识图谱类，参考实现 [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)）。grep 有三个结构性盲区不能兜底：路由前缀配置在 context-path、符号改名后静默失效、无索引覆盖信号。工具不可用默认终止巡检。

**非 git 仓库（SVN / 仅本地）**：记忆区建议单独 `git init` 推私有仓库备份；变更影响分析的 `--files` 模式对任何 VCS 都可用。

## 日常使用

| 场景 | 动作 |
| --- | --- |
| 会话开始 | 读 `PROFILE.md`（存在时）→ 查 `README.md` 索引（自动生成） |
| 沉淀经验 | 说"记一下"，或功能验收后确认提议 → 条目入库，索引与锚点自动刷新 |
| 改代码前 | `memory-build --changed`（自动探测 git/svn）；符号级精度走 `/memory-check <类名>` |
| 合入前 / PR | `<变更命令> \| memory-build --files <仓库> --report impact.md`；`--strict` 供 CI 阻断 |
| 每迭代 | `/memory-check` 全量巡检；CI 跑 `.repo-memory-kit/bin/validate-memory.sh .` |

## 设计原则

1. **人类把关**：条目经确认才入库，状态降级须人工确认——LLM 蒸馏的错误教训会静默毒化后续输出。
2. **单一口径**：规则只在 RULES.md，技能是指针，索引与锚点表是生成物。
3. **过期记忆比没有记忆更危险**：校验点、巡检、锚点表、校验器四层防漂移。
4. **凭证永不入库**：存指针不存值；误入库走应急流程（删除 + 清历史 + 轮换）。
5. **门槛驱动升级**：L3 要 10 条有效 confirmed 记忆（`memory-build` 自动统计并提示）、标签路由要 50 条、向量检索要 200 条——由可测量信号触发，不由想象触发。

## 目录结构

```text
repo-memory-kit/
├── install.sh          # 接入 / --update / --uninstall
├── memory-build        # 生成索引与锚点、校验 frontmatter、变更影响与报告
├── validate-memory.sh  # 记忆区校验（CI 入口）
├── skills/             # 双端技能（巡检 / 捕获）
├── templates/          # 规则、索引、四类条目模板、指令区块
└── tests/              # 安装器与生成器测试（CI 运行）
```

## License

MIT
