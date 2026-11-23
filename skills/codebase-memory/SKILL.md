---
name: codebase-memory
description: "用当前工作树的代码图谱定位符号、追调用链和分析变更影响。"
---

# Codebase Memory

用知识图谱导航代码，再用当前源码和测试确认结论。图谱用于减少盲搜，不替代事实核验。

## 前置检查

1. 确认当前会话存在 `list_projects`、`search_graph`、`trace_path`、`get_code_snippet`、`check_index_coverage` 等 codebase-memory 工具。
2. Git 仓库先用内部 `.repo-memory-kit/bin/codebase-context resolve <实际代码目录> --context-json -`，输入对象 `{"projects": [...]}`，数组来自 `list_projects`。根目录仅有子索引时按任务涉及的子目录解析。用所选项目的 `index_status(verbose=true)` 核对 `worktree_root`、`index_root` 和 `head`，也可将 MCP 原结果作为输入对象的 `index_status` 字段交给同一命令。返回同根 `project_candidates` 时，将其中可核对的候选原状态传入 `index_status`；若多个候选都匹配，再按实际文件 coverage 选择。无 aek 安装时直接核对这些字段。
3. `configured`、`visible`、`indexed`、`fresh` 分开记录。索引归属和 HEAD 核对通过后，仍以当前代次及相关文件 coverage 判定新鲜度。写代码、Git/SVN 更新、切分支或合并后重验身份，即使没有源码 diff。当前批次共享一次 status/coverage 与最多一次显式刷新。
   `plan/observe` 返回 `refresh_token` 时，在有效期内执行索引；沿同一个工作树和工作单元调用内部
   `codebase-context refresh-result`，JSON 带原 `refresh_token` 与 `outcome=executed`（工具已执行，包括执行失败）
   或 `not_started`（确认未启动）。遇到 `refresh_pending` 或预约过期先查索引状态。仍在运行时等待，确认未运行才重新规划。
4. 已证明覆盖的路径用图谱，源码仅补读缺口。`metadata_match` 只证明文件元数据相符，仍须补读解析缺口。用 `codebase-context observe` 时，将明确跳过、排除或未收录的路径放入 `source_only_paths`，沿返回的 `graph_paths` 与 `source_paths` 取证。工具不可见或刷新后仍无可用覆盖时有界回退并披露置信度；`memory-check` 遵循更严格的停止规则。

## 取证等级

- **Scout**：快速找到正向候选；结论是暂定的，不用于声称“不存在”“全部”“死代码”或完整影响面。
- **Verify（默认）**：任务定向检索、相关方向调用链、关键符号源码、完整相关分页。
- **Auditor**：限定范围内核验当前代次、完整分页、双向关系及所有限制；仅在用户要求审计或结论需要穷尽时使用。

先声明本次等级。发现候选路径后，每个等级都必须调用一次 `check_index_coverage`，一次性包含全部证据路径；否定或穷尽结论还要包含相关目录范围。干净结果只表示“未记录覆盖缺口”，不代表数学意义上的完整。

## 标准流程

1. 先按查询目的分类。定位实现、追调用者或检查依赖用 STRUCTURAL，即使问题引用错误文案；匹配具体字符串、查配置或非代码文件用 TEXTUAL。内部 `codebase-context plan` 可传 `query_kind: "STRUCTURAL" | "TEXTUAL"`。STRUCTURAL 的第一次检索必须使用图工具；TEXTUAL 直接有界查找，无需刷新索引。
2. `search_graph` 找到精确符号或候选模块。
3. `trace_path` 按问题检查 inbound、outbound 或 both；跨服务、异步与数据流关系同时检查相应边。
4. `get_code_snippet` 阅读关键实现；配置、注解、字符串和未索引文件回到源码检索。
5. 变更任务使用 `detect_changes` 或完整变更文件清单评估波及范围。
6. 候选确定后，一次 coverage 调用必须包含当前 dirty epoch 的全部代码路径与本次全部候选证据路径；仅对工具报告的 skipped/partial/excluded/not_tracked 精确范围回退源码。
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
