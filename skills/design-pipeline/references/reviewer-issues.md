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

rubric 五维：
1. **事实接地**（首要，分层增量）——现状声明分三档处理：
   - **已验证事实直接采信**：业务域地图登记的模块口径、`memory-recall` 命中的
     已确认条目（pitfall/decision/playbook）、上游阶段已核实的声明——引用来源
     即可（evidence 写域地图条目或记忆路径），**不重新推导**；
   - **新增/未覆盖声明查代码**：用 `codebase-memory` 定位实际符号与调用链；
   - **矛盾即过时**：文档/记忆与代码不一致 → 以代码为准 → issue（category=
     grounding），evidence 给代码位置，并标记受影响记忆条目（回 memory-check
     巡检）；不给"我理解"式判断；
2. **一致性**——不推翻上游冻结结论、与业务域地图口径一致；
3. **可验证性**——验收可测、测试接缝完整、失败/边界/回退已覆盖；
4. **缺失维度**——该阶段应成型而未成型的兼容/安全/迁移内容；
5. **风险盲区**——对公共契约/数据模型/兼容/安全做一次"证明它错"。
