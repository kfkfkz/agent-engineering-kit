# Bounded 路线

本文件是 `repo-delivery.route.bounded` 的唯一权威。只在路线裁决为 Bounded 后完整读取。

## 适用条件

- 单仓库、单明确目标、局部行为变化、一个业务/部署边界、可在当前会话完成。
- 无高风险覆盖项；发现公共契约、跨模块、难回退或实质意图缺口时升 Standard。

## 紧凑载体

使用当前 issue/spec/计划或临时 Route Card 记录：验收条件、范围与不做事项、关键决定、风险、
验证方式、权威文档同步点。不要仅为 Bounded 初始化完整 SDD 目录。

## 上下文与实现

- 只读目标模块与接缝。Memory 先看 metadata，默认候选不超过 5、正文展开不超过 2；命中高风险、
  截断或低置信时记录目的后扩展，不能把 top-k 未命中当作“无约束”。
  Agent 内部使用 `memory-recall --context-json` 并传冻结 Route、subject digest、稳定 session ID；
  只消费返回的 `expanded`，不得绕过预算直接打开未选择候选。partial report 中未观测的宿主通道
  必须保留披露，不得改写成 complete。
- 采用“简短验收与范围 → 必要 TDD → 定向测试 → 快速审查”。测试观察公共接缝，不验证私有调用顺序。
- SQL/ORM 变化先执行 `sql-performance-screen` 数据库无关初查，初查不自动升径；只有命中
  性能/容量风险才产生 `performance_capacity`、叠加 `performance-review` 并升 Standard。
- 新依赖、外部集成或通用组件才调用 `reuse-research`；安装器、Agent 配置、外部输入或敏感操作调用
  `security-review`。

## 输出

超出预计范围必须更新 Route Card 后继续。完成后读取 `evidence-closeout.md`，留下紧凑但可复现的证据。
