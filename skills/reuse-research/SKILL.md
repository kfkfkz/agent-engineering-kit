---
name: reuse-research
description: "在引入依赖、外部集成或关键选型前，比较采用、扩展、组合与自建。"
---

# 复用优先调研

以证据判断已有能力能否采用、扩展或组合。

## 触发与边界

新依赖、外部集成、通用组件或关键选型先调研；路径明确的小改动无需调研。只评估时不安装或修改外部系统。

## 流程

1. 先查仓库、依赖、记忆与权威设计；第一次结构检索必须调用 `codebase-memory`（freshness/coverage），
   禁止先 grep 代码；文本仅补字面量、配置、非代码或 coverage 缺口。
2. 有空缺再查官方文档、包仓库、MCP/插件及 GitHub；技术依据用一手来源，记录版本与日期。
3. 比较能力、集成/退出成本、维护、许可、安全与团队负担。
4. 明确选择一种结论：
   - `采用`：现成能力满足需求；
   - `扩展`：已有能力只缺小范围适配；
   - `组合`：多个稳定能力能闭合需求；
   - `自建`：候选不满足约束，并给出反证。
5. 把结论写入项目既有 spec/plan/decision 载体，不另建平行报告。

可保持选型未决；方案方向不等于 ADOPTABLE，不把 PENDING 写成已批准采用。

涉及拟采用/升级/降级/替换依赖时，先完整读
[依赖采用与交付复验](../security-review/references/dependencies.md)（Catalog `security-dependencies`）。
按 [项目知识取证](../repo-delivery/SKILL.md#项目知识取证) 用 `stage=design/purpose=target_evidence`，
结合当前图谱与源码核实依赖使用面。设计评审根据具体拟用版本、公告、兼容性和处置决定是否采用。
PENDING 提交设计评审；真实解析与产物复验按依赖参考在适用阶段补证。

## 输出契约

```text
需求与约束：
已查渠道与证据：
候选比较：
决策：采用 / 扩展 / 组合 / 自建
未选原因：
依赖、许可与安全影响：
拟包/准确版本/作用域与来源：
适用公告/核验时点/兼容/当前风险处置：
独立解析/产物后补范围：
需要人工确认的事项：
```

未搜到不等于不存在；披露检索限制，遵守宪章与审批门槛。

## 来源

本流程的 search-first 与 adopt/extend/build 思路参考并改编自 [Everything Claude Code](https://github.com/affaan-m/ECC)，Copyright 2026 Affaan Mustafa，MIT License。
