---
name: spec-migrate
description: "检查并迁移已有 Spec Kit 文档到统一交付字段，保留原文、补结构化待核验章节并更新未来模板。适用于仓库已有 .specify/specs，或用户要求迁移/体检 Spec Kit 文档；不会凭空补业务事实。"
---

# Spec Kit 文档迁移

迁移器负责结构发现与安全增量，Agent 负责语义核验。禁止整篇重写已有 spec，也不能把待核验占位当作完成设计。

## 命令

从技能真实路径向上三级定位仓库，优先使用目标仓库安装的脚本：

```bash
.repo-memory-kit/bin/spec-migrate --check .
.repo-memory-kit/bin/spec-migrate --dry-run .
.repo-memory-kit/bin/spec-migrate --apply .
.repo-memory-kit/bin/spec-migrate --apply --feature 001-demo .
.repo-memory-kit/bin/spec-migrate --dry-run --layout sdd --sdd-version V1.0 --feature 001-demo .
.repo-memory-kit/bin/spec-migrate --apply --layout sdd --sdd-version V1.0 --feature 001-demo .
```

- 默认或 `--check`：只检查字段覆盖、缺失核心文档和待核验项（含 SDD 结构中遗留的待核验标记）；
- `--dry-run`：展示会修改哪些 feature 与模板，不写文件；
- `--apply`：只有用户明确要求迁移时使用。它先全局预检，任一 feature 缺 `spec.md`、`plan.md` 或 `tasks.md` 都阻断，避免半迁移；
- `--feature <目录名>`：只处理指定 feature（可重复传入）；`install.sh --migrate-specify=名字1,名字2` 与之等价。用户只要求迁移部分 feature 时必须使用，不要全仓 apply；
- `--layout sdd --sdd-version <版本目录名>`：迁移为团队 SDD 目录结构（默认根 `docs/03-SDD/`，可用 `--sdd-root` 改）：`spec.md→02-需求/NN-功能名.md`、`plan.md→03-架构设计`、`tasks.md→06-实现计划`、`data-model.md→04-详细设计/N-功能名/03-数据库设计.md`、`quickstart.md→07-验证指南.md`、`contracts/→02-API设计/契约/`、`research/baseline→07-原始需求材料`，图片类文件进 `04-详细设计/N-功能名/images/`，未知辅助文件进详细设计区保留原名。**同一槽位存在多份文档时归入 `NN-槽位/` 子目录并在内部编号**（如 `02-API设计/01-前端对接.md`、`02-API设计/契约/`），不并列多个 `NN-` 前缀的同号文件——语义核验阶段整理自定义文档时遵守此规则。`01-功能设计`/`04-流程设计`/`05-验收标准` 落结构占位，占位提示软引用项目详细设计模板（如项目的详细设计技能/模板），不复制模板正文。**apply 必须带 `--feature`**（防整仓移动），源 feature 目录移动后移除，功能中文名取自 spec.md 一级标题。等价的安装器入口：`install.sh --migrate-specify=001-demo --sdd-layout --sdd-version V1.0 <目标>`。

执行前查看版本控制状态，保护用户未提交修改。二进制附件、图片、表格、原型和非 Markdown 设计不自动变更，只在报告中列为人工核验范围。

## 迁移后的语义核验

迁移器会保留旧正文并添加 `agent-engineering-kit:migration-pending:*` 标记（附 `> 状态：待核验` 行，与项目文档的状态头约定一致）。字段判定只认核心三文件的标题和 `quickstart.md` 的存在性；research、contracts、checklists 等辅助文档是证据来源，不作为字段已覆盖的依据。逐 feature 读取当前文档、权威设计、代码和测试，把已有事实归并到这些字段：

`目标与范围 | 当前证据/根因与复用依据 | 行为与验收 | 设计/契约/数据流 | 兼容性与风险 | 安全与威胁模型 | 测试接缝与用例 | 实施任务 | 验证结果 | 文档与记忆影响`

能从证据确认的内容写回原章节；需要业务选择或人工确认的内容保持待核验并明确问题。只有语义已整合且项目要求的人工确认完成后，才能移除对应 pending 标记和提示。不要新建“补充设计”来绕过旧文档冲突。

迁移完成标准：`--check` 报告的 `migration-pending` 与 SDD 待核验计数归零；残留未清的迁移不视为完成。

未来新 feature 由 `.specify/templates/*-template.md` 中的受管覆盖层承接这些字段；项目自定义模板正文保持不变。

## 输出

报告已检查 feature、缺失字段、待核验项、实际修改、未处理附件、冲突及下一步。迁移完成不等于设计已审核。
