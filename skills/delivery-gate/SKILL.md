---
name: delivery-gate
description: "评审代码变更，或校验最终范围和治理要求后验证交付并形成持久回执。"
---

# 代码交付门禁

只读代码评审固定被审版本、范围与验收目标，执行「评审」并报告发现。交付任务依次执行以下各节。

## 范围与治理

1. 固定目标、验收、基线、Route Card 和最终 diff，区分本轮改动与用户已有改动。Direct 以对话保存等价事实时，收口前生成临时 Route Card。
2. 内部运行 `.repo-memory-kit/bin/route-eval <仓库根> --card <卡片> --git-ref <基线> --json`。默认包含 tracked 与未忽略的 untracked 文件。仅在已有可信完整 diff 时用 `--diff`。`route_valid=false`、`route_efficient=false` 或 `drift_detected=true` 时阻断，说明路径新增原因，修正路线或保留有依据的 `route_override` 后复验。
3. 采用 `route-eval` 返回的治理命中和 `required_actions`，单独诊断才运行 `governance-eval`。旧 `min_route:*` 按程序的迁移结果转换为治理要求，风险本身不升任务路线。

## 评审

### 代码评审取证顺序

按 [取证规范](references/review-evidence.md) 使用预算化 Memory MCP、新鲜图谱和当前源码。复用与本轮范围、阶段和身份相符的已有证据。

核对行为和失败路径、宪章、调用面与兼容性、测试、维护性、文档和记忆、实际 diff 与冻结设计。按 CRITICAL/HIGH/MEDIUM/LOW 报告位置、触发条件、影响、证据和最小修复方向。合并同根因，保留关联位置。未覆盖范围和不确定性写进结论。纯偏好不阻塞。

命中安全规则时调用 `security-review`，按 [安全工作流](../security-review/references/workflow.md) 将当前要求、来源和证明送入原 Composer。未知、过期、缺失或 ERROR 证据保持 PENDING，硬违反为 FAIL。风险接受不能替代技术证明。关闭须可信 READY 及同一 VERIFIED 快照的 closure_proof。

### 性能与容量风险覆盖

命中 `sql-performance-screen` 时执行数据库无关初查。`performance_capacity` 或 `performance-review` 触发深度验证。按 [性能参考](references/performance-review.md) 取适用证据，未命中时不增加性能专项步骤。

### 对抗式复核

HIGH、CRITICAL 和交付阻断发现须检查反证，包括不可达、上游拦截、部署条件和已有测试。宿主具备能力且允许委派时，交未参与实现的独立 Reviewer 复核当前 diff。否则在同一上下文二审并披露，治理强制的独立审查仍为待满足。子 Agent 默认继承当前模型，无法继承时披露。复验优先复用同一独立 Reviewer 会话，用当前证据核对未决发现。换会话时说明原因并保留问题历史。

## 验证与回执

按项目宪章、CI 和 `execution_profile.verification_depth` 执行适用的构建、静态检查、测试和专项验证。`receipt_detail=compact` 只跑改动直接相关的检查和快速审查，不得扩成全仓库审查。治理强制检查仍要完成。按实际影响扩展集成、契约、并发或性能验证。性能证据记录代表性数据规模、基线、命令和原始指标。线上联调的 NOT_RUN 按事实保留。

优先消费可核对的测试或构建摘要，保留完整日志、摘要、命令及真实退出码。修复后复跑直接回归。不明原因按 `systematic-debugging` 诊断。不得通过放宽断言或改写验收放行。失败、工具异常、证据矛盾或未执行的强制项须记录影响与人工补验。

从 `.repo-memory-kit/governance` 读取回执载体。strict 的非平凡变更落 `docs/delivery-receipts/YYYY-MM-DD-<中文任务名>.md`。lightweight 的低风险变更可用 commit/PR 持久回执，高风险项按治理要求落独立文件。同日同任务重跑追加编号，保留旧回执。回执详细程度由 `receipt_detail` 决定，紧凑回执记录任务、基线、范围、路线和治理裁决、验证命令与结果、发现和结论。完整回执按需读 [模板](references/full-receipt.md)。

### 计划执行统一收口

ArtifactPlan 要求计划阶段而计划尚未过门禁时，先返回设计流程。计划过门禁后，执行期只更新执行记录与勾选状态。执行前计划基线保护任务定义、范围与验证方式。上游或任务定义改变时返回 `design-pipeline`，按当前证据重开评审并保留原 Issue 和轮次。

验证结束后回写完成状态、实际范围和测试结果，落验证回执并核对最终范围。Agent 内部运行 `.repo-memory-kit/bin/doc-gate closeout <SDD目录>` 一次，核验 `reviews/计划.closeout.json` 为 PASS 且状态为已收口。收口后正文改变须复验。不得逐任务重冻或用 `gate --no-review` 代替。无需计划阶段的任务跳过收口。

仅当适用验证、治理、回执及计划收口满足后给 READY。存在技术阻断或证据不足时给 NOT READY。仍需人工裁决时报 NEEDS HUMAN REVIEW，并列出待决项。
