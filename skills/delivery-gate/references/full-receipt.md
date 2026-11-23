# 完整交付回执

仅在 `receipt_detail=full` 或项目明确要求完整回执时使用。沿用项目已有等价载体。必须记录失败、强制检查未执行的原因和人工补验方式。

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
