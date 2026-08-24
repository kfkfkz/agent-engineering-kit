## 项目记忆（坑与流程）

历史踩坑与流程经验沉淀在 [docs/memory/](docs/memory/README.md)：

- 修改代码前先查 `docs/memory/pitfalls/`，命中相关条目必须先读全文；状态为「待验证」的条目不得作为修改依据
- bug 修复人工审核通过后，将原因/影响范围/修复方案整理为新 pitfall 条目入库
- 环境操作、测试数据、可执行测试流程放 `docs/memory/playbooks/`
- 功能验收通过后，按 `docs/memory/README.md` 的功能蒸馏清单沉淀核心流程与环境信息（跨 Agent 记忆区）
- 记忆触发：用户说"记一下/沉淀记忆"等，或任务收尾（功能验收/bug 修复完成）时，执行记忆捕获（`/memory-capture`）：提取候选 → 起草 → 用户确认 → 入库 docs/memory
