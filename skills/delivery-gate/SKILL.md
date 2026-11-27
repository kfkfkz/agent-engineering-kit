---
name: delivery-gate
description: "代码评审、交付校验与回执。用户点名 delivery-gate 或要求汇报验收证据时使用。"
---

# 代码交付门禁

用户点名 `delivery-gate` 时立即载入，用已有范围和证据准备收口。只读代码评审固定版本、范围和验收，执行「评审」。仅汇报已有验证时给出证据和缺项。完整交付执行以下各节。

## 范围与治理

1. 固定目标、验收、基线、Route Card 和最终 diff，区分本轮改动与用户已有改动。传入写入前记录的最新已确认范围快照，保留其原 Git 基线。
2. 有范围快照时运行 `.repo-memory-kit/bin/route-eval <仓库根> --card <卡片> --scope <范围文件> --json`。校验暂存、未暂存和非忽略的未跟踪文件。有明确旧流程基线的任务仍可用 `--git-ref <基线>`，可信完整 diff 用 `--diff`。范围、归属或路线失败时说明原因并保持阻塞。提高路线不能补授权。
3. 采用 `route-eval` 返回的治理命中和 `required_actions`，单独诊断才运行 `governance-eval`。旧 `min_route:*` 按程序的迁移结果转换为治理要求，风险本身不升任务路线。

## 评审

### 代码评审取证顺序

按 [取证规范](references/review-evidence.md) 先核验执行阶段的交接，再定向补需要的 Memory、图谱和源码证据。

核对行为和失败路径、宪章、调用面与兼容性、测试、维护性、文档和记忆、实际 diff 与冻结设计。按 CRITICAL/HIGH/MEDIUM/LOW 报告位置、触发条件、影响、证据和最小修复方向。合并同根因，保留关联位置。未覆盖范围和不确定性写进结论。纯偏好不阻塞。

命中安全规则时调用 `security-review`，按 [安全工作流](../security-review/references/workflow.md) 将当前要求、来源和证明送入原 Composer。未知、过期、缺失或 ERROR 证据保持 PENDING，硬违反为 FAIL。风险接受不能替代技术证明。关闭须可信 READY 及同一 VERIFIED 快照的 closure_proof。

### 性能与容量风险覆盖

命中 `sql-performance-screen` 时执行数据库无关初查。`performance_capacity` 或 `performance-review` 触发深度验证。按 [性能参考](references/performance-review.md) 取适用证据，未命中时不增加性能专项步骤。

### 对抗式复核

HIGH、CRITICAL 和交付阻断发现须检查反证，包括不可达、上游拦截、部署条件和已有测试。宿主具备能力且允许委派时，交未参与实现的独立 Reviewer 复核当前 diff。否则在同一上下文二审并披露，治理强制的独立审查仍为待满足。子 Agent 默认继承当前模型，无法继承时披露。复验优先复用同一独立 Reviewer 会话，用当前证据核对未决发现。换会话时说明原因并保留问题历史。

## 验证与回执

按项目宪章、CI 和 `execution_profile.verification_depth` 执行适用的构建、静态检查、测试和专项验证。`receipt_detail=compact` 只跑改动直接相关的检查和快速审查，不得扩成全仓库审查。治理强制检查仍要完成。按实际影响扩展集成、契约、并发或性能验证。性能证据记录代表性数据规模、基线、命令和原始指标。线上联调的 NOT_RUN 按事实保留。

每项强制验证先明确通过条件、证据与依赖。优先使用原生摘要。缺少摘要的前台检查用 `.repo-memory-kit/bin/validation-run --log <新日志> -- <命令> [参数…]`。失败按位置补读，修复后按原断言复跑直接回归。

有现成回执时先用相同 `--repo`、适用的 `--input`、`--scope`、工作目录及命令调用 `validation-run --reuse`。工具比对当前输入和日志，成功时直接引用回执。相关输入变化时只补做受影响的检查。依赖范围不明用完整 Git 可见差异，外部输入不能核验时保留待验证。命令退出 0 只证明该检查的通过条件。

消费宿主完成事件，无通知时按需查状态。同一失败且无新线索，或强制检查缺少外部前提时，以 NOT READY 收口，回执记录证据、缺项及下一步。仍可继续定位根因时按 `systematic-debugging` 有界推进。

从 `.repo-memory-kit/governance` 读取回执载体。strict 的非平凡变更落 `docs/delivery-receipts/YYYY-MM-DD-<中文任务名>.md`。lightweight 的低风险变更可用 commit/PR 持久回执，高风险项按治理要求落独立文件。同日同任务重跑追加编号，保留旧回执。回执详细程度由 `receipt_detail` 决定，紧凑回执记录任务、基线、范围、路线和治理裁决、验证命令与结果、发现和结论。完整回执按需读 [模板](references/full-receipt.md)。

### 计划执行统一收口

ArtifactPlan 要求计划阶段而计划尚未过门禁时，先返回设计流程。计划过门禁后，执行期只更新执行记录与勾选状态。执行前计划基线保护任务定义、范围与验证方式。上游或任务定义改变时返回 `design-pipeline`，按当前证据重开评审并保留原 Issue 和轮次。

验证结束后回写完成状态、实际范围和测试结果，落验证回执并核对最终范围。Agent 内部运行 `.repo-memory-kit/bin/doc-gate closeout <SDD目录>` 一次，核验 `reviews/计划.closeout.json` 为 PASS 且状态为已收口。收口后正文改变须复验。不得逐任务重冻或用 `gate --no-review` 代替。无需计划阶段的任务跳过收口。

仅当适用验证、治理、回执及计划收口满足后给 READY。存在技术阻断或证据不足时给 NOT READY。仍需人工裁决时报 NEEDS HUMAN REVIEW，并列出待决项。
