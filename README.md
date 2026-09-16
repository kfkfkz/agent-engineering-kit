# agent-engineering-kit

给 Claude Code、Codex 和其他仓库级 AI Agent 使用的仓库内工程控制面。它不只是
一组 Prompt/Skill：安装器负责受管资源生命周期，`doc-gate` 与
`governance-eval` 负责确定性裁决，MCP 提供受约束的工具入口，benchmark 用机械指标
评估 Agent 是否真的改善了交付结果。

安装一次后，团队成员会使用同一套需求与设计文档流水线（确定性门禁 + 人工评审关口）、
项目宪章、复用调研、代码取证、系统调试、TDD、安全审查、交付验证和项目记忆规则，
减少“不同 Agent 各写一套设计、各走一套流程”的偏差。AI 负责提出候选和评审问题，
脚本负责可重复判定，维护者保留需求确认、业务流程签核和高风险决策权。

## 当前架构

| 层 | 主要组件 | 保证什么 |
| --- | --- | --- |
| 工作流层 | `repo-delivery`、`design-pipeline`、TDD、安全审查等 Skills | 统一 Agent 的任务路由、取证、实现和交付顺序 |
| 确定性门禁层 | `doc-gate`、`governance-eval`、`domain-check` | 文档追踪链、冻结凭证、策略阈值和 diff 治理可机械复现 |
| 生命周期层 | Python 安装器、Manifest v2、事务日志、平台后端 | 安装、更新、修复、恢复和卸载不依赖“脚本刚好跑完” |
| 能力与证据层 | MCP、项目记忆、archify、benchmark、验证回执 | 结构化工具访问、知识复用、图表交付和效果评测 |

这次架构升级已经把几个原本独立的脚本边界收进同一模型：Manifest、治理状态和合法
v1 清单退役都进入内部事务；Codex workspace 的创建/删除以外部事务步骤记录意图并可恢复；
文档初始化采用排他锁、原子发布和失败回滚；策略文件与 MCP 请求按 schema fail-closed；
Git 中文/特殊路径使用机器安全的解析方式；Windows 默认使用 copy 策略，不要求开发者模式或
符号链接权限。

## 三分钟接入

### 1. 安装到项目

```bash
git clone https://github.com/kfkfkz/agent-engineering-kit.git
cd agent-engineering-kit
./install.sh /path/to/your-project
```

安装是幂等的，可以重复执行。它默认不联网，也不会覆盖项目已有的记忆条目；只有显式
`--with-codebase-memory` 会调用外部安装器。安装器需要本机有 **Python 3.10+**。

| 平台 | 默认 workspace 策略 | 安全档 | CI 覆盖 |
| --- | --- | --- | --- |
| Linux / macOS / WSL | `symlink` | `strict`：`dir_fd + O_NOFOLLOW`，删除前原子隔离 | Ubuntu 全套件；POSIX 生命周期与故障注入 |
| Windows 原生 | `copy` | `compatible`：path-based reparse-point 预检 | Windows import + install / update / doctor / uninstall |

Windows 的 `compatible` 后端不能提供 POSIX 后端针对恶意本机并发进程的完整 TOCTOU
保证；存在此威胁模型时使用 WSL。普通 Windows 团队不需要开启符号链接权限。

先预览、不写入：

```bash
./install.sh --dry-run /path/to/your-project
```

如果希望同时安装或配置代码图谱 MCP：

```bash
./install.sh --with-codebase-memory /path/to/your-project
```

`--with-codebase-memory` 会联网使用 [DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) 的官方安装器；机器上已有该命令时只执行其配置入口。

### 2. 开始工作

Claude Code：

```text
/repo-delivery 实现订单导出功能
```

Codex：

```text
$repo-delivery 实现订单导出功能
```

也可以直接用自然语言描述任务。非平凡的功能、缺陷修复、重构和迁移会由 Agent 自动进入交付工作流。

### 3. Codex 从 workspace 根启动时

Codex 只会从当前目录向上发现 `.agents/skills`。如果日常在 workspace 根目录启动，而项目是其子目录，请这样安装：

```bash
./install.sh --codex-root /path/to/workspace /path/to/workspace/your-project
```

`--link-strategy auto` 是默认值：POSIX 在 workspace 建立指向目标项目技能的受管符号链接，
Windows 建立带内容血统校验的受管副本。也可以在任一平台明确选择：

```bash
# 不依赖 symlink；适合 Windows，也可用于受限的 POSIX 环境
./install.sh --codex-root /path/to/workspace --link-strategy copy /path/to/project
```

更新会核验或补建这些 workspace 产物；卸载只删除仍与记录血统一致的链接/副本，用户修改过的
内容会进入冲突状态并保留现场。

## 日常怎么用

通常只需要记住 `repo-delivery`。它会根据任务类型调用其他技能，并复用项目已有的 Spec Kit、SDD、版本文档或变更记录。

| 场景 | Claude Code | Codex | 作用 |
| --- | --- | --- | --- |
| 完整功能、修复、重构、迁移 | `/repo-delivery 任务` | `$repo-delivery 任务` | 从宪章门禁推进到实现、验证和文档收口 |
| 设计文档流水线（03-SDD 目录全链） | `/design-pipeline SDD目录` | `$design-pipeline SDD目录` | 概要→(UI)→业务流程(人审)→详细→计划：doc-gate 门禁 + archify 流程图 |
| 理解结构、调用链、影响面 | `/codebase-memory 问题` | `$codebase-memory 问题` | 用代码图谱取证并核验源码与索引覆盖 |
| Bug、测试失败、异常行为 | `/systematic-debugging 问题` | `$systematic-debugging 问题` | 先复现和定位根因，再决定修复 |
| 测试先行实现 | `/tdd 行为` | `$tdd 行为` | 按纵向切片执行 RED → GREEN → REFACTOR |
| 新依赖、集成或技术选型 | `/reuse-research 需求` | `$reuse-research 需求` | 比较采用、扩展、组合和自建，记录证据 |
| 漏洞与配置风险 | `/security-review 范围` | `$security-review 范围` | 检查应用数据流与 Agent 配置攻击面 |
| 交付收口门禁 | `/delivery-gate` | `$delivery-gate` | diff 审查、治理引擎裁决、对抗复核与验证闭环，结论以验证回执为证 |
| Spec Kit 迁移 | `/spec-migrate` | `$spec-migrate` | 检查/预览，增量迁移或转 SDD 目录结构 |
| 跨会话或 Agent 交接 | `/task-handoff` | `$task-handoff` | 留下可复核、可恢复的任务状态 |
| 巡检项目记忆 | `/memory-check` | `$memory-check` | 检查记忆与当前代码是否一致 |
| 沉淀经验 | `/memory-capture` | `$memory-capture` | 提取候选，经人确认后写入项目记忆 |

推荐直接使用完整入口；只有在目标非常明确时才单独调用专项技能。例如已经有稳定复现的测试失败，可以直接进入 `systematic-debugging`。

## 使用示例

下面用 Claude Code 的 `/` 写法演示；Codex 中把它换成 `$` 即可。用户只需描述目标，不需要手工依次调用所有技能，`repo-delivery` 会完成路由。

### 示例一：开发一个功能

假设需要为订单列表增加 CSV 导出：

```text
/repo-delivery 为订单列表增加 CSV 导出。
要求：
- 沿用当前列表的筛选条件和数据权限
- 单次最多导出 5 万条
- 导出字段为订单号、客户名称、金额、状态、创建时间
- 不改变现有查询接口
```

工作流会按以下顺序执行：

1. 读取项目指令、constitution 和相关记忆，提取安全、依赖、测试、文档及人工确认门禁。
2. **需求拆分**：用 `codebase-memory` 找到订单查询入口、权限过滤、导出能力、调用链和受影响测试；`memory-recall` 检索相关的坑与域知识；把需求拆成功能点并列出待澄清问题。
3. **需求确认**：把"加个导出"衍生为具体场景（如"运营在订单列表页筛选后导出当日对账单"），每个场景定义可观察产出（谁在哪个页面拿到什么）；场景与产出确认后写入需求分析，维护者签核 + `doc-gate freeze` 落哈希凭证；导出方式、异步任务、文件存储等会改公共契约的细节一并交用户裁决。
4. **概要设计**：先对齐项目内同类已认可文档的骨架；功能点（F-ID）覆盖全部验收项；需要新 CSV 组件时用 `reuse-research` 比较现有依赖、扩展点和新库。过 `doc-gate` 门禁后冻结。
5. **业务流程设计 → 详细设计（API/数据库）→ 任务清单 + 测试方案**：经 `design-pipeline` 产出——业务流程设计是**给人读的**（通俗、无代码细节、archify 流程图 + UI 原型截图），交维护者人工评审；通过后进入详细设计三文档。机器门禁阶段每个都过 `doc-gate` 硬校验（结构/追踪链/薄输入）+ AI 评审（独立上下文出 issues.json，绑定文档哈希）+ 确定性判定（severity 阈值），PASS 即冻结；发现缺漏定向修改后重新过门，连续 3 次失败升级人工；每阶段过门后回填索引行的阶段列。
6. **任务拆分 + 编码**：`tdd` 按纵向切片推进——
   - RED：有权限的筛选结果应导出指定字段；
   - GREEN：完成最小导出链路；
   - RED：超过 5 万条时返回已确认的限制行为；
   - GREEN：补齐容量保护；
   - REFACTOR：在测试保持绿色时整理重复代码。

   期间对导出权限、公式注入、敏感字段和临时文件执行 `security-review`。
7. 执行 `delivery-gate`，处理阻塞发现、落验证回执，并把真实实现和验证结果回写原设计文档。
8. 如果形成了可复用经验，调用 `memory-capture` 展示候选，等待人确认后再写入。

一次合格的最终汇报应类似：

```text
完成：订单列表 CSV 导出
范围：新增导出入口并复用现有筛选和数据权限；未修改查询接口
设计：同步流式导出，5 万条硬限制，字段及时间格式已写入原版本设计
测试：权限过滤、字段映射、空结果、数量上限和异常响应均已覆盖
验证：目标测试、相关模块测试、静态检查通过
风险：大数据量耗时仍依赖线上数据分布，已保留指标观察点
文档/记忆：设计已回写；提出 1 条导出容量保护候选，尚待确认
```

如果只希望先完成设计，可以明确限制本轮范围：

```text
/repo-delivery 为订单 CSV 导出完成需求澄清和技术设计，本轮不要编码。
重点比较同步流式导出与异步任务两种方案，并列出需要我确认的决策。
```

### 示例二：修复一个 Bug

假设生产环境偶发重复扣减库存：

```text
/repo-delivery 修复"支付回调重试时偶发重复扣减库存"。
已知现象：同一个支付单收到两次回调后，少量订单会产生两条扣减记录。
期望：同一个业务回调只允许成功扣减一次，并保留正常重试能力。
```

`repo-delivery` 会把它路由到缺陷链：

```text
systematic-debugging 根因定位
→ tdd 回归测试
→ 最小根因修复
→ security-review（支付/幂等风险）
→ delivery-gate（审查 + 验证回执）
→ 设计、故障记录和记忆收口
```

具体过程：

1. 稳定复现或收集足够反证，区分"消息重复""幂等判断失效""事务边界错误"等不同原因。
2. 使用 `codebase-memory` 追踪支付回调、订单状态、库存服务、消息重试和事务调用链。
3. 写出一个单一假设，例如："幂等记录与库存扣减不在同一事务边界，因此并发回调都通过了前置检查。"
4. 用最小实验验证假设；不成立就撤回，不叠加试错补丁。
5. 根因确认后，使用 `tdd` 在稳定接口建立并发回调回归测试，确认 RED 是因为重复扣减而不是测试环境问题。
6. 只修改根因所需代码，再验证单次回调、重复回调、并发回调、失败重试及事务回滚。
7. 更新原故障记录或设计文档，并把真正可复用的坑点作为记忆候选交给人确认。

### 其他常见用法

```text
# 重构：要求行为不变，并给出影响分析和删除旧实现的证据
/repo-delivery 重构通知发送模块，统一邮件和短信的重试策略，不改变外部 API。

# 数据库迁移：先设计兼容、灰度和回滚，再进入实现
/repo-delivery 把订单状态字段迁移为状态码，要求支持新旧版本并行和可回滚。

# 影响分析：只回答结构问题
/codebase-memory 如果修改 OrderService.confirm，会影响哪些入口、消息和测试？

# 对已定位问题测试先行
/tdd 为"优惠券只能核销一次"增加并发回归测试并完成最小实现。

# 功能完成后提取经验；仍需人工确认才会入库
/memory-capture 从本次订单导出交付中提取可复用的坑点和流程。

# 迭代末巡检项目记忆
/memory-check
```

## 统一工作流

```text
repo-delivery
  ├─ 读取项目指令、宪章和相关记忆
  ├─ 判定：直接修改 / 缺陷修复 / 既有变更 / 新功能（发现超预期影响面 → 动态升径）
  ├─ ① 需求拆分：codebase-memory + 业务域地图 + memory-recall 取证
  ├─ ② 需求确认：原始需求（01-需求/）→ 衍生需求场景与产出定义（验收依据），
  │   待澄清问题交人裁决；冻结时确认开发人员与上线时间（回填索引行）
  ├─ ③④⑤ design-pipeline（03-SDD/NNN-名称/）：概要设计 → (UI设计)
  │   → 业务流程设计（人工评审关口——通俗人读文档，嵌 UI 原型与流程图）
  │   → 详细设计(含 API设计/数据库设计) → 任务清单+测试方案
  │     每阶段：doc-gate 硬校验 → AI 评审(issues.json) → 确定性门禁 → 冻结
  │     追踪链：场景 → 验收项A → 功能点F → 设计落点D → 用例TC → 任务T（全程机械校验）
  │     每阶段通过即冻结并回填索引行的"当前功能开发阶段"列
  ├─ ⑥ tdd 编码与验证（systematic-debugging / security-review 按需）
  ├─ delivery-gate：governance-eval 治理裁决 + diff 审查 + 对抗复核 + 验证回执
  └─ 线上联调清单（事实文档）+ 终稿 As-Built → 回写设计文档，提出记忆候选
```

新功能走全链；直接修改与缺陷修复走短路，既有变更从影响面进入——不为小改动强加六道门禁。

工作流只做门禁、路由和收口，不另建一套平行的设计体系：

- 项目 `AGENTS.md`、`CLAU.md` 和 constitution 是约束来源；
- 已有 spec、SDD、版本文档和代码是事实来源；
- 项目专属技术栈、覆盖率、数据禁区等以当前项目为准；
- 高风险决策仍需人工确认，AI 草稿不能自行标记为已确认。

## 设计文档工程（01-需求 / 03-SDD）

需求与设计文档按**产物归属**分两处，同号同名（`NNN-名称`）。登记单一入口：
`doc-gate init NNN-名称 --repo <仓库根>`——一次建好两处目录 + 模板骨架 + 索引行
（幂等；原始需求确认和指派后由维护者运行，SDD 目录同步就绪）：

```text
docs/01-需求/
  README.md                      需求指派索引（8 列：序号/需求/需求状态/当前功能开发
                                 阶段/全局目录名称/开发人员/需求所属版本/需求上线时间）
  NNN-名称/原始需求.md             维护者产物——需求原文与附件（不进门禁，Agent 只引用）

docs/03-SDD/
  NNN-名称/
    需求分析.md        场景（前置/操作/结果）+ 验收项 A-ID    ← 维护者签核 + freeze
    概要设计.md        功能点 F-ID（覆盖验收项）              ← 机器门禁
    UI设计.md          可选：原型 + 设计系统落地               ← 机器门禁 + AI 视觉评审
    业务流程设计.md     人读文档：通俗、无代码细节、嵌流程图    ← 人工评审关口
    详细设计.md        业务逻辑/异常/安全（D-ID 三文档共用）    ← 机器门禁 + AI 评审
    API设计.md         接口契约
    数据库设计.md       表结构与迁移
    任务清单.md        T-ID + 预计修改范围（喂 Change Precision）
    测试方案.md        TC-ID 挂验收项
    线上联调功能验证清单.md  事实文档（"通过"只接受真实证据）
    终稿.md            As-Built（最终状态/设计差异/决策沉淀）
    reviews/           doc-gate 唯一写者（gate 凭证/评审 issues/校验记录）
    diagrams/          archify 产物（JSON 源/HTML/证据截图）
```

**门禁模型（sensor / judge 分离）**：AI 评审只产出 issues.json（无总分、无 PASS/FAIL，
绑定被评审文档哈希——评审后改文档即过期）；`doc-gate` 做全部确定性裁决——
结构/追踪链（场景→A→F→D→TC→T 全程机械校验）/薄输入拒绝/上游冻结检查，
severity 阈值判定，连续 3 次失败 BLOCKED 升级人工。PASS 即冻结（哈希凭证），
冻结后改动即失配。需求分析与业务流程设计是**人工签核阶段**（评审权在人）。

门禁阈值可通过 `.repo-memory-kit/doc-policy.json` 配置：`max_blocker`、`max_major`、
`max_minor` 必须是非负整数，`max_iterations` 必须大于等于 1。文件一旦存在，JSON 损坏、
字段类型错误或出现未知字段都会在写 gate 前阻断，不会静默回退到默认策略。上游文档只要被
编辑且尚未重新冻结，其所有下游凭证立即失效。

**业务流程图由 archify 生成**：写类型化 JSON → validate（9/9 校验）→ deliver
（自包含交互 HTML）→ visual-check（校验 + 自动产出双主题证据截图）。
查看器内置 PNG/SVG/WebM 导出。无 Node.js 时回退 mermaid 并披露。

## 统一产出

无论项目使用哪种模板，最终交付信息都应能映射到以下字段（括号为产出阶段）：

```text
目标与范围（①② 需求拆分/确认）
当前证据 / 根因与复用依据（①）
行为与验收（②④）
设计 / 契约 / 数据流（③④ 概要/详细设计）
兼容性与风险（③④）
安全与威胁模型（③④）
测试接缝与用例（④ 测试与验证设计 → ⑤ 任务级具体化）
实施任务（⑤ 任务拆分）
验证结果（⑥ 编码与验证）
文档与记忆影响（⑥）
```

已有模板时把这些信息写入对应章节，不为了统一外观复制一份新设计。这样不同 Agent 的文件名可以不同，但关键决策、测试和验收信息保持一致。

## 包含的技能

### repo-delivery

统一入口。提取项目宪章门禁，选择足够且最轻的交付路线，复用现有设计载体，并完成实现、验证、文档与记忆收口。

### codebase-memory

负责结构化代码取证。默认按 Verify 等级查询符号、调用链、数据流和影响范围，再回到当前源码、测试及索引覆盖信息核验。

项目 Skill 会始终安装；MCP 运行时是可选增强。运行时缺失时允许普通交付降级为源码检索并披露置信度，但严格记忆巡检会按规则停止，避免用 grep 冒充完整结构核验。

### systematic-debugging

统一故障分析记录：

```text
现象与期望 → 稳定复现/反证 → 根因链 → 影响范围
→ 单一假设 → 最小修复 → 回归验证
```

没有根因证据时不进入试错修复；连续多个假设失败或出现共享状态、散弹修改时，把问题升级为架构候选。

### tdd

围绕调用者可观察行为选择稳定接缝，逐个纵向切片执行 RED、GREEN 和小型 REFACTOR。

除了测试先行，它还会把过度 mock、测试专用浅抽象、散弹修改和复杂测试接口识别为架构反馈。本技能参考并改编自 [mattpocock/skills 的 TDD](https://github.com/mattpocock/skills/tree/main/skills/engineering/tdd) 与 [improve-codebase-architecture](https://github.com/mattpocock/skills/blob/main/skills/engineering/improve-codebase-architecture/SKILL.md)，上游按 MIT License 使用。

### reuse-research

在增加依赖、集成、通用能力或关键技术方案前先查项目内复用点和一手来源，明确采用、扩展、组合或自建，避免没有证据的重复建设。

### security-review

覆盖两类风险：应用代码中的认证授权、注入、SSRF、路径/文件、敏感数据、支付回调与供应链；以及 Agent 指令、skills、MCP、hooks、安装脚本中的提示注入、过宽权限、秘密泄露和危险执行。HIGH/CRITICAL 必须给出可达路径，并由 `delivery-gate` 逆向验证。

### design-pipeline

设计文档工程流水线：从原始需求衍生需求分析（维护者签核冻结）后，编排
概要设计 → (UI设计) → 业务流程设计（**人工评审关口**）→ 详细设计（详细/API/数据库
三文档共用 D-ID）→ 任务清单+测试方案 的迭代式生成与评审。核心分工是
**sensor / judge 分离**——AI 评审（独立上下文）只产出 issues.json（无总分、无
PASS/FAIL，绑定文档哈希），`doc-gate` 做全部确定性裁决。每个阶段 PASS 即冻结，
冻结后改动即哈希失配；连续 3 次失败 BLOCKED 升级人工；每阶段通过后回填
`docs/01-需求/README.md` 索引行的"当前功能开发阶段"列（只动自己的行）。
业务流程设计是人读文档（通俗、无代码细节），评审权在维护者——评审通过后
`doc-gate freeze` 落哈希凭证才能进详细设计。下游发现上游不可实现走打回流程
（出待维护者裁决清单，不代改维护者产物）。任务清单的预计修改范围直接喂
Change Precision 评测。

### archify（图表，vendor 供应链锁定）

业务流程图/架构图/时序图/数据流/生命周期五类图的自包含交互 HTML 渲染器。
以 vendor 快照随 kit 分发（上游 commit 锁定 + 聚合哈希 manifest——安装器生成
规格前校验，篡改拒绝安装），逐文件部署到两棵技能树。运行时需要 **Node.js ≥18**；
缺失时 doctor 报 DEGRADED（图表回退 mermaid，基础功能不受影响）。
产物链：JSON 源（权威可编辑）→ validate/deliver（校验回执）→ visual-check
（有界行为校验 + 双主题证据截图）。查看器内置 PNG/SVG/WebM 导出。
校验边界：validate 只证明图能正确渲染，不证明架构事实正确。

### delivery-gate

代码交付门禁（设计文档门禁已由 design-pipeline 接管）。阶段一：`governance-eval` 治理引擎对 diff 做确定性规则匹配（命中规则 + required_actions），叠加对最终 diff 的正确性、宪章、兼容性、测试、维护性和安全自审，高危发现做对抗式复核（运行环境提供子 Agent 时交给未参与实现的独立审查者），并对照任务清单预计修改范围检查范围蔓延。阶段二从项目自身的 CI、构建文件和宪章提取实际门禁执行验证闭环。结论枚举 `READY`、`NOT READY`、`NEEDS HUMAN REVIEW`，未运行的检查不会被写成通过；验证回执（`docs/delivery-receipts/`）是声称验证通过的唯一证据形态。

### spec-migrate 与 task-handoff

`spec-migrate` 把已有 Spec Kit 文档安全映射到统一字段，保留原文并显式标记待核验内容；`task-handoff` 在中断或跨 Agent/会话时记录目标、证据、修改、验证、风险和唯一下一步。

### memory-check 与 memory-capture

`memory-check` 负责全量或增量巡检；`memory-capture` 负责把已验证的工程经验转成候选条目。候选必须经人确认后才能写入，避免未经验证的 AI 总结污染项目记忆。

## MCP 工具入口

安装器会把 `agent-engineering-mcp` 注册到目标仓库的 Claude/Codex 配置片段。
服务器只绑定安装它的仓库，不接受 Agent 传入任意路径，并暴露四个工具：

- `memory_recall`：语义检索项目文档与记忆；
- `memory_build`：重建、校验索引或计算变更影响；
- `domain_check`：校验业务域地图的登记、状态和证据路径；
- `kit_status`：查看 kit、语义索引和域地图状态。

MCP 实现完整的 initialize 生命周期与 JSON-RPC envelope/参数校验；非法
请求返回结构化错误，notification 不回包，单个工具异常不会终止 stdio 服务。
它委托现有 CLI 实现，不另复制一套业务逻辑。

## 项目记忆（按模块分目录）

记忆条目按**接口入口所属业务模块**分目录存放（模块名以项目业务域地图的登记为准），条目类型在 frontmatter 的 `type` 字段：

```text
docs/memory/
  <模块名>/YYYY-MM-DD-<中文标题>.md   原子条目（pitfall 坑 / decision 决定 / playbook 场景流程）
  RULES.md                            记忆规则（kit 管辖）
  README.md                           用户笔记（纯 seed：首次创建后归用户）
  _PITFALL_TEMPLATE.md 等             三类条目模板 + PROFILE 模板（kit 管辖）
.repo-memory-kit/                      每机本地派生物（不入版本库）
  memory-index.md                     本地索引（人肉浏览用）
  .anchors.json                       符号 → 条目映射（增量巡检/变更影响）
  zvec/                               语义索引（generation + current.json 指针）
```

分层是逻辑概念而非物理目录：

```text
L3  PROFILE.md（项目画像）      会话开始先读：风险域地图/稳定约定/反模式
L2  playbook 条目             场景流程：某功能/环境怎么跑通
L1  pitfall / decision 条目   原子经验：坑（缺陷/陷阱）与决定（背景/取舍/后果）
L0  代码（第一事实源）、业务域地图、spec/SDD、接口文档
```

**匹配首选语义检索**：`memory-recall` 直扫 `docs/**/*.md` 与 frontmatter 建全文索引（zvec jieba 双字段 + RRF），不依赖目录结构——功能点多了以后靠人工翻索引表必然漏召回；`memory-index.md` 只服务人肉浏览。条目 frontmatter 是唯一事实源（`type`/`status`/`module`/`created`/`verified`/`anchors`…），本地索引与锚点表全部由它生成、禁止手改。

常用命令：

```bash
# 语义检索（匹配首选）
.repo-memory-kit/bin/memory-recall "任务描述" .
.repo-memory-kit/bin/memory-recall --list .      # 语料清单（不依赖 zvec）

# 校验条目并重建本地索引/锚点（zvec 可用时语义索引随动刷新）
.repo-memory-kit/bin/validate-memory.sh .
.repo-memory-kit/bin/memory-build .

# 变更影响：当前 git/svn 工作区改动波及哪些记忆（可 --since <ref> / --files）
.repo-memory-kit/bin/memory-build --changed .

# 条目按标题批量重命名（交叉引用联动）；v3 存量类型目录迁入模块目录
.repo-memory-kit/bin/memory-build --rename-by-title .
.repo-memory-kit/bin/memory-build --migrate-to-modules .

# 域地图守卫：索引登记/状态/证据路径核验（回写域条目后运行）
.repo-memory-kit/bin/domain-check .
```

**业务域地图是业务流的登记处**：项目建立业务域地图（如 `docs/02-业务域地图/`）后，开发完/测试完的业务流总结（含跨域端到端链路）、能力入口、调用链登记为域地图条目，不在 playbook 里复制；playbook 只保留场景化操作并以指针链接域地图。事实以代码为准，域地图与代码冲突时修正域地图。

PROFILE 在有效 `confirmed` 条目达到 10 条时创建；后续积累达到提醒阈值时，`memory-build` 会提示重新蒸馏。

## 安装内容与文件所有权

安装器会创建或更新：

```text
.claude/skills/                 Claude Code 项目技能（含 archify/——bin/schemas/renderers 全量）
.agents/skills/                 Codex 项目技能（同上，两棵树内容一致）
.repo-memory-kit/bin/           校验器、生成器、Spec 迁移器、doc-gate 与 governance-eval
.repo-memory-kit/governance     治理 profile 标记（strict / lightweight——唯一权威）
.repo-memory-kit/governance.json  团队治理规则（首次安装写入，此后团队自持）
docs/memory/RULES.md            记忆规则
docs/memory/_*_TEMPLATE.md     条目模板（pitfall/decision/playbook/PROFILE）
docs/memory/README.md           用户笔记种子（首次创建，此后归用户）
CLAUDE.md / AGENTS.md           agent-engineering-kit 托管区块
.cbmignore / .gitignore         图谱索引区块 / 每机文件忽略区块（多人协作）
.mcp.json / .codex/config.toml  MCP 注册（kit 片段，含本机路径）
```

治理双轨：**profile（strict/lightweight）唯一权威在文本标记**，每次安装重写——切换即时生效；
**团队规则在 governance.json**（路径/文件名/新增行正则 → required_actions），存在才读、
非法即阻断（fail-closed）。`delivery-gate` 收口时把 diff 喂给 `governance-eval` 拿到
命中规则与动作要求——命中独立审查的变更强制对抗复核。

`.repo-memory-kit/` 下的 `manifest.json`（Manifest v2：版本与受管资源记录）、
`install.lock`、事务目录与 zvec 索引都是**每机状态/派生物**，不提交进版本库。

所有记忆条目与 `docs/memory/README.md` 属于项目用户，更新和卸载不会删除它们。
安装器的生命周期保证是：

1. 先在内存生成完整 plan，再写 HMAC 保护的事务记录和 staged 内容；
2. 每个受管资源按 pre/post hash 分类和提交，Manifest 是最后一个内部提交步骤；
3. 治理 marker、治理规则和合法 v1 Manifest 退役与其他受管资源同事务；
4. workspace 链接/副本作为外部步骤先记意图，崩溃后可根据策略和内容血统补建或补删；
5. 恢复只在全部步骤明确为 OLD/NEW 时自动回滚或补完，CONFLICT 进入 `needs_human`；
6. 删除授权要求 Manifest 记录与当前内容血统**双条件匹配**。

因此，被手工修改过的受管内容会按 conflict 保护并保留现场；`--repair` 只恢复有 kit
血统的漂移文件。容器型文件（`CLAUDE.md`、`.mcp.json`、`settings.json` 等）
只移除 kit 片段，即使移除后为空也不删容器；跳过的条目会保留在缩减版清单里。

为兼容已经接入的项目，内部状态目录 `.repo-memory-kit` 以及托管区块标记继续使用旧命名。它们只是稳定的安装协议，不代表当前项目名称；请勿在业务仓库中手工改名。

## 已有 Spec Kit 文档怎么接入

普通安装只发现并报告 `.specify/specs`，不会修改现有文档（安装时已自动执行一次检查）：

```bash
./install.sh /path/to/your-project
cd /path/to/your-project && .repo-memory-kit/bin/spec-migrate --check .
```

确认后先预览，再显式迁移：

```bash
./install.sh --dry-run --migrate-specify /path/to/your-project
./install.sh --migrate-specify /path/to/your-project
```

迁移是增量的：先确保每个 feature 都有 `spec.md`、`plan.md`、`tasks.md`，然后保留原文、补缺失结构，并给新增段落打 `migration-pending` 标记；同时给 `.specify/templates` 增加受管覆盖层，统一未来文档。Agent 仍需结合代码、测试和业务决定完成语义整理，待核验标记不能当作设计已确认。图片、Word、表格、原型等附件不会自动改写。

也可以把单个 feature 迁移为团队 SDD 目录结构：

```bash
./install.sh --migrate-specify=001-demo --sdd-layout --sdd-version V1.0 /path/to/your-project
```

此时 feature 目录整体移动到 `docs/03-SDD/V<版本>/` 并按团队命名规范重排，`01-索引` 自动登记，原 feature 目录随之移除。安装器入口必须用 `--migrate-specify=feature名` 显式指定，防止整仓移动。

## 多人协作（git / SVN 同一套治理）

团队共用一个仓库（无论 git 还是 SVN）时，治理模型是同一套：

- **每机文件不提交**：`.repo-memory-kit/`（安装清单/事务/语义索引等每机状态与派生物）、`.mcp.json`、`.codex/`（含本机绝对路径）——每台机器必然不同，入库即成永久冲突源。`settings.json` 不在此列：hook 命令是 `$CLAUDE_PROJECT_DIR` 字面量（运行时展开），可跨机器共享。
- **同事开箱即用**：checkout/clone 即得技能与文档；各自跑一次 `install.sh`（幂等、零本地差异）获得工具/MCP/hook。
- **升级单点执行**：kit 版本升级由维护者统一执行——同版本重跑 install 不产生任何本地差异；版本统一后任何人的 install 都是无冲突的空操作。
- **需求与设计目录治理**：`docs/01-需求/`（索引 + 原始需求）由需求维护者唯一维护，与 `docs/03-SDD/`（设计文档集）同号同名（`NNN-名称`）；实现人只在自己被指派的 03-SDD 目录内工作。索引行分权：行与表头归维护者（含开发人员/需求上线时间，在需求分析冻结时确认）；"当前功能开发阶段"列由实现人的 Agent 在每阶段过门后回填——**只动自己的行**，多人公共索引冲突面最小。交付回执集中于 `docs/delivery-receipts/` 按功能命名。骨架从 kit 仓库 `templates/requirements/` 复制（或用已入库的本地副本，维护者操作）。

各版本控制的具体机制：

**git**——无需任何额外步骤：kit 在目标 `.gitignore` 的托管区块里自动写入上述每机文件清单（`.gitignore` 不存在时请先创建，kit 不代建用户文件）。

**SVN**——`svn:ignore` 是版本化属性，无法由文件承载，首次安装后运行一次治理初始化：

```bash
./install.sh --svn /path/to/your-project
```

幂等完成：svn:ignore 轻提交清单（合并追加，不覆盖 `.idea` 等既有项）、`docs/01-需求/README.md` 指派索引种子、内容确定文件 svn add（递归时自动跳过每机文件）、已版本化 kit 文本 `svn:eol-style=LF`（防 Windows CRLF 化与 kit 血统脱钩）、已入库的每机文件给出 `svn rm --keep-local` 整改提示。

## 更新与卸载

拉取最新版后更新项目：

```bash
git pull
./install.sh --update /path/to/your-project
```

检查安装健康度并恢复受管产物：

```bash
./install.sh --doctor /path/to/your-project
./install.sh --repair /path/to/your-project
```

`--doctor` 是只读检查，输出六种状态（HEALTHY / OUTDATED / DRIFTED / DEGRADED / CONFLICT / INCOMPLETE）。`--repair` 只恢复有 kit 血统的漂移文件；被手工修改过的内容（conflict）不会被自动覆盖，处置方式见报告提示。

安装清单丢失但内容仍是 kit 产物时，可显式登记（不修改任何文件）：

```bash
./install.sh --adopt-legacy /path/to/your-project
```

极少数情况（如崩溃后留下 needs_human 事务）按报告提示人工选择恢复策略：

```bash
./install.sh --recover=rollback /path/to/your-project    # 或 --recover=roll-forward
```

使用 Codex workspace 产物的项目：更新时建议再次传入 `--codex-root`（新链接/副本的
创建与核验只认显式参数）；卸载会自动读取已记录的位置，也可显式传入以便复核。

卸载：

```bash
./install.sh --uninstall /path/to/your-project
```

卸载只移除 Manifest 记录且内容血统双条件匹配的 kit 产物及 workspace 技能链接，不会移除用户记忆，也不会卸载机器级的 `codebase-memory-mcp`。

## 依赖边界

- 安装器：Python 3.10+（`install.sh` 首步检测）；无其他外部依赖。
- 语义检索层：可选，`pip install zvec`（预编译轮子，无模型下载、零 API key）；未安装时其余功能不受影响。
- archify 图表：可选，**Node.js ≥18**；缺失时 doctor 报 DEGRADED，业务流程图回退 mermaid。
- 无头浏览器（visual-check 证据截图）：可选，Chrome/Chromium；缺失时文档嵌 HTML 引用不嵌图。
- 代码图谱增强：仅在使用 `--with-codebase-memory` 时需要网络。
- git 不是目标项目的硬要求；SVN 或纯本地项目也可以使用记忆系统和显式文件清单检查。

## 效果评测

`tests/benchmark/evaluate.py` 用机械证据评测 Agent 交付，不使用 LLM judge。当前指标包括：

- Task Correctness：场景验证命令是否通过；
- Evidence Completeness：设计、测试方案、回归测试和交付回执是否齐全；
- Change Precision：实际 diff 是否落在预计修改范围，是否触及禁止路径；
- Memory Utilization：是否引用了场景相关记忆，实现是否体现其决策。

```bash
# 评测一个交付仓库
python3 tests/benchmark/evaluate.py \
  --repo /path/to/agent-output --scenario bug-fix-regression --base main

# paired 对比：未安装 kit 与安装 kit 的同一任务
python3 tests/benchmark/evaluate.py \
  --compare /path/to/vanilla /path/to/with-kit \
  --scenario bug-fix-regression --base main
```

评测器使用 NUL 分隔的 Git 路径输出，同时纳入未跟踪文件；“有回归测试”要求
diff 中出现真实新增的可执行测试信号，空文件或注释不计分。内置场景是评测基线，
不是所有项目的通用质量标准；项目应按实际路径、测试命令和证据要求扩展场景。

## 开发与验证

```bash
./tests/installer.test.sh     # 安装器生命周期（事务/崩溃恢复/外部事务模型/archify 集成/--svn 治理）
./tests/install.test.sh        # wrapper 全链路（安装/更新/卸载/安全清理）
./tests/build.test.sh          # 记忆构建与语义检索层（模块目录/迁移/锚点）
./tests/skill-sync.test.sh     # 技能副本一致性
./tests/spec-migrate.test.sh   # Spec Kit 迁移
./tests/doc-gate.test.sh       # 文档门禁（硬校验/追踪链/评审绑定/冻结哈希/治理引擎）
./tests/import-guard.test.sh   # Windows 可导入性守卫（AST）
./tests/mcp.test.sh            # MCP 协议（版本协商/生命周期/参数校验）
./tests/benchmark.test.sh      # Benchmark 评测器（路径边界/回归测试证据）
shellcheck install.sh tests/*.test.sh
```

当前九套件共 **594 条断言**；CI 含 Ubuntu 全量验证、ShellCheck、archify 真实执行，
以及 Windows 原生导入和 copy 策略的 install / update / doctor / uninstall 生命周期。

## License

MIT。第三方思想来源与许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
