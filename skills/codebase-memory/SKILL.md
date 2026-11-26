---
name: codebase-memory
description: "用当前工作树的代码图谱定位符号、追调用链和分析变更影响。"
---

# Codebase Memory

用知识图谱导航代码，再用当前源码和测试确认结论。图谱用于减少盲搜，不替代事实核验。

## 前置检查

1. 确认当前会话存在 `list_projects`、`search_graph`、`trace_path`、`get_code_snippet`、`check_index_coverage` 等 codebase-memory 工具。
2. Git 仓库先用内部 `.repo-memory-kit/bin/codebase-context resolve <实际代码目录> --context-json -`，输入对象 `{"projects": [...]}`，数组来自 `list_projects`。根目录仅有子索引时按任务涉及的子目录解析。用所选项目的 `index_status(verbose=true)` 核对 `worktree_root`、`index_root` 和 `head`，也可将 MCP 原结果作为输入对象的 `index_status` 字段交给同一命令。返回同根 `project_candidates` 时，将其中可核对的候选原状态传入 `index_status`；若多个候选都匹配，再按实际文件 coverage 选择。无 aek 安装时直接核对这些字段。
3. `configured`、`visible`、`indexed`、`fresh` 分开记录。同阶段、同一工作树、代码身份和图谱代次复用已核验的结果及 coverage。新增候选只补本次缺口。写代码、Git/SVN 更新、切分支或合并后重验身份，即使没有源码 diff。当前批次最多一次显式刷新。
   `plan/observe` 返回 `refresh_token` 时，在有效期内执行索引；沿同一个工作树和工作单元调用内部
   `codebase-context refresh-result`，JSON 带原 `refresh_token` 与 `outcome=executed`（工具已执行，包括执行失败）
   或 `not_started`（确认未启动）。遇到 `refresh_pending` 或预约过期先查索引状态。仍在运行时等待，确认未运行才重新规划。
4. 已证明覆盖的路径用图谱，源码仅补读缺口。`metadata_match` 只证明文件元数据相符，仍须补读解析缺口。用 `codebase-context observe` 时，将明确跳过、排除或未收录的路径放入 `source_only_paths`，沿返回的 `graph_paths` 与 `source_paths` 取证。工具不可见或刷新后仍无可用覆盖时有界回退并披露置信度；`memory-check` 遵循更严格的停止规则。

## 取证等级

- **Scout**：快速找到正向候选；结论是暂定的，不用于声称“不存在”“全部”“死代码”或完整影响面。
- **Verify（默认）**：任务定向检索、相关方向调用链、关键符号源码、完整相关分页。
- **Auditor**：限定范围内核验当前代次、完整分页、双向关系及所有限制；仅在用户要求审计或结论需要穷尽时使用。

首次取证或代码身份改变时，各等级一次核对当前所有证据路径的 `check_index_coverage`。否定或穷尽结论还要核对相关目录范围。覆盖结果表示索引记录的能力与缺口。

## 标准流程

1. 按目的选证据。查运行结果先用现有日志或状态摘要。追故障入口、调用链或依赖用 STRUCTURAL，即使问题引用错误文案。匹配字面量、查配置或非代码文件用 TEXTUAL。内部 `codebase-context plan` 可传 `query_kind: "STRUCTURAL" | "TEXTUAL"`。STRUCTURAL 的第一次检索必须使用图工具。TEXTUAL 直接有界查找。
2. `search_graph` 找到精确符号或候选模块。
3. `trace_path` 按问题检查 inbound、outbound 或 both；跨服务、异步与数据流关系同时检查相应边。
4. `get_code_snippet` 阅读关键实现；配置、注解、字符串和未索引文件回到源码检索。
5. 变更任务使用 `detect_changes` 或完整变更文件清单评估波及范围。
6. 核验的 coverage 并集必须覆盖当前 dirty epoch 改动路径和候选证据路径。扩大范围时合并本轮已核验结果与新增路径。仅对工具报告的 skipped/partial/excluded/not_tracked 精确范围回退源码。
7. 输出“代码身份/代次 → 图谱证据 → coverage → 源码证据 → 限制”，不要只给图节点名称。

## 常用选择

| 问题 | 首选 |
| --- | --- |
| 找符号/实现 | `search_graph` → `get_code_snippet` |
| 谁调用它 | `trace_path` inbound |
| 它调用谁 | `trace_path` outbound |
| 完整调用上下文 | `trace_path` both |
| 本次改动影响 | `detect_changes` + 相关调用链 |
| 高扇入/高扇出/死代码候选 | `search_graph` 度数过滤，Auditor 核验 |
| 复杂跨服务关系 | `query_graph`，限制返回规模并处理分页 |
