# 证据与收口

本文件是 `repo-delivery.evidence.closeout` 的唯一权威，所有路线在交付前完整读取。

## 验证与漂移

1. 用当前 Route Card 与基线计算最终 tracked/untracked diff；未计划路径或路线过轻时更新范围并重评，
   不把漂移写成“已知偏差”后直接放行。
2. 运行 `delivery-gate`：确定性路线/治理裁决、证据化代码审查、适用测试、静态检查、性能/安全专项。
   认证、外部输入、数据库/文件/网络/命令、密钥、Agent 配置或安装脚本变更必须纳入 `security-review`。
3. governance `strict` 使用独立交付回执；`lightweight` 的低风险任务可使用项目允许的紧凑载体。
4. 未实际执行的检查不得声称通过；无外部扫描器只能披露“未运行”，不能声称“无漏洞”。

## 文档、计划与记忆

- 将实现结果整合回既有 spec/SDD/版本文档；清除失效口径，不创建平行事实源。
- 计划门禁保存执行前计划基线；执行期只持续更新执行记录。全部 release-blocking 任务和验证完成后，
  Agent 内部运行 `doc-gate closeout` 统一收口一次。
- 检查现有记忆是否受影响，需要时运行 `memory-check`；新记忆先通过 `memory-capture` 展示候选，
  用户确认后才能写入和重建索引。
- 中断或跨会话转交用 `task-handoff`，不得只留一条无证据的“继续实现”。

## 最终结论

仅在验收、宪章/治理、测试、审查、范围漂移和正式回执全部满足时给 READY。汇报完成内容、验证证据、
人工门禁、残余风险和建议提交范围。Beta/advisory 状态单独披露，不得悄悄改变 Core release verdict。
