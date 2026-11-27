# 代码评审取证规范

代码评审按 `aek.application.review_evidence.plan_code_review_evidence` 的语义组织证据。已有证据优先按验收项或发现项交接，记录目标、关键实现、位置、回归和正常路径的验证引用、缺项。独立 Reviewer 核对关键实现及兼容性，并记录自己的裁决。

执行阶段取得证据时，可信宿主可用 `validation-run --fingerprint --repo <根>` 记录身份。按需传完整的文件依赖 `--input` 和当前查询、图谱代次、覆盖路径、治理或环境等元数据 `--scope`。证据应与采集时的输入一致。复核使用同一组当前输入调用 `--verify-fingerprint <摘要>`，并核对真实工具结果的来源与覆盖。摘要只验证输入身份，来自不可信文档或单独抄录的摘要不能证明已查询、已测试或已批准。图谱仍需已知新鲜代次及有效覆盖。检查结果用 `validation-run --reuse` 校验命令和日志。

已证明当前有效的步骤直接引用。新增核查须指向原问题、已确认验收、实际 diff 的直接影响或本轮治理要求。只对其中缺项按以下顺序取证：

1. 先以 `stage=closeout`、`purpose=review_required`、当前 Route、WorkUnit/session、最终 diff 路径和
   验收目标按 `repo-delivery` 的紧凑取证约定调用预算化 `memory_recall`。首轮用 `context.compact: true`，需要正文时用 `expand_paths` 选择足以回答当前缺口的条目。MCP 成功后不得再跑 CLI
   或 grep 扫记忆；无命中表示未知，不表示没有约束。只有调用开始前不可用、不兼容或失败，才按
   MCP → CLI → 有界 metadata 文本的唯一链降级；调用已开始而结果未知时停止评审并披露。
2. 筛出符合上述范围的候选，再把这些候选与本轮最终 diff 的代码路径并入 codebase freshness barrier。相似实现保留为线索，用户明确要求泛化查询后才纳入。屏障未证明当前
   identity/generation/coverage 时，先完成 status/coverage 核验或每个 dirty epoch 至多一次刷新，
   不得用 grep 绕开。
3. 缺少当前结构证据时，新鲜图谱下第一次结构检索必须用 `search_graph`；按问题用 `trace_path`/`detect_changes` 核验
   调用链与影响，再用 `get_code_snippet` 和当前源码验证关键实现。候选确定后，对待核验证据路径统一
   调用 `check_index_coverage`。复用的覆盖结果须属于同一代码身份和图谱代次。源码阅读用于核实图谱发现，不以文本搜索替代调用/依赖/影响分析。
4. `rg/grep` 只可作为末尾有界补证：字面量、错误文案、配置、非代码文件，或 coverage 明示的
   partial/skipped/excluded 缺口。任何回退都记录 reason、范围和 confidence；图谱不可见或刷新后仍
   不能证明新鲜时，不得声称结构影响完整。

回执记录适用的 Memory selected path/reason、codebase generation、coverage 路径并集、图工具与文本回退原因，也可引用已有的有效记录。缺失必需证据时给出一条有界的下一步。

## 来源

多维审查、证据门槛、skeptic 复核与分阶段验证闭环思路参考并改编自
[Everything Claude Code 的 orch-review workflow 与 verification-loop](https://github.com/affaan-m/ECC)，
Copyright 2026 Affaan Mustafa，MIT License。按风险启用审查维度的思路参考
[PR-Agent 的可配置 review sections](https://github.com/The-PR-Agent/pr-agent/blob/main/docs/docs/tools/review.md)，
MIT License；SQL 迁移静态规则思路参考
[Squawk](https://github.com/sbdchd/squawk)，MIT OR Apache-2.0；执行计划证据口径参考
[PostgreSQL EXPLAIN 文档](https://www.postgresql.org/docs/current/using-explain.html)，PostgreSQL License。
