---
name: codebase-memory
description: "使用 codebase-memory-mcp 知识图谱完成代码结构检索、调用链追踪、影响分析和架构取证。适用于理解代码库、查找符号与调用方、评估变更影响、识别架构热点；不用于纯字符串、配置值或非代码文件搜索。"
---

# Codebase Memory

用知识图谱导航代码，再用当前源码和测试确认结论。图谱用于减少盲搜，不替代事实核验。

## 前置检查

1. 确认当前会话存在 `list_projects`、`search_graph`、`trace_path`、`get_code_snippet`、`check_index_coverage` 等 codebase-memory 工具。
2. 用 `list_projects` 或 `index_status` 确认目标仓库、索引代次和新鲜度；不存在或陈旧时先索引。
3. 工具不可用时不得假装使用过图谱：改用目标明确的源码/文本检索，并在结构性、否定性或完整性结论上标注“图谱不可用，置信度降低”。`memory-check` 要求图谱时遵循其更严格的停止规则。

## 取证等级

- **Scout**：快速找到正向候选；结论是暂定的，不用于声称“不存在”“全部”“死代码”或完整影响面。
- **Verify（默认）**：任务定向检索、相关方向调用链、关键符号源码、完整相关分页。
- **Auditor**：限定范围内核验当前代次、完整分页、双向关系及所有限制；仅在用户要求审计或结论需要穷尽时使用。

先声明本次等级。发现候选路径后，每个等级都必须调用一次 `check_index_coverage`，一次性包含全部证据路径；否定或穷尽结论还要包含相关目录范围。干净结果只表示“未记录覆盖缺口”，不代表数学意义上的完整。

## 标准流程

1. `search_graph` 找到精确符号或候选模块。
2. `trace_path` 按问题检查 inbound、outbound 或 both；跨服务、异步与数据流关系同时检查相应边。
3. `get_code_snippet` 阅读关键实现；配置、注解、字符串和未索引文件回到源码检索。
4. 变更任务使用 `detect_changes` 或明确的变更文件清单评估波及范围。
5. 执行覆盖检查并补读所有 skipped、partial、stale、pending、unknown 区段。
6. 输出“图谱证据 → 源码证据 → 限制”，不要只给图节点名称。

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

