# Bounded 路线

本文件是 `repo-delivery.route.bounded` 的唯一权威。只在路线裁决为 Bounded 后完整读取。

## 适用条件

- 单仓库、单明确目标、局部行为变化、一个业务/部署边界、可在当前会话完成。
- 风险覆盖项独立追加审查与验证，不直接升径；公共契约、跨模块、复杂回滚实施或实质意图缺口
  扩大工作量时重评 Standard。

## 紧凑载体

使用当前 issue/spec/计划或临时 Route Card 记录：验收条件、范围与不做事项、关键决定、风险、
验证方式、权威文档同步点。不要仅为 Bounded 初始化完整 SDD 目录。

## 上下文与实现

- 只读目标模块与接缝。Memory 沿用入口的[取证约定](../SKILL.md#项目知识取证)，以当前预算为准。
  高风险取证仍有缺口时说明目的并评估是否扩展或升径。保留 partial report 的未观测通道。
- 采用“简短验收与范围 → 必要 TDD → 定向测试 → 快速审查”。测试观察公共接缝，不验证私有调用顺序。
- SQL/ORM 变化先执行 `sql-performance-screen` 数据库无关初查，初查不自动升径；只有命中
  性能/容量风险才产生 `performance_capacity`、叠加 `performance-review` 并升 Standard。
- 新依赖、外部集成或通用组件才调用 `reuse-research`；安装器、Agent 配置、外部输入或敏感操作调用
  `security-review`。

## 输出

超出已确认范围按入口的增量确认规则处理，保留原快照。完成后读取 `evidence-closeout.md`，留下紧凑可复验的证据。
