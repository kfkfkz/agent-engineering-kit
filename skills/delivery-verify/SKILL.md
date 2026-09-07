---
name: delivery-verify
description: "在最终交付前运行与风险匹配的验证闭环，记录实际命令和结果并给出 READY/NOT READY/NEEDS HUMAN REVIEW。适用于实现或修复已完成、准备汇报或提交的阶段。"
---

# 交付验证闭环

先从项目宪章、根指令、CI 和构建文件提取真实门禁，不硬编码语言、覆盖率或工具。只报告实际执行结果。

## 自适应阶段

按项目与变更适用性运行：

1. 构建、编译或类型检查；
2. 格式化、lint 和静态分析；
3. 针对性测试、相关模块测试、必要的全量/集成/契约/并发/性能测试；
4. `security-review` 要求的扫描与安全回归；
5. `delivery-review` 的最终 diff 门禁；
6. spec/SDD、变更记录、用户文档和项目记忆的一致性检查。

先跑快速、定位性强的检查，再跑成本更高的门禁。命令失败或行为异常时进入 `systematic-debugging`；不得跳过、放宽断言或改写验收条件来制造绿色。因环境、权限或缺少依赖无法运行时记录准确原因、影响和人工补验步骤。

## 报告格式

```text
结论：READY / NOT READY / NEEDS HUMAN REVIEW
验收项：逐项通过/失败/未运行及证据
实际命令：命令、退出码、关键结果
审查结果：delivery-review 结论与已关闭发现
文档与记忆：一致/待同步
未覆盖与残余风险：
```

只有适用门禁全部通过、没有未裁决的阻塞发现、代码与权威文档无已知冲突时才可标记 `READY`。项目要求人工审核时必须标记 `NEEDS HUMAN REVIEW`，即使自动检查全绿。

## 来源

分阶段验证闭环思路参考并改编自 [Everything Claude Code 的 verification-loop](https://github.com/affaan-m/ECC)，Copyright 2026 Affaan Mustafa，MIT License。
