---
name: delivery-gate
description: "代码交付门禁：Route Card 最低路线与最终 diff 漂移校验 + 治理引擎裁决 + 证据化审查（含风险触发式性能/容量审查）+ 验证闭环 + 交付回执。两阶段：route-eval/governance-eval 确定性裁决与对抗式复核，然后按项目门禁执行验证闭环。回执载体由 governance 标记决定。结论枚举 READY / NOT READY / NEEDS HUMAN REVIEW。"
---

# 交付门禁（代码变更）

## 治理等级（governance profile）

**第一步：读取 `.repo-memory-kit/governance`**——它决定本节的回执形态要求。

- **strict**（默认）：所有非平凡变更必须落正式验证回执（`docs/delivery-receipts/`）
- **lightweight**（`install.sh --lightweight` 启用）：仅**高风险变更**（数据库
  schema、外部 API 契约、安全域）要求独立回执文件；低风险变更（文案/配置/
  纯内部重构）允许在 commit message 或 PR 描述中含验证结论替代

**lightweight 不降低验证标准，只放宽回执形式**：测试必须通过、行为变更
必须可验证、高风险判定标准不变。本节优先于下方"必需产物"中的回执要求
（冲突时以 governance marker 实际值为准）。

## 阶段一：路线复核、治理评估与最终 diff 审查

1. 固定本次任务目标、验收条件、Route Card、基线和最终 diff；区分本次修改与用户已有改动。
2. **路线与范围裁决**：运行
   `.repo-memory-kit/bin/route-eval <仓库根> --card <Route Card> --git-ref <基线> --json`；
   它会合并 tracked diff 与未忽略的 untracked 文件。仅当 CI 已提供可信完整 diff 时使用
   `--diff <文件>`，不得用默认漏掉 untracked 的 `git diff` 冒充最终范围。
   `route_valid=false`、`route_efficient=false` 或 `drift_detected=true` 时阻断：路线过轻就
   补充适用设计/检查或升径；路线过重就降到 `recommended_route`，除非存在带具体原因的
   `route_override`。不得在交付阶段静默扩大 `planned_paths` 以制造 PASS；新增
   路径必须说明来源和影响。Direct 若只在进度中记录等价字段，收口前生成临时卡片供校验。
3. **治理引擎裁决**（确定性，不是你的语义判断）：`route-eval` 已复用同目录
   `governance-eval` 的结果；需要单独诊断规则时再把 diff 喂给
   `.repo-memory-kit/bin/governance-eval <仓库根目录> --diff <file>`（或管道），
   拿到命中规则 ID 与 required_actions（receipt / independent_review / inline_review /
   min_route / required_check / review_depth）。
   引擎输出即门禁要求——命中 `independent_review` 时本节对抗式复核必须执行。
4. 至少逐项检查：行为正确性与失败路径、项目宪章/规范、调用影响与兼容性、
   测试充分性、复杂度与维护性、文档/记忆一致性、**与冻结设计文档的偏离**
   （diff 超出 任务清单 预计修改范围 = 显式记录，静默超范围视作未声明范围蔓延）。
5. 命中安全触发条件时调用 `security-review`，并把其结果纳入同一门禁。
6. 专项检查含 `sql-performance-screen` 时只执行下方数据库初查；Route Card 含
   `performance_capacity` 或治理要求 `performance-review` 时才执行深度验证。
   未命中时不增加性能专项步骤。
7. 每个发现给出准确文件/符号、可观察后果、触发条件、证据和最小修复方向；按
   `CRITICAL/HIGH/MEDIUM/LOW` 标级。纯偏好不作为阻塞项。

### 性能与容量风险覆盖

这是两级、风险触发式审查，不是所有代码变更的固定仪式。

#### 一级：数据库无关初查

SQL/ORM 查询形状或访问模式变化时启用 `sql-performance-screen`，但它**不自动升径**。先识别
目标数据库的方言、版本、schema/索引事实和调用场景，再做低成本静态检查：

初查结构化证据采用 `aek.core.planning.sql_screen.SqlPerformanceScreenResult`：记录目标数据库
`dialect`、`query_kind`、实际检查的 `screened_dimensions`、候选 `risk_indicators`、
`scope_complete` 与证据 digest，不把 SQL 原文写进 Context Ledger。该结构只产生
`performance_capacity=true|false|unknown` 候选事实；缺证据保持 unknown，不能假称性能通过。

- 是否出现 N+1、循环查询、重复往返或不合理的事务跨度；
- 结果集、扫描、分页和批次是否有界，过滤/连接/排序列是否能对应已声明索引；
- 参数分布、空值、隐式转换、函数谓词或通配模式是否可能破坏访问路径；
- 写入、DDL、回填是否有明显的长事务、锁放大或一次性大批量风险。

这是候选风险筛查，**初查不要求执行计划或压测**，也不得仅凭 SQL 文本宣称性能通过。
不同数据库在优化器、索引、锁、DDL 和计划输出上差异明显：必须按项目实际数据库解释，
不得把 PostgreSQL 的 `CONCURRENTLY`、锁级别或计划节点直接套到 MySQL、Oracle、SQL Server、
SQLite 等系统。初查没有发现规模风险时，记录“不升级 + 依据”后结束。

#### 二级：深度性能验证

只有存在明确性能预算、已知热点/大表或高并发路径、schema/索引/回填风险、初查无法排除的
访问路径问题，或批处理/扇出/序列化/缓存/并发可能随规模恶化时，才标记
`performance_capacity` 并执行 `performance-review`。最终 diff 新增这些证据时必须更新 Route
Card 重评，不能只在回执里补一句“已关注性能”。

先从需求、冻结设计、SLO/NFR、现有基准与生产观测提取预算，优先级为：项目明确门槛 >
已确认设计目标 > 可复现的变更前基线。**不得虚构性能阈值**；没有目标时说明采用的基线、
数据规模与局限。根据改动只执行适用项：

1. **SQL/ORM 查询**：记录实际 SQL（含 ORM 生成结果）、参数分布、调用次数与事务边界。
2. **运行计划**：在代表性数据规模和统计信息下使用目标数据库原生计划工具（如项目数据库
   支持的 `EXPLAIN` 或等价能力）；安全的非生产环境才使用会真正执行语句的分析模式，写语句
   必须放在可回滚事务/隔离环境，禁止拿生产写路径试验。按该数据库语义比较估算/实际行数、
   扫描/访问方式、连接与循环、spill、IO、锁等待和执行时间；计划形状必须与数据基数一起解释。
3. **索引与迁移**：核对过滤、连接、排序与目标数据库的索引匹配规则/选择性，评估重复索引
   和写放大；依据该数据库版本审查 DDL 锁、表重写、在线变更能力、约束校验、长事务、回填
   批次/节流与回滚。
4. **非 SQL 路径**：检查复杂度随输入规模的增长、循环内 IO/远程调用、分配与序列化、批量与
   背压、锁竞争/任务泄漏、缓存命中与失效；按项目既有工具补微基准、负载或剖析证据。
5. **端到端验证**：在预算对应的并发、数据量、冷/热缓存和失败场景下记录 p50/p95/p99、
   吞吐或资源指标中实际适用者，并与目标或变更前基线比较；单次耗时不得冒充容量结论。

性能发现必须写明位置、场景/数据规模、证据、预算或基线、可观察后果、最小修复与置信度。
**静态审查不能证明运行时性能**：它只能发现候选风险。强制 `performance-review` 缺少可复现
环境、代表性数据、执行计划/基准或权威预算时，结论最多为 `NEEDS HUMAN REVIEW` 并列出补验；
证据已显示违反明确预算时为 `NOT READY`。只有适用证据满足门槛，或证明该项对当前路径确实
不适用并给出证据，才可 `READY`。

### 对抗式复核

对 HIGH/CRITICAL 和会阻止交付的结论，另做一次"证明它是误报"的复核：检查不可达分支、
上游校验、事务边界、部署配置和已有测试。运行环境提供子 Agent **且当前上层指令允许
委派**时，把 diff、验收条件与本轮发现交给一个**未参与实现的独立审查 Agent** 复核；
不可用时在同一 Agent 中清空假设做第二遍，并在回执中披露这是同上下文复核。

只保留证据和置信度足够的发现，重复问题合并为根因项。任何审查维度未完成、工具异常
或证据相互矛盾时 fail closed：结论只能更严，不得把缺失检查当作通过。

## 阶段二：验证闭环

先从项目宪章、根指令、CI 和构建文件提取真实门禁，不硬编码语言、覆盖率或工具。
按项目与变更适用性运行，先快后慢，并遵循 `execution_profile.verification_depth`：当
`receipt_detail=compact`（Direct/Bounded）时，只执行改动直接相关的验证和快速审查，
不得扩成全仓库审查、无关测试矩阵或独立设计复核；治理规则/风险覆盖项明确要求的检查除外。
Standard/Initiative 才按影响面扩大验证：

1. 构建、编译或类型检查；
2. 格式化、lint 和静态分析；
3. 针对性测试、相关模块测试、必要的全量/集成/契约/并发/性能测试；命中性能覆盖项时附上
   数据规模、预算/基线、命令与原始指标摘要，不能只写“性能通过”；
4. `security-review` 要求的扫描与安全回归；
5. spec/设计文档、变更记录、用户文档和项目记忆的一致性检查；
6. 需求目录有线上联调清单时核对其完整性：遗留 NOT_RUN 项必须如实进回执，
   **不得按"应该成功"代填 PASS**（事实文档原则）。

### 计划执行统一收口

存在已过计划门禁的 SDD 时，执行期间只更新《任务清单》的执行记录与 checkbox；任务定义、
预计范围和验证方式仍由执行前计划基线保护。验证闭环完成后按顺序：

1. 回写每个任务的完成状态、实际改动范围、验证结果和备注，并完成适用的测试/回归 checkbox；
2. 落验证回执并完成最终范围对照，不把超范围修改伪装成执行记录；
3. 由 Agent 内部运行 `.repo-memory-kit/bin/doc-gate closeout <SDD目录>` 统一收口；
4. 核验 `reviews/计划.closeout.json` 为 PASS，且状态显示“已收口”。

closeout 会重新执行计划硬校验、拒绝未完成任务或执行前计划基线漂移，并绑定任务清单与测试
方案的完整终态哈希。收口后再修改任何内容都会回到 DRAFT，必须重新验证并再次收口。只有
该步骤 PASS 后才能对外给出 READY；不得逐任务重冻，也不得用 `gate --no-review` 覆盖执行期
的任务定义变化。没有 SDD/计划阶段的 Direct 或 Bounded 任务不增加这一步。

命令失败或行为异常时进入 `systematic-debugging`；不得跳过、放宽断言或改写验收条件
来制造绿色。因环境、权限或缺少依赖无法运行时，在回执中记录准确原因、影响和人工补验
步骤。阶段一的修复必须重跑直接回归后重新过门，不能仅凭 diff 看起来合理就关闭。

## 验证回执（必需产物）

验证结论必须落持久回执，这是**声称验证通过的唯一证据形态**——对话内叙述的验证结果
不构成完成依据。回执载体由 `.repo-memory-kit/governance` 决定（见「治理等级」节）：
strict 写入 `docs/delivery-receipts/YYYY-MM-DD-<中文任务名>.md`，lightweight 低风险
允许 commit message 内嵌；**同日同任务重跑时追加 `-2`、`-3` 序号，不覆盖**（目录不存在
则创建）：

回执的**存在形式**与**详细程度**正交：strict 仍要求 Direct/Bounded 落文件，但
`receipt_detail=compact` 时只保留任务、范围、路线、实际执行命令/结果、发现和结论，不为
填满模板运行无关检查；`receipt_detail=full` 才使用完整影响面与审查细节。下列模板是 full
上限，compact 可删除不适用的表格行，但不得省略失败、未执行的强制项或人工补验步骤。

```markdown
---
type: receipt
created: YYYY-MM-DD HH:MM
task: <一句话任务>
scope: <变更范围（文件/模块）>
verdict: READY / NOT READY / NEEDS HUMAN REVIEW
governance: <引擎命中规则与 required_actions 摘要>
route: <selected/minimum；是否存在 drift>
review: <审查结论；对抗复核为独立 Agent 或同上下文>
commit: <HEAD SHA 或 "uncommitted">
base: <基线 SHA 或 "none">
---

# 交付验证回执：<任务>

## 门禁执行

| 检查 | 方式/命令 | 结果 |
| --- | --- | --- |
| 治理评估 | governance-eval（命中规则/无命中） | <要求与满足情况> |
| 路线复核 | route-eval（selected/minimum + drift） | <一致/已升径/阻断> |
| 构建 | <命令>（退出码 <code>） | <一句话> |
| 范围对照 | diff vs 任务清单预计修改范围 | <一致/超范围项> |

未执行项必须写明原因与人工补验步骤，不得留空。

## 审查发现与处置

| 严重性 | 位置 | 证据 | 处置（已修复 / 留档 / 误报-复核依据） |
| --- | --- | --- | --- |

## 结论

<READY / NOT READY / NEEDS HUMAN REVIEW 及依据；人工审核项逐条列出>
```

只有适用门禁全部通过、没有未裁决的阻塞发现、代码与权威文档无已知冲突时才可 `READY`。
项目要求人工审核时必须 `NEEDS HUMAN REVIEW`，即使自动检查全绿。回执落盘后不改写历史
行；重跑门禁就写新回执。

## 来源

多维审查、证据门槛、skeptic 复核与分阶段验证闭环思路参考并改编自
[Everything Claude Code 的 orch-review workflow 与 verification-loop](https://github.com/affaan-m/ECC)，
Copyright 2026 Affaan Mustafa，MIT License。按风险启用审查维度的思路参考
[PR-Agent 的可配置 review sections](https://github.com/The-PR-Agent/pr-agent/blob/main/docs/docs/tools/review.md)，
MIT License；SQL 迁移静态规则思路参考
[Squawk](https://github.com/sbdchd/squawk)，MIT OR Apache-2.0；执行计划证据口径参考
[PostgreSQL EXPLAIN 文档](https://www.postgresql.org/docs/current/using-explain.html)，PostgreSQL License。
