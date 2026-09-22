# Initiative 路线

本文件是 `repo-delivery.route.initiative` 的唯一权威；使用前还必须完整读取 `standard.md`。

## 适用条件

多仓库、较长多会话、多团队/并行工作流或需要 WorkUnit 级集成治理。单仓库复杂改造通常仍是
Standard，不能因文件多或工具调用多而误升 Initiative。

## 分阶段交付

1. 完整 SDD 冻结目标、非目标、跨域能力、外部契约、数据流、风险和验收。
2. 将计划拆成可独立交付、独立回退、带明确依赖的 WorkUnit；每单元保留自己的 Route Card、验证和回执。
3. Standard 规则适用于每个 WorkUnit；共享决定只在一个权威位置维护，其他单元引用 digest/anchor。
4. 每阶段维护 Context Capsule，只放摘要、anchor 和 source reference，不复制上游全文；来源变化即重建。
5. 并行工作必须声明文件/模块所有权，不覆盖他人改动；跨单元接口先契约测试，再做集成门禁。
6. 多会话中断使用 `task-handoff` 保存目标、状态、文件、验证、风险和唯一下一步。

## 集成与发布

单元完成不等于项目 READY。最后执行跨 WorkUnit 兼容、安装/迁移、性能容量、安全、回滚和端到端
集成验证；未完成项、延期 Beta 和人工门禁分别披露，不能用平均分掩盖一个单元回退。
