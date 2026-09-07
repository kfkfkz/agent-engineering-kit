---
name: delivery-review
description: "在交付收口前对最终 diff 做证据化自审，覆盖正确性、项目宪章、维护性和按风险触发的安全维度，并对高危发现做对抗式复核。适用于功能、修复、重构和迁移完成后的质量门禁。"
---

# 交付审查

这是零额外依赖的最终 diff 门禁。外部 review CLI 可作为可选增强，但不可成为基础流程的隐式要求。

## 审查范围

1. 固定本次任务目标、验收条件、基线和最终 diff；区分本次修改与用户已有改动。
2. 至少逐项检查：行为正确性与失败路径、项目宪章/规范、调用影响与兼容性、测试充分性、复杂度与维护性、文档/记忆一致性。
3. 命中安全触发条件时调用 `security-review`，并把其结果纳入同一个门禁。
4. 每个发现给出准确文件/符号、可观察后果、触发条件、证据和最小修复方向；按 `CRITICAL/HIGH/MEDIUM/LOW` 标级。纯偏好不作为阻塞项。

## 对抗式复核

对 HIGH/CRITICAL 和会阻止交付的结论，另做一次“证明它是误报”的复核：检查不可达分支、上游校验、事务边界、部署配置和已有测试。运行环境允许且任务已授权多 Agent 时，优先交给独立、未参与实现的审查者；否则在同一 Agent 中清空假设做第二遍，并披露这是同上下文复核。

只保留证据和置信度足够的发现，重复问题合并为根因项。任何审查维度未完成、工具异常或证据相互矛盾时 fail closed：输出 `NOT READY` 或 `NEEDS HUMAN REVIEW`，不得把缺失检查当作通过。

## 输出

```text
结论：READY / NOT READY / NEEDS HUMAN REVIEW
阻塞发现：严重性、位置、证据、影响、修复与复核结果
非阻塞建议：
已检查维度与命令：
未覆盖范围：
```

修复发现后必须重跑直接回归，并把结果交给 `delivery-verify`，不能仅凭 diff 看起来合理就关闭。

## 来源

多维审查、证据门槛和 skeptic 复核思路参考并改编自 [Everything Claude Code 的 orch-review workflow](https://github.com/affaan-m/ECC)，Copyright 2026 Affaan Mustafa，MIT License。
