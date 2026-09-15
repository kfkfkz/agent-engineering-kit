---
name: design-pipeline
description: "设计文档工程流水线：从已冻结的需求分析出发，编排 概要设计→(UI设计)→详细设计(含API设计/数据库设计)→任务清单+测试方案 的迭代式生成与评审——Author 生成 → doc-gate 硬校验 → AI 评审出 issues → doc-gate 确定性判定 → 冻结进入下一阶段；阶段通过后回填需求索引行。适用于新功能链 ③④⑤ 阶段的文档产出；不用于编码本身（编码走 tdd/repo-delivery）。"
---

把需求文档从"人给定的输入"推进到"冻结可编码的设计"。本技能只负责**编排**；
裁决由 `.repo-memory-kit/bin/doc-gate`（确定性）承担——**你不判 PASS，issues 也不由你裁决**。

## 统一原则

```
LLM 不做最终裁决
LLM 不产生不可验证状态
LLM 只产生结构化观察和候选内容
```

目录结构（原始需求与设计文档分离——`NNN-名称` 两处同名）：

```
docs/01-需求/NNN-名称/     原始需求.md + 原始材料（维护者产物，不进门禁）
docs/03-SDD/NNN-名称/     需求分析/概要/UI/详细/API/数据库/任务/测试/联调/终稿 + reviews/
```

阶段顺序（前一阶段未冻结不进入下一阶段；概要的功能点含界面三件套时插入 UI设计）：

```
需求分析（维护者冻结）→ 概要设计 → (UI设计) → 业务流程设计（人工评审！）
                      → 详细设计(详细+API+数据库三文档) → 计划(任务清单+测试方案)
```

## 流程

### 1. 定位与取上下文

1. 解析 SKILL.md 真实路径（跟随符号链接），向上三级为 kit 仓库根——**模板在
   `templates/requirements/`**（原始需求→01-需求；需求分析/概要/UI/详细/API/数据库/
   任务/测试/联调/终稿→03-SDD）。
2. 目标 SDD 目录 `docs/03-SDD/NNN-名称/`（用户指定或对话中最近的目标）；
   原始需求在同号 `docs/01-需求/NNN-名称/`——生成需求分析前先读它，不脱离原始需求加戏。
3. 生成前先取证：`memory-recall` 检索相关坑/决定/playbook；读业务域地图确认模块口径；
   `codebase-memory` 定位既有能力与调用链——**禁止凭记忆编造现状**。
4. 运行 `.repo-memory-kit/bin/doc-gate status <SDD目录>` 了解各阶段当前状态。
   需求分析若已签写冻结记录但无凭证（status 显示 未冻结/未过 freeze），提醒
   维护者运行 `.repo-memory-kit/bin/doc-gate freeze <SDD目录> --by <名>`
   落哈希凭证——文本"已冻结"不构成冻结凭证，冻结后改动会被哈希检出。
   **冻结时机提醒**：需求分析冻结前，向维护者确认两件事——开发人员、需求上线
   时间（回填 `docs/01-需求/README.md` 索引表自己的行）。

### 2. Author：按模板生成阶段文档

- 骨架从模板复制；**不适用的章节写 N/A + 原因，不删节**（结构稳定是确定性校验的前提）。
- 接地规则：设计令牌/文案/菜单树取自真实源码与运行环境；复用能力一句话引用；
  现状描述必须有代码证据。
- ID 规范（doc-gate 机械解析，格式必须精确）：
  - 需求分析：`场景1`（前置/操作/结果）、验收项 `- [ ] A1：…（场景 1）`
  - 概要设计：`### F1：…` + `覆盖验收项：A1`
  - UI设计：`### PAGE-1：…（对应 F1 / A1）`
  - 详细设计三文档共用 D-ID（全局唯一）：详细设计.md 业务逻辑 `### D1：…（对应 F1 / A1）`；
    API设计.md 接口 `### D2：…（对应 F1 / A1）`；数据库设计.md 变更 `### D3：…（对应 F1 / A1）`
  - 任务清单：`### T1：…` + `对应设计：D1、TC-1` + `预计修改范围：<glob>`
  - 测试方案：用例行 `| TC-1 | … | A1 | …`
- 汇总矩阵（验收覆盖表/需求→任务映射）**不手写**——doc-gate 生成 traceability，双写必漂移。

### 3. 硬校验循环

```bash
.repo-memory-kit/bin/doc-gate check <SDD目录> --stage <阶段>
```

有 FAIL → 按 `hard-checks.json` 的 group/name **定向修复**（不重写全文），重跑直到全过。
薄输入拒绝：需求分析场景/验收还是占位符 → 停止生成，向用户报告"需求意图不完整，
请维护者补齐原始需求或口径"——不带空意图往下游走。

### 4. AI Reviewer（sensor——不是你裁决）

用**未参与生成的独立子 Agent**（可用时）做评审；不可用时清空假设做第二遍，
并在 issues.json 中写 `"reviewer": "same-context"`（披露）。

评审输入：阶段文档全文 + `reviews/hard-checks.json` + 上游冻结文档 + **分层取证**——**已验证事实直接采信**（业务域地图口径、已确认记忆条目、上游阶段评审已核实的声明与取证），**只对新增/未覆盖/可疑的声明做代码查证**（`codebase-memory`）。**评审不是文档对文档，是文档对事实**：内部自洽但现状失实的设计必须被打回。不从零核查——已有结论引用来源即可，重复推导是浪费且引入新噪声。
评审输出**仅** `reviews/<阶段>.issues.json`（无总分、无 PASS/FAIL、不改文档正文）：

```json
{
  "reviewer": "independent-subagent",
  "stage": "详细设计",
  "doc_hashes": {"详细设计.md": "<sha256——评审时计算；门禁核对，文档改过即评审作废>"},
  "issues": [
    {"id": "REV-001", "severity": "blocker|major|minor",
     "category": "grounding|requirement_gap|consistency|verifiability|risk|upstream",
     "location": "§数据与存储/D2", "problem": "…", "evidence": "…",
     "required_change": "…", "upstream": false}
  ]
}
```

**字段硬约束**（doc-gate 拒绝不合规文件）：`stage` 必须匹配当前阶段；
`doc_hashes` 必须覆盖该阶段全部文档（sha256sum 计算）——评审后文档再改动，
评审即过期，必须重评；每条 issue 必含 id/severity/location/problem/
required_change，severity 只允许 blocker/major/minor。

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
每条 issue 必须含 location/problem/evidence/required_change——禁止"建议完善"式空话。

### 5. 门禁判定

```bash
.repo-memory-kit/bin/doc-gate gate <SDD目录> --stage <阶段>
```

- **PASS（退出码 0）** → 文档冻结（gate.json 记录哈希）→ **回填索引行**：更新
  `docs/01-需求/README.md` 中本需求的"当前功能开发阶段"列（只动自己的行——
  多人协作下公共索引冲突面最小）→ 进入下一阶段。
- **NEEDS_REVISION（1）** → 只修 gate.json 指定的问题（REV-ID/hard 项），回步骤 3。
  **定向修改，禁止借机重写全文**——防设计漂移。
- **BLOCKED（2）** → 连续 3 轮不达标。停止迭代，向用户汇总：已试轮次、未解决问题、
  建议的人工裁决点。**不得通过反复重试碰运气**。

### 6. 业务流程设计（人工评审关口）

概要设计 PASS 后生成 `业务流程设计.md`——**给人读的文档**：通俗、无代码/
表结构/接口字段（细节在 数据库设计/API设计）；按功能五件套展开（界面[嵌 UI
原型截图]/业务逻辑/性能/安全/三方依赖）与端到端闭环叙述。

**业务流程图用 archify 产出**（已随 kit 安装于 `.claude/skills/archify/`，需
Node.js ≥18；doctor 报 archify 降级时回退 mermaid 并在文档中披露）：

```bash
cd <SDD目录>/diagrams
# 1. 写 typed JSON IR（对照 .claude/skills/archify/schemas/ 对应类型 schema
#    + examples/ 同型示例——用示例的字段形状，不用它的事实）
# 2. 校验（receipt 全过才继续）
node ../.claude/skills/archify/bin/archify.mjs validate workflow <名称>.json --quality showcase --json
# 3. 交付（编译为自包含 HTML，生成 receipt）
node ../.claude/skills/archify/bin/archify.mjs deliver workflow <名称>.json <名称>.html
# 4. 视觉校验——同时自动产出证据截图（浅/深双主题 × 1440/2048 双尺寸）
node ../.claude/skills/archify/bin/archify.mjs visual-check <名称>.html --json
```

产物：`diagrams/<名称>.json`（权威可编辑源）+ `.html`（可交互交付物——
**读者可在页面上直接导出 PNG/SVG/WebM**）+ visual-check 证据截图
（`<名称>.visual-check.1440x900.light.png` 等——文档嵌入用 light 版）。
本机无 Chrome 时 visual-check 报 skipped——文档只嵌 HTML 引用不嵌图，如实披露。**校验边界**：archify validate
只证明图能正确渲染/结构合法，不证明架构事实正确——事实仍由代码取证与门禁负责。

生成后 `doc-gate check --stage 业务流程设计`（结构）→ **停下交维护者/业务方
人工评审**（评审意见逐条处理并回写修订记录；这是本阶段的门禁——无 AI 评审）→
评审通过（评审记录：通过）后 `doc-gate freeze --stage 业务流程设计 --by <评审人>`
落哈希凭证，方可进入详细设计。

### 7. 打回上游（口径割裂处理）

评审中明确的上游问题（需求不可实现/场景缺口）：在 issues.json 标 `upstream: true`
（不计入本阶段阈值），单独整理**待维护者裁决清单**——你**不改原始需求和需求分析的
冻结口径**（维护者裁决）。维护者更新并重新冻结后：受影响阶段全部重过门禁
（哈希失配会强制暴露）。

### 8. 完成与交接

全部阶段冻结后：`doc-gate status` 确认 → 交接编码（`tdd` 按 任务清单 的纵向切片，
交付链进入 ⑥，收口走 `delivery-gate` 代码形态）。小需求不强制走全链——但走了的
阶段必须过门禁，没有"半冻结"状态。

## 来源

迭代式评审、sensor/judge 分离与有界修订循环改编自 kit 设计文档
`docs/design/document-pipeline.md`（ChatGPT 方案 + Claude 修正综合）。
