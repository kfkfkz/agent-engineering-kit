## 项目记忆（坑与流程）

历史踩坑与流程经验沉淀在 [docs/memory/](docs/memory/)——规则见 [RULES.md](docs/memory/RULES.md)（kit 管辖），索引见 [README.md](docs/memory/README.md)：

- 非平凡的功能实现、缺陷修复、重构或迁移，使用 `repo-delivery` 技能先提取项目宪章门禁，再做入链判定与端到端收口；复用项目既有 spec/SDD，不另建平行文档体系
- 显式启动：Claude Code 使用 `/repo-delivery [任务描述]`；Codex 使用 `$repo-delivery [任务描述]`。自然语言请求仍可自动触发
- 代码结构、调用链和影响分析优先使用 `codebase-memory`；图谱不可用时必须披露降级及置信度
- Bug、测试失败或异常行为先使用 `systematic-debugging` 建立根因链，再进入修复；不得用试错补丁替代诊断
- 可观察行为默认使用 `tdd` 逐个纵向切片实现；难测、过度 mock 或散弹修改应作为架构信号回到设计链
- 会话开始先读 `docs/memory/PROFILE.md`（L3 画像，存在时），再查 `README.md` 索引
- 修改代码前先查 `docs/memory/pitfalls/`，命中相关条目必须先读全文；状态为「待验证」的条目不得作为修改依据
- bug 修复人工审核通过后，将原因/影响范围/修复方案整理为新 pitfall 条目入库
- 环境操作、测试数据、可执行测试流程放 `docs/memory/playbooks/`
- 功能验收通过后，按 RULES.md 的功能蒸馏清单沉淀核心流程与环境信息（跨 Agent 记忆区）
- 记忆触发：用户说"记一下/沉淀记忆"等，或任务收尾（功能验收/bug 修复完成）时，执行记忆捕获（memory-capture 技能）：提取候选 → 起草 → 用户确认 → 入库 docs/memory
