---
name: design-pipeline
description: "设计文档工程流水线：从已冻结的需求分析出发，编排 概要设计→(UI设计)→详细设计(含API设计/数据库设计)→任务清单+测试方案 的迭代式生成与评审——Author 生成 → doc-gate 硬校验 → AI 评审出 issues → doc-gate 确定性判定 → 冻结进入下一阶段；阶段通过后回填需求索引行。适用于新功能链 ③④⑤ 阶段的文档产出；不用于编码本身（编码走 tdd/repo-delivery）。"
---

把需求文档从"人给定的输入"推进到"冻结可编码的设计"。本技能只负责**编排**；
裁决由 `.repo-memory-kit/bin/doc-gate`（确定性）承担——**你不判 PASS，issues 也不由你裁决**。

本技能只由任务 Route Card 的 Standard（确有正式设计需要）或 Initiative 路线调用。
Direct/Bounded 不为满足仪式进入完整流水线；Bounded 把验收、范围、决策、风险和验证写入
项目已有计划/issue/spec 载体即可。治理 profile 为 strict 也不会单独触发本技能。

## 调用入口

- Claude Code：`/design-pipeline <SDD目录>`
- Codex：`$design-pipeline <SDD目录>`

通常由 `repo-delivery` 内部路由，无需用户手工串联；只有任务已结束、跨会话恢复或客户端
不能在原任务续跑时，才把上述当前客户端对应的 canonical skill 作为用户可见继续入口。

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

阶段顺序由冻结的 ArtifactPlan 决定。以下是完整拓扑，不是“每次都做”的固定清单；标记
`skipped` 的 Artifact 不生成、不评审、不冻结，后继阶段使用计划解析出的最近参与前置：

```
需求分析（维护者冻结）→ 概要设计 → (UI设计) → 业务流程设计（人工评审！）
                      → 详细设计(详细+API+数据库三文档) → 计划(任务清单+测试方案)
```

### 0. 冻结本次 ArtifactPlan

仅 Standard/Initiative 进入本技能。首次为该 SDD 工作且 `reviews/artifact-plan.json` 不存在时，
Agent 使用 Route Card 已冻结的路线与任务起点 Git commit，内部依次执行 `plan-prepare` 和
`plan-freeze`；不得让用户手工运行命令。计划必须来自当前 Git 可见范围的内置 facts producer：
扫描不能完整证明 false 时保持 unknown 并增加产物，禁止由 LLM 口头声明 false 来减少文档。

读取机器返回的 plan items，建立本轮唯一产物集合：`required` 需要生成；`skipped` 只保留
计划中的 reason/evidence，不把模板占位文件当成待完成产物。阶段全部 skipped 时直接越过；
部分 skipped（例如 API/数据库设计）时，硬校验、评审输入和冻结只覆盖参与文档。每个阶段
PASS 后原有门禁入口会自动建立 plan binding；状态必须为 `FROZEN/BOUND` 才能进入真实后继。
残缺 plan、credential、binding、事务日志或 STALE/INVALID 一律停止并修复/显式回滚，绝不
回退 legacy-static 猜测。已有且无动态 sidecar 的 1.0 文档继续走 legacy-static，不强制迁移。

## 流程

### 人工决策与机械执行分离

需求确认和业务流程评审的**结论**必须由维护者/业务方给出，但本地文件与 CLI 操作由 Agent
完成。尚未确认时只询问是否通过和签核人（以及本阶段必需的开发人员/上线时间），不要把
命令当作问题答案。用户对当前明确版本确认后，由 Agent 写入签核记录并完成内部冻结，
检查成功后自动进入下一阶段。
不得把 `doc-gate` 命令列为“你的下一步”，也不得要求用户手工串联 `init/check/gate/status`
等本地 CLI。只有命令需要超出已授权范围，或本地执行在申请权限/重试后仍受阻时，才说明
阻塞原因。**用户可见的继续入口只能是 canonical skill**：当前任务可继续时自动续跑；需要
重新触发时，Claude Code 提示 `/design-pipeline <SDD目录>`，Codex 提示
`$design-pipeline <SDD目录>`，不能用内部 CLI 代替 skill 入口。
不得从沉默、模糊回复或旧评审记录推断本轮已批准。

**用户提示零 CLI 泄漏**：面向用户的确认问题、阶段汇报和“下一步”
不得出现 `doc-gate`、`.repo-memory-kit/bin/`、`--stage` 或 `--by`，也不描述“我将执行某 CLI”。内部命令只出现在
Agent 的工具调用中；仅当用户明确要求诊断底层命令时才展示。人工评审节点使用以下话术：

`请确认本轮业务流程设计是否通过，并提供评审人姓名；确认后我会继续当前 design-pipeline 流程并进入详细设计。`

若当前任务不能续跑，再追加当前客户端的 skill 入口；不要追加内部实现命令。

### 1. 定位与取上下文

1. 解析 SKILL.md 真实路径（跟随符号链接），向上三级为 kit 仓库根——**模板在
   `templates/requirements/`**（原始需求→01-需求；需求分析/概要/UI/详细/API/数据库/
   任务/测试/联调/终稿→03-SDD）。
2. 目标 SDD 目录 `docs/03-SDD/NNN-名称/`（用户指定或对话中最近的目标）；
   原始需求在同号 `docs/01-需求/NNN-名称/`——生成需求分析前先读它，不脱离原始需求加戏。
   需求已指派但 SDD 目录不存在（或索引行缺失）→ Agent 运行
   `doc-gate init NNN-名称 --repo <仓库根>`（登记单一入口：两处目录+模板骨架+索引行，幂等）。
3. 生成前先取证：使用 `memory-recall --context-json` 并绑定当前 Route、subject digest、稳定
   session ID 检索相关坑/决定/playbook，只读取返回的 expanded 正文；读业务域地图确认模块口径；
   `codebase-memory` 定位既有能力与调用链——**禁止凭记忆编造现状**。
4. 运行 `.repo-memory-kit/bin/doc-gate status <SDD目录>` 了解参与阶段的当前状态；`SKIPPED`
   是冻结计划授权的正常终态，不补模板、不伪造 gate。
   需求分析若已签写冻结记录但无凭证（status 显示 未冻结/未过 freeze），Agent 从记录读取
   签核人并运行 `.repo-memory-kit/bin/doc-gate freeze <SDD目录> --by <名>` 落哈希凭证——
   文本"已冻结"不构成冻结凭证，冻结后改动会被哈希检出。若当前文档在既有凭证后有改动，
   必须先展示本轮差异并重新取得确认；不要自动沿用旧签核。
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
  - 任务清单：`### T1：…` + `对应设计：D1、TC-1` + `发布阻塞：是|否` +
    `预计修改范围：<glob>`；缺省按“是”。只有冻结计划在实现前明确标为“否”的任务，
    执行记录才可写 `延期/DEFERRED`，且仍须填写实际范围与验证证据；Agent 不得在执行期
    临时把阻塞任务降级为非阻塞。
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

硬校验通过后，Agent 内部运行
`.repo-memory-kit/bin/doc-gate review-input <SDD目录> --stage <阶段> --json`。
由 ReviewService 决定 `request.mode=full|incremental`：只把 `request.full_documents`
或 `request.section_documents`（二者恰有一个非空）和 `request.supporting_context` 送给 reviewer，
不得自行扩缩章节。解析缺口、标题/引用/依赖变化或旧证明不可用会自动选择 full；receipt
失配时重新生成输入并按工具结论全文重送，不能继续沿用旧评审。

Reviewer 在这份受控输入上做**分层取证**：已验证事实直接采信（业务域地图口径、已确认
记忆条目、上游阶段已核实的声明与取证），只对新增/未覆盖/可疑声明做代码查证
（`codebase-memory`）。评审不是文档对文档，而是文档对事实；内部自洽但现状失实的设计
必须被打回。不从零重复核查已有结论。

评审输出 `reviews/<阶段>.issues.json`，并把 review-input 返回的 `receipt` 原样写入
`reviews/<阶段>.review-receipt.json`。receipt 只证明实际送审输入，不代表 PASS；不得手工改
request/receipt digest。issues 无总分、无 PASS/FAIL、不改文档正文：

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

仅当 ArtifactPlan 中 `business-flow` 为 required 时执行本节；若为 skipped，不生成流程图、
不询问人工评审，直接沿解析后的真实前置进入后继阶段。

概要设计 PASS 后生成 `业务流程设计.md`——**给人读的文档**：通俗、无代码/
表结构/接口字段（细节在 数据库设计/API设计）；按功能五件套展开（界面[嵌 UI
原型截图]/业务逻辑/性能/安全/三方依赖）与端到端闭环叙述。

**业务流程图用 archify 产出**（已随 kit 安装到 `.agents` 与
`.claude` 两棵技能树，需 Node.js ≥18）。先从**仓库根**定位 CLI，
不得用相对 SDD 目录的 `../.claude`：

```bash
REPO_ROOT="<仓库根绝对路径>"
SDD_DIR="<SDD目录绝对路径>"
ARCHIFY_ROOT="$REPO_ROOT/.agents/skills/archify"
[ -f "$ARCHIFY_ROOT/bin/archify.mjs" ] || \
  ARCHIFY_ROOT="$REPO_ROOT/.claude/skills/archify"
ARCHIFY_CLI="$ARCHIFY_ROOT/bin/archify.mjs"
[ -f "$ARCHIFY_CLI" ] || { echo "archify skill 未安装" >&2; exit 1; }

node "$ARCHIFY_CLI" doctor
mkdir -p "$SDD_DIR/diagrams"
cd "$SDD_DIR/diagrams"
# 1. 写 typed JSON IR（对照 "$ARCHIFY_ROOT/schemas/" 对应类型 schema
#    + examples/ 同型示例——用示例的字段形状，不用它的事实）
# 2. 校验（receipt 全过才继续；成功后原子发布回执）
node "$ARCHIFY_CLI" validate workflow <名称>.workflow.json \
  --quality showcase --json > <名称>.validate.json.tmp && \
  mv <名称>.validate.json.tmp <名称>.validate.json
# 3. 交付（编译为自包含 HTML，持久化可核验 receipt）
node "$ARCHIFY_CLI" deliver workflow <名称>.workflow.json <名称>.html \
  --quality showcase --json > <名称>.delivery.json.tmp && \
  mv <名称>.delivery.json.tmp <名称>.delivery.json
# 4. 视觉校验——同时自动产出证据截图（浅/深双主题 × 1440/2048 双尺寸）
node "$ARCHIFY_CLI" visual-check <名称>.html --json
```

产物：`diagrams/<名称>.workflow.json`（权威可编辑源）+ `.html`
（可交互交付物）+ `.validate.json` + `.delivery.json`（内容哈希与
9/9 校验回执）+ visual-check 证据截图
（`<名称>.visual-check.1440x900.light.png` 等——文档嵌入用 light 版）。
**读者可在 HTML 页面上直接导出 PNG/SVG/WebM**。
本机无 Chrome 时 visual-check 报 skipped——文档只嵌 HTML 引用不嵌图，如实披露。

**降级不得猜测**：只有 `node` 不存在/主版本 <18，且 doctor 明确报
`DEGRADED` 时才允许 Mermaid；文档必须写明“图表模式：Mermaid 降级”与
具体原因。`EPERM`、`MODULE_NOT_FOUND`、validate/deliver 非零都是交付
错误；在 Codex 受限沙箱遇到子进程 `EPERM` 时申请权限后重试，
不得偷换为 Mermaid。**校验边界**：archify validate
只证明图能正确渲染/结构合法，不证明架构事实正确——事实仍由代码取证与门禁负责。

生成并完成内部结构检查后，**停下交维护者/业务方人工评审**（评审意见逐条处理并回写
修订记录；这是本阶段的门禁——无 AI 评审）。面向用户只发送固定话术：

`请确认本轮业务流程设计是否通过，并提供评审人姓名；确认后我会继续当前 design-pipeline 流程并进入详细设计。`

收到当前版本的明确“通过”与评审人姓名后，按以下内部状态转换执行，禁止向用户复述其中的
机械步骤或命令名：

```text
persist_review(conclusion="通过", reviewer, reviewed_at, resolved_comments)
freeze_stage(stage="业务流程设计", signer=reviewer)
assert_stage_frozen(stage="业务流程设计")
continue_to(stage="详细设计")
```

### 7. 打回上游（口径割裂处理）

评审中明确的上游问题（需求不可实现/场景缺口）：在 issues.json 标 `upstream: true`
（不计入本阶段阈值），单独整理**待维护者裁决清单**——你**不改原始需求和需求分析的
冻结口径**（维护者裁决）。维护者更新并重新冻结后：受影响阶段全部重过门禁
（哈希失配会强制暴露）。

### 8. 完成与交接

全部参与阶段为 FROZEN、计划跳过阶段为 SKIPPED 后：`doc-gate status` 确认 →
返回 `repo-delivery`，由总编排按 Route Card
进入 ⑥；需要编码时由它调用 `tdd` 按任务清单做纵向切片，最后由它调用
`delivery-gate` 收口。若本技能是被单独显式调用的，只向用户建议当前客户端对应的
canonical 入口（Claude Code：`/repo-delivery`；Codex：`$repo-delivery`），不要把 `tdd`、
`delivery-gate` 或阶段名称冒充为并列的“下一步 skill”。小需求不强制走全链——但走了的
阶段必须过门禁，没有“半冻结”状态。

计划阶段 PASS 建立的是**执行前计划基线**：任务定义、预计范围、验证方式保持冻结；进入
实现后允许任务清单更新执行记录和 checkbox 状态。它们不是设计漂移，也不要求逐任务重跑
计划门禁；完整终态由后续 `delivery-gate` 在统一收口时冻结。

## 来源

迭代式评审、sensor/judge 分离与有界修订循环改编自 kit 设计文档
`docs/design/document-pipeline.md`（ChatGPT 方案 + Claude 修正综合）。
