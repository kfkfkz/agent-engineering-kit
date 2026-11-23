# 评审问题与定位格式

```json
{
  "reviewer": "independent-subagent",
  "stage": "详细设计",
  "doc_hashes": {"详细设计.md": "<sha256——评审时计算；门禁核对，文档改过即评审作废>"},
  "issues": [
    {"id": "REV-001", "severity": "blocker|major|minor",
     "category": "grounding|requirement_gap|consistency|verifiability|risk|upstream",
     "location": "§数据与存储/D2", "problem": "…", "evidence": "…",
     "required_change": "…", "affected_ids": ["D2", "A5"],
     "upstream": false}
  ]
}
```

**字段硬约束**（doc-gate 拒绝不合规文件）：`stage` 必须匹配当前阶段；
`doc_hashes` 必须覆盖该阶段全部文档（sha256sum 计算）——评审后文档再改动，
评审即过期，必须重评；每条新 issue 必含 id/severity/location/problem/evidence/
required_change/affected_ids，severity 只允许 blocker/major/minor。`affected_ids`
填受影响的 A/F/D/T/TC ID；无法定位时写 `UNSCOPED`，不得猜 ID。

incremental 发现未知位置/范围缺口时先保留 OPEN occurrence，再按机器结论扩大或 full，
未覆盖的问题不能 VERIFIED/CLOSED。当前 full 已完整覆盖材料仍有未知位置时，停止相同
全文重送，等待定位证据；full 之外的范围在已有权限/计划内显式扩大，否则交维护者裁决。
需要澄清旧问题位置时，仅实际 Reviewer 在本轮 full receipt 的 `issue_locations` 返回
`{fingerprint, document, heading, section_digest}`；必须绑定原 fingerprint、当前实际章节字节，
并覆盖该问题全部已知 owner/位置。Author 提示只是待验证背景，不改旧 occurrence；Gate
核验合法定位后仍须有效 FIXED 声明、当前范围复核与原状态链，不能靠定位摘要直接关闭。

## 判断本阶段设计能否交付

- 对照已确认的验收和保持项，找出会改变业务结果或使实现、测试无法落地的问题。优先检查关键路径、失败行为、兼容和回退。按当前阶段及真实影响检查安全、性能、迁移等必要内容。
- 决定本次方案的现状声明需要当前证据。当前工作树和范围已核实的事实直接复用。历史记忆、域地图与上游结论可作为线索；关键声明缺当前证据、发生变化或存在冲突时，用 `codebase-memory` 定向核对源码、契约和测试。
- 证据与设计冲突时在 issue 中提供可复核位置，说明受影响的验收或消费者，以及需要修正的行为。事实仍无法确定时明确缺口和影响，保留未决。确认结论由维护者决定，状态由 Gate 裁决。
