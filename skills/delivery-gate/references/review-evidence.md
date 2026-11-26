# 代码评审取证规范

代码评审按 `aek.application.review_evidence.plan_code_review_evidence` 的语义组织证据；宿主执行 MCP，
AEK 只约束顺序与回执。同一评审阶段复用与当前问题、范围和来源身份相符的证据。新增或变动路径补证，独立 Reviewer 仍核对本轮结论。

1. 先以 `stage=closeout`、`purpose=review_required`、当前 Route、WorkUnit/session、最终 diff 路径和
   验收目标按 `repo-delivery` 的紧凑取证约定调用一次预算化 `memory_recall`，只展开相关决定、坑点和旧回执。MCP 成功后不得再跑 CLI
   或 grep 扫记忆；无命中表示未知，不表示没有约束。只有调用开始前不可用、不兼容或失败，才按
   MCP → CLI → 有界 metadata 文本的唯一链降级；调用已开始而结果未知时停止评审并披露。
2. 把最终 diff 的全部代码路径与候选证据路径并入 codebase freshness barrier。屏障未证明当前
   identity/generation/coverage 时，先完成 status/coverage 核验或每个 dirty epoch 至多一次刷新，
   不得用 grep 绕开。
3. 新鲜图谱下第一次结构检索必须用 `search_graph`；按问题用 `trace_path`/`detect_changes` 核验
   调用链与影响，再用 `get_code_snippet` 和当前源码验证关键实现。候选确定后，对待核验证据路径统一
   调用 `check_index_coverage`。复用的覆盖结果须属于同一代码身份和图谱代次。源码阅读用于核实图谱发现，不以文本搜索替代调用/依赖/影响分析。
4. `rg/grep` 只可作为末尾有界补证：字面量、错误文案、配置、非代码文件，或 coverage 明示的
   partial/skipped/excluded 缺口。任何回退都记录 reason、范围和 confidence；图谱不可见或刷新后仍
   不能证明新鲜时，不得声称结构影响完整。

回执至少记录 Memory selected path/reason、codebase generation、coverage 路径并集、实际图工具和
文本回退原因；缺任一必需阶段不能给 `READY`。

## 来源

多维审查、证据门槛、skeptic 复核与分阶段验证闭环思路参考并改编自
[Everything Claude Code 的 orch-review workflow 与 verification-loop](https://github.com/affaan-m/ECC)，
Copyright 2026 Affaan Mustafa，MIT License。按风险启用审查维度的思路参考
[PR-Agent 的可配置 review sections](https://github.com/The-PR-Agent/pr-agent/blob/main/docs/docs/tools/review.md)，
MIT License；SQL 迁移静态规则思路参考
[Squawk](https://github.com/sbdchd/squawk)，MIT OR Apache-2.0；执行计划证据口径参考
[PostgreSQL EXPLAIN 文档](https://www.postgresql.org/docs/current/using-explain.html)，PostgreSQL License。
