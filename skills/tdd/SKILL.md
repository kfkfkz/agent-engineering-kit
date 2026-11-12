---
name: tdd
description: "项目要求测试先行、用户明确要求 TDD 或关键行为需要回归时，按 RED→GREEN→REFACTOR 保留可验证证据。"
license: MIT
metadata:
  source: "Adapted from mattpocock/skills tdd and improve-codebase-architecture"
---

# 测试先行

Claude Code 使用 `/tdd`，Codex 使用 `$tdd`。

先确定可观察行为、独立预期与项目验证门槛。定位现有接缝必须先用 `codebase-memory`，按当前工作树与 coverage 取证。缺陷根因不明时先诊断。

每个纵向切片保留预期失败的 RED、最小生产实现的 GREEN 和保持行为的整理。测试走稳定公共接口，预期值以已确认事实为准。断言不随实现细节迁移。优先使用真实系统接缝验证并发、失败和恢复。

难测、过度 mock 或散弹修改属于架构信号。公开契约或模块边界变化回到 `repo-delivery` 处理。记录覆盖行为、失败证据、实际验证和缺口。

本技能吸收 Matt Pocock 上游资料中 tdd 的公共接口、独立预期值和纵向切片思想，并参考其中 improve-codebase-architecture 的深模块、locality 与 deletion test 校准测试接缝；后二者是来源资料名，不是 AEK 可调用 skill。上游版权为 Copyright (c) 2026 Matt Pocock，按 MIT License 使用和改编；来源：`mattpocock/skills`。
