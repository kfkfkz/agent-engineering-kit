## 项目记忆（坑与流程）

历史踩坑与流程经验沉淀在 [docs/memory/](docs/memory/)——规则见 [RULES.md](docs/memory/RULES.md)（kit 管辖），索引见 [README.md](docs/memory/README.md)：

- 非平凡的功能实现、缺陷修复、重构或迁移，使用 `repo-delivery` 技能先做低成本 Route Card 分流，再按 Direct / Bounded / Standard / Initiative 最轻安全路线提取适用门禁；只有正向证据才升径，并遵循 `execution_profile` 限制上下文、设计与验证深度；复用项目既有 spec/SDD，不另建平行文档体系
- 显式启动：Claude Code 使用 `/repo-delivery [任务描述]`；Codex 使用 `$repo-delivery [任务描述]`。自然语言请求仍可自动触发
- 代码结构、调用链和影响分析优先使用 `codebase-memory`；图谱不可用时必须披露降级及置信度
- Bug、测试失败或异常行为先使用 `systematic-debugging` 建立根因链，再进入修复；不得用试错补丁替代诊断
- 可观察行为默认使用 `tdd` 逐个纵向切片实现；难测、过度 mock 或散弹修改应作为架构信号回到设计链
- 新依赖、外部集成、通用组件或关键选型先用 `reuse-research`，形成采用/扩展/组合/自建的证据化决策
- 认证授权、外部输入、敏感操作、依赖或 Agent 配置变更使用 `security-review`；高危结论必须有利用路径并做对抗式复核
- 设计定稿由 `design-pipeline` 负责；代码交付用 `delivery-gate`，收口前用 `route-eval` 对照 Route Card 检查最低路线与最终 diff 漂移。验证结论以治理 profile 要求的持久回执为证，无回执不得声称验证通过
- 已有 `.specify/specs` 先用 `spec-migrate` 检查；只有明确要求时才增量迁移，待核验标记不等于设计完成
- 项目建有 `docs/03-SDD/` 时，功能的设计交付物（业务流程设计/详细设计/API/验收）以其版本目录为载体
- 任务中断或跨 Agent/会话移交时使用 `task-handoff`，以当前源码和权威 spec 复核交接内容
- 任务动手前先用 `.repo-memory-kit/bin/memory-recall "<任务描述>"` 语义检索 docs/ 全量（坑/决定/场景/域条目/SDD），命中条目读全文；索引落后于文档时 `--rebuild` 重建
- 会话开始先读 `docs/memory/PROFILE.md`（L3 画像，存在时），再查 `README.md` 索引
- 修改代码前先查 `docs/memory/pitfalls/`，命中相关条目必须先读全文；状态为「待验证」的条目不得作为修改依据
- bug 修复人工审核通过后，将原因/影响范围/修复方案整理为新 pitfall 条目入库
- 环境操作、测试数据、可执行测试流程放 `docs/memory/playbooks/`
- 功能验收通过后，按 RULES.md 的功能蒸馏清单沉淀核心流程与环境信息（跨 Agent 记忆区）
- 记忆触发：用户说"记一下/沉淀记忆"等，或任务收尾（功能验收/bug 修复完成）时，执行记忆捕获（memory-capture 技能）：提取候选 → 起草 → 用户确认 → 入库 docs/memory
