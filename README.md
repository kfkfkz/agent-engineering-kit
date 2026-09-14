# agent-engineering-kit

给 Claude Code、Codex 和其他仓库级 AI Agent 使用的一体化工程工作流包。

安装一次后，团队成员会使用同一套项目宪章、复用调研、代码取证、系统调试、TDD、安全审查、交付验证、Spec Kit 迁移和项目记忆规则，减少“不同 Agent 各写一套设计、各走一套流程”的偏差。代码审查与交付验证由内置工作流闭环完成，不要求额外的审查 CLI。

## 三分钟接入

### 1. 安装到项目

```bash
git clone https://github.com/kfkfkz/agent-engineering-kit.git
cd agent-engineering-kit
./install.sh /path/to/your-project
```

安装是幂等的，可以重复执行。它不会联网，也不会覆盖项目已有的记忆条目。安装器需要本机有 **Python 3.10+**（其余无外部依赖；`sha256sum`/`shasum` 都不再需要）。

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

### 3. Codex 从父目录启动时

Codex 只会从当前目录向上发现 `.agents/skills`。如果日常在 workspace 根目录启动，而项目是其子目录，请这样安装：

```bash
./install.sh --codex-root /path/to/workspace /path/to/workspace/your-project
```

也可以同时启用代码图谱：

```bash
./install.sh --with-codebase-memory \
  --codex-root /path/to/workspace \
  /path/to/workspace/your-project
```

安装器会在 workspace 建立指向目标项目技能的受管符号链接，技能内容仍由目标项目统一维护。

## 日常怎么用

通常只需要记住 `repo-delivery`。它会根据任务类型调用其他技能，并复用项目已有的 Spec Kit、SDD、版本文档或变更记录。

| 场景 | Claude Code | Codex | 作用 |
| --- | --- | --- | --- |
| 完整功能、修复、重构、迁移 | `/repo-delivery 任务` | `$repo-delivery 任务` | 从宪章门禁推进到实现、验证和文档收口 |
| 理解结构、调用链、影响面 | `/codebase-memory 问题` | `$codebase-memory 问题` | 用代码图谱取证并核验源码与索引覆盖 |
| Bug、测试失败、异常行为 | `/systematic-debugging 问题` | `$systematic-debugging 问题` | 先复现和定位根因，再决定修复 |
| 测试先行实现 | `/tdd 行为` | `$tdd 行为` | 按纵向切片执行 RED → GREEN → REFACTOR |
| 新依赖、集成或技术选型 | `/reuse-research 需求` | `$reuse-research 需求` | 比较采用、扩展、组合和自建，记录证据 |
| 漏洞与配置风险 | `/security-review 范围` | `$security-review 范围` | 检查应用数据流与 Agent 配置攻击面 |
| 交付收口门禁 | `/delivery-gate` | `$delivery-gate` | diff 审查、对抗复核与验证闭环，结论以验证回执为证 |
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
3. **需求确认**：把"加个导出"衍生为具体场景（如"运营在订单列表页筛选后导出当日对账单"），每个场景定义可观察产出（谁在哪个页面拿到什么）；场景与产出确认后作为验收依据；导出方式、异步任务、文件存储等会改公共契约的细节一并交用户裁决。
4. **概要设计**：先对齐项目内同类已认可文档的骨架；功能设计按能力逐节写界面（菜单落位+原型）、前端展示清单（④接口的验收依据）与业务逻辑；导出入口落在订单列表页工具栏、导出记录查询落在新页面；页面文案与菜单树取真实依据，原型渲染 PNG 嵌入；需要新 CSV 组件时用 `reuse-research` 比较现有依赖、扩展点和新库。
5. **详细设计**：产出《订单列表导出-详细设计》——每个接口、表结构、流程改动指到具体页面与功能点；验收要点逐条溯源到场景与产出；测试与验证设计给出接缝分层（导出服务层单测+权限过滤集成测）、120 环境与测试数据、导出失败日志点与错误码、回归范围（现有导出接口不动）；随后过 `delivery-gate` 文档门禁（结构/字段/一致性/可验证性），发现缺漏回补后落回执。
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
/repo-delivery 修复“支付回调重试时偶发重复扣减库存”。
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

1. 稳定复现或收集足够反证，区分“消息重复”“幂等判断失效”“事务边界错误”等不同原因。
2. 使用 `codebase-memory` 追踪支付回调、订单状态、库存服务、消息重试和事务调用链。
3. 写出一个单一假设，例如：“幂等记录与库存扣减不在同一事务边界，因此并发回调都通过了前置检查。”
4. 用最小实验验证假设；不成立就撤回，不叠加试错补丁。
5. 根因确认后，使用 `tdd` 在稳定接口建立并发回调回归测试，确认 RED 是因为重复扣减而不是测试环境问题。
6. 只修改根因所需代码，再验证单次回调、重复回调、并发回调、失败重试及事务回滚。
7. 更新原故障记录或设计文档，并把真正可复用的坑点作为记忆候选交给人确认。

一次合格的最终汇报应类似：

```text
根因：幂等判断与库存扣减分属两个事务，并发回调可同时通过检查
证据：并发测试稳定复现；调用链和事务日志确认了竞态窗口
修复：在库存扣减的原子边界内写入业务幂等键，未改变正常重试协议
回归：单次、串行重复、并发重复、失败重试和回滚测试通过
影响：支付回调到库存扣减链；未修改其他库存入口
残余风险：历史重复记录需单独数据治理，本次未自动清理
文档/记忆：故障记录已回写；提出“跨事务前置幂等失效”pitfall 候选
```

如果当前只允许排查、不允许修改：

```text
/systematic-debugging 排查支付回调重复扣减，只给出根因、证据和建议，不修改代码。
```

### 其他常见用法

```text
# 重构：要求行为不变，并给出影响分析和删除旧实现的证据
/repo-delivery 重构通知发送模块，统一邮件和短信的重试策略，不改变外部 API。

# 数据库迁移：先设计兼容、灰度和回滚，再进入实现
/repo-delivery 把订单状态字段迁移为状态码，要求支持新旧版本并行和可回滚。

# 影响分析：只回答结构问题
/codebase-memory 如果修改 OrderService.confirm，会影响哪些入口、消息和测试？

# 对已定位问题测试先行
/tdd 为“优惠券只能核销一次”增加并发回归测试并完成最小实现。

# 功能完成后提取经验；仍需人工确认才会入库
/memory-capture 从本次订单导出交付中提取可复用的坑点和流程。

# 迭代末巡检项目记忆
/memory-check
```

## 统一工作流

```text
repo-delivery
  ├─ 读取项目指令、宪章和相关记忆
  ├─ 判定：直接修改 / 缺陷修复 / 既有变更 / 新功能
  ├─ ① 需求拆分：codebase-memory + 业务域地图 + memory-recall 取证
  ├─ ② 需求确认：衍生需求场景与产出定义（验收依据），待澄清问题交人裁决
  ├─ ③ 概要设计：功能点（菜单/页面落位/关系）+ 交互流程 + 原型/页面内容
  ├─ ④ 详细设计：《功能名-详细设计》（领域模型/分侧/数据库/接口/验收/测试与验证设计）
  ├─ 设计定稿门禁：delivery-gate 文档形态（结构/字段/一致性/可验证性 + 回执）
  ├─ ⑤ 任务拆分 → ⑥ tdd 编码与验证（systematic-debugging / security-review 按需）
  ├─ delivery-gate：diff 审查、对抗复核与验证回执
  └─ 回写既有设计文档，并提出记忆候选
```

新功能走全链；直接修改与缺陷修复走短路，既有变更从影响面进入——不为小改动强加六道门禁。

工作流只做门禁、路由和收口，不另建一套平行的设计体系：

- 项目 `AGENTS.md`、`CLAUDE.md` 和 constitution 是约束来源；
- 已有 spec、SDD、版本文档和代码是事实来源；
- 项目专属技术栈、覆盖率、数据禁区等以当前项目为准；
- 高风险决策仍需人工确认，AI 草稿不能自行标记为已确认。

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

覆盖两类风险：应用代码中的认证授权、注入、SSRF、路径/文件、敏感数据、支付回调和供应链；以及 Agent 指令、skills、MCP、hooks、安装脚本中的提示注入、过宽权限、秘密泄露和危险执行。HIGH/CRITICAL 必须给出可达路径，并由 `delivery-gate` 逆向验证。

### delivery-gate

收口门禁，两种形态。**代码形态**：阶段一对最终 diff 做正确性、宪章、兼容性、测试、维护性和安全自审，高危发现做对抗式复核（运行环境提供子 Agent 时交给未参与实现的独立审查者）；阶段二从项目自身的 CI、构建文件和宪章提取实际门禁执行验证闭环。**设计文档形态**：设计阶段产出（需求澄清/业务流程设计/详细设计/任务清单）进入下一阶段前做结构完整性、字段覆盖、一致性、可验证性四维审查，高风险设计决策同样对抗复核。两种形态共用结论枚举 `READY`、`NOT READY`、`NEEDS HUMAN REVIEW`，未运行的检查不会被写成通过；验证回执（`docs/delivery-receipts/`）是声称验证通过的唯一证据形态。

### spec-migrate 与 task-handoff

`spec-migrate` 把已有 Spec Kit 文档安全映射到统一字段，保留原文并显式标记待核验内容；`task-handoff` 在中断或跨 Agent/会话时记录目标、证据、修改、验证、风险和唯一下一步。

### memory-check 与 memory-capture

`memory-check` 负责全量或增量巡检；`memory-capture` 负责把已验证的工程经验转成候选条目。候选必须经人确认后才能写入，避免未经验证的 AI 总结污染项目记忆。

## 项目记忆

```text
L3  docs/memory/PROFILE.md              会话开始先读的项目画像
L2  docs/memory/playbooks/              场景化操作（环境访问/测试数据/执行套路/构建）
L1  docs/memory/pitfalls/ + decisions/  原子坑点与技术决定
L0  代码（第一事实源）、业务域地图、spec/SDD、接口文档
```

规则位于 `docs/memory/RULES.md`。条目 frontmatter 是唯一事实源，`memory-build` 自动维护 README 索引和 `.anchors.json`：

```bash
# 校验条目、索引和锚点是否一致
.repo-memory-kit/bin/validate-memory.sh .

# 重新生成索引与锚点
.repo-memory-kit/bin/memory-build .

# 检查当前 git/svn 变更影响了哪些记忆
.repo-memory-kit/bin/memory-build --changed .

# 存量英文命名条目按标题批量转为中文名
.repo-memory-kit/bin/memory-build --rename-by-title .

# 语义检索：docs/ 全量（记忆/域地图/SDD/回执）混合检索，可选层
# memory-build 构建时 zvec 可用即自动随动刷新，单独重建用 --rebuild
.repo-memory-kit/bin/memory-recall "任务描述" .

# 域地图守卫：索引登记/状态/证据路径核验（回写域条目后运行）
.repo-memory-kit/bin/domain-check .
```

条目命名 `YYYY-MM-DD-<中文标题>.md`，纯代码符号名可保留英文。

**业务域地图是业务流的登记处**：项目建立业务域地图（如 `docs/02-业务域地图/`）后，开发完/测试完的业务流总结（含跨域端到端链路）、能力入口、调用链登记为域地图条目，不在 playbook 里复制；playbook 只保留场景化操作并以指针链接域地图。事实以代码为准，域地图与代码冲突时修正域地图。

PROFILE 在有效 `confirmed` 条目达到 10 条时创建；后续积累达到提醒阈值时，`memory-build` 会提示重新蒸馏。

## 安装内容与文件所有权

安装器会创建或更新：

```text
.claude/skills/                 Claude Code 项目技能
.agents/skills/                 Codex 项目技能
.repo-memory-kit/bin/           校验器、生成器和 Spec 迁移器
.repo-memory-kit/manifest.json  安装清单（Manifest v2：版本与受管资源记录）
.repo-memory-kit/zvec/          语义索引派生数据（generation + current.json 指针）
docs/memory/RULES.md            记忆规则
docs/memory/*/_TEMPLATE.md      条目模板
CLAUDE.md / AGENTS.md           agent-engineering-kit 托管区块
.cbmignore                      代码图谱索引托管区块
```

所有记忆条目、`docs/memory/README.md` 索引和 `.anchors.json` 属于项目用户。更新和卸载不会删除它们。

安装器是事务化的（崩溃安全）：安装/卸载先在内存生成完整计划并落盘事务日志（HMAC 保护），再以内容 CAS 提交；中断后再次运行会自动恢复现场（回滚或补完）。删除授权要求 Manifest 记录与内容血统**双条件匹配**——被手工修改过的受管内容按 conflict 保护并报告，`--repair` 只恢复有 kit 血统的漂移文件；容器型文件（CLAUDE.md、`.mcp.json`、`settings.json` 等）只移除 kit 片段，即使移除后为空也不删容器，跳过的条目会保留在缩减版清单里。

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

此时 feature 目录整体移动到 `docs/03-SDD/V<版本>/` 并按团队命名规范重排（`02-需求`、`03-架构设计`、`04-详细设计/N-功能名`、`06-实现计划`、`07-原始需求材料`），`01-索引` 自动登记，原 feature 目录随之移除（`.specify/specs` 下不保留副本）。安装器入口必须用 `--migrate-specify=feature名` 显式指定（等价于迁移器的 `--apply --feature`），防止整仓移动。

## SVN 多人协作

目标仓库托管在 SVN、由团队共用时，首次安装后运行一次治理初始化：

```bash
./install.sh --svn /path/to/your-project
```

它会自动完成（幂等，可重复执行）：

- **svn:ignore 轻提交清单**：`.repo-memory-kit`、`.mcp.json`、`.codex`（每机状态/本机配置），
  `.claude/settings.json`（hook 含本机路径），`docs/memory/` 下的 `README.md` 与
  `.anchors.json`（memory-build 全量重写的生成物）——这些文件每台机器必然不同，
  入库即成永久冲突源；合并追加，不覆盖既有 ignore 项；
- **需求目录种子**：创建 `docs/01-需求/README.md` 指派索引（seed 式，此后不再改写）；
- **svn add 内容确定文件**（技能/模板/RULES/托管区块等，递归时自动跳过上述每机文件）；
- **`svn:eol-style=LF`**：已版本化的 kit 文本文件统一 LF，防 Windows 提交 CRLF 化
  导致与 kit 血统脱钩；
- 已被版本控制的每机文件给出 `svn rm --keep-local` 整改提示。

多人协作约定：**需求目录结构（含指派索引）由需求维护者唯一维护**；实现人只在
自己被指派的需求目录内补充与撰写（一个需求一个目录，验证通过后整理终稿），
交付回执集中于 `docs/delivery-receipts/` 按功能命名。每个需求目录的文档骨架
从 kit 仓库 `templates/requirements/` 复制（维护者操作）。kit 版本升级由
维护者统一执行——同版本重跑 install 不产生任何本地差异。

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

使用 workspace 符号链接的项目：更新时建议再次传入 `--codex-root`（新链接的创建与核验只认显式参数）；卸载会自动读取已记录的位置，也可显式传入以便复核。

卸载：

```bash
./install.sh --uninstall /path/to/your-project
```

卸载只移除 Manifest 记录且内容血统双条件匹配的 kit 产物及 workspace 技能链接，不会移除用户记忆，也不会卸载机器级的 `codebase-memory-mcp`。

## 依赖边界

- 安装器：Python 3.10+（`install.sh` 首步检测）；无其他外部依赖。
- 语义检索层：可选，`pip install zvec`（预编译轮子，无模型下载、零 API key）；未安装时其余功能不受影响。
- 代码图谱增强：仅在使用 `--with-codebase-memory` 时需要网络。
- git 不是目标项目的硬要求；SVN 或纯本地项目也可以使用记忆系统和显式文件清单检查。

## 开发与验证

```bash
./tests/installer.test.sh     # 安装器生命周期（§20.1 冻结版清单：事务/恢复/存量迁移）
./tests/install.test.sh      # wrapper 全链路（安装/更新/卸载/安全清理）
./tests/build.test.sh        # 记忆构建与语义检索层
./tests/skill-sync.test.sh   # 技能副本一致性
./tests/spec-migrate.test.sh  # Spec Kit 迁移
shellcheck install.sh tests/*.test.sh
```

## License

MIT。第三方思想来源与许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
