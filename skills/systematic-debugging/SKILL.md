---
name: systematic-debugging
description: "在根因不明、诊断跨模块或反复失败时定位问题，记录根因、影响范围和可验证的修复证据。"
---

# 系统化诊断

Claude Code 使用 `/systematic-debugging`，Codex 使用 `$systematic-debugging`。

第一次结构检索必须先用 `codebase-memory`，沿当前工作树的实际调用链和 coverage 回到源码。日志、字面量和缺口做定向查找。

在现有任务记录留下现象、稳定复现或反证、根因、影响范围、验证假设和结果。证据确立后定向修复，复跑原场景及适用回归。独立的诊断请求以根因和修复建议收口。

同一失败反复出现且没有新证据时停下，保留失败现场和未决假设。涉及公共契约或架构的处置回到 `repo-delivery`。
