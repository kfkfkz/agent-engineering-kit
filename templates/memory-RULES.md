# 项目记忆规则（RULES）

> **本文件由 agent-engineering-kit 管辖**，`install.sh --update` 会自动刷新——请勿手工编辑（改动会被覆盖）。如需改规则，改 kit 仓库的 `templates/memory-RULES.md`。
> 用户数据（索引、巡检记录、条目、锚点表）在 `README.md`、各条目文件与 `.anchors.json` 中，可自由编辑。

## 使用规则（对 Agent 同样生效）

1. 动手修改代码前，先查 `README.md` 索引（`memory-build` 自动生成），命中相关条目必须先读全文。
2. 状态为「待验证」（unverified）的条目仅供了解，不得作为修改依据。
3. Bug 修复经人工审核通过后，将原因、影响范围、修复方案整理为新条目入库（素材即审核通过的 bug 分析，零额外成本）。
4. 条目过时改为 `deprecated`；被新条目替代改为 `superseded`（并在新条目 frontmatter 的 `supersedes` 指回旧条目）。原则上不删除，保留可追溯。**唯一例外见「敏感信息误入库应急」**。
5. 遵循安全要求：条目中禁止出现密钥、Token、真实 PII、内网地址。
6. **功能验收通过后进行功能蒸馏**：按下方「功能蒸馏」清单将核心流程与环境信息沉淀为 playbook 条目。
7. **任何条目增删改后必须运行 `memory-build`**（`.repo-memory-kit/bin/memory-build`）刷新 README 索引与锚点表——索引与锚点表是生成物，禁止手改；zvec 可用时语义索引随本次构建自动刷新。

## 触发与捕获（记忆怎么进来）

两类触发，同一流程：

| 触发 | 时机 | 说明 |
| --- | --- | --- |
| 口头触发 | 用户说"记一下/记住这个/沉淀一下"等 | 即时捕获当前会话中的经验 |
| 收尾触发 | 功能验收通过、bug 修复完成、任务收尾 | 主动提议：本次有哪些值得沉淀 |

捕获流程（Agent 执行，**确认环节不可省**）：

1. **候选提取**：扫描当前会话，识别四类候选——坑（根因已明的缺陷/陷阱）、流程（跑通的操作步骤）、环境信息（路由/配置/关键 ID）、决定（重要取舍及理由）。
2. **分类起草**：按条目模板生成草稿（**frontmatter 必填**：type/status/created，playbook 还须 verified）；坑 → pitfall（默认 status: unverified，除非已有验证证据），流程/环境 → playbook，**决定 → decision**（背景/取舍/后果，禁止硬塞进 playbook）。不捕获凭证与内网地址（安全规则）、纯会话性内容。
3. **呈现确认**：逐条列出（类型/标题/一句话），等用户确认或修改——**确认是入库的必要条件**。
4. **入库**：确认后写入对应目录（`YYYY-MM-DD-<中文标题>.md`，标题与正文一级标题一致，纯代码符号名可保留英文；frontmatter 的 anchors 填类型化定位符），**随即运行 `memory-build` 刷新索引与锚点表**；达到 L3 门槛（有效 `confirmed` 条目 ≥ 10，由 `memory-build` 自动判断）时提示蒸馏 PROFILE。
5. **后续提示**：待验证条目给出验证方式；playbook 标注「最后有效」日期。

## 功能蒸馏清单

功能验收通过后，对该功能蒸馏以下内容入库：

| 蒸馏项 | 去处 | 内容 |
| --- | --- | --- |
| 核心流程 | 业务域地图条目（项目已建立时）；无域地图时 `playbooks/` | 端到端可执行步骤（谁调用谁、接口顺序）、涉及的组件/算法包清单 |
| 环境信息 | `playbooks/` | 部署环境、服务路由前缀、关键配置项（凭证除外，写获取途径） |
| 可复用测试数据 | `playbooks/` | 现成的场景/资源 ID，附「最后有效」日期 |
| 过程中踩的坑 | `pitfalls/` | 功能开发/测试期间定位的缺陷与陷阱 |
| 权威文档指针 | playbook 内 | spec/接口文档等详细文档的位置，报文细节不复制 |

原则：记忆区存"怎么跑通"和"去哪查详情"，不复制权威文档内容（单一口径）。

**系统状态知识的统一登记处**：项目已有业务域地图（或 SDD 等体系——按团队建议的格式组织、方便理解，**不是事实权威**）时，能力入口、调用链、域职责、业务流（含跨域端到端链路）统一登记在域地图条目，不在 playbook 里复制；**事实以代码为准**——域地图与代码冲突时修正域地图，不在记忆区另立口径。开发完/测试完的业务流总结，改排为对应域地图条目（经人确认后写入）；playbook 只保留**场景化操作**（环境访问、测试数据、环境执行套路、构建与工具操作），并以指针链接域地图。域地图条目同样遵守证据规则：从代码形成的结论必须带文件、类、方法或数据表证据。

## 分层与下钻

记忆区是三层结构 + 一个外部证据层，上层只存结论与指针，细节必须可下钻：

```text
L3  PROFILE.md（项目经验画像）   宏观引导：风险域地图/稳定约定/反模式，会话开始优先读
L2  playbooks/                  场景流程：某功能/环境怎么跑通
L1  pitfalls/ + decisions/      原子经验：坑（缺陷/陷阱）与决定（背景/取舍/后果）
L0  权威文档与代码（记忆区外）   证据：源码（第一事实源）、业务域地图、spec/SDD、接口文档
```

**语义检索层（memory-recall）**：`docs/` 下全部 Markdown（记忆条目、业务域地图、SDD、交付回执；模板与生成索引除外）经 zvec jieba 全文 + 双字段 RRF 建成可检索索引，Agent 取上下文时先 `.repo-memory-kit/bin/memory-recall "<任务描述>"`，再读命中条目全文——功能点多了以后，靠人工记得去查索引表必然漏召回。索引是派生数据（`.repo-memory-kit/zvec/recall`，随卸载移除），条目增删改后由 `memory-build` 随动重建（单独 `--rebuild` 仍可用）；可选依赖 `pip install zvec`，未安装时明确报错降级，不影响记忆体系其余功能。

**L3 消费规则（写入 Agent 指引）**：`PROFILE.md` 存在时，Agent 会话开始必须先读它，再查 README 索引——没有消费规则的生产规则是死代码。

规则：

1. **L3 建立门槛**：有效 `confirmed` 的 L1/L2 条目 ≥ 10 条时，从 `_PROFILE_TEMPLATE.md` 创建 `PROFILE.md` 并蒸馏；`unverified`、`deprecated`、`superseded` 不计入。`memory-build` 自动统计并提示，不在 README 手工维护计数。PROFILE 存在时，以「最后蒸馏」日期和条目 `created` 自动检测新增 confirmed 条目；新增 ≥ 3 条时提示重新蒸馏。
2. **L3 分区证据规则**：稳定约定须有 ≥2 个独立的 confirmed 来源；风险域地图是预警索引，允许单一 confirmed 来源，但须标「待观察」，≥2 个独立事件才可标「反复」；反模式有一次证据充分的失败或明确否决决定即可进入，否则仍须 ≥2 个独立来源。同一事故拆成多条记录不算独立来源。
3. **下钻链必须闭合**：L3 每条结论标注支撑条目；L2 引用相关 L1 与 L0；L1 的校验点指向代码——任何结论都能沿 "L3 → L2 → L1 → L0" 走到证据。
4. **上层不复制下层内容**：L3 只有一句话结论 + 指针；发现 L3 出现细节描述即为违反大小约束。
5. 明确不吸收的：短期会话压缩、自动 L0 捕获、自动生成记忆条目——属自动蒸馏阶段；检索加速（memory-recall 语义层）不属此列，见「语义检索层」。

## 条目大小与时效（压缩与矫正）

记忆条目会随时间膨胀和失实（接口改了、ID 换了、路径移了），必须定期压缩与矫正，否则会误导后续 Agent。**过期的记忆比没有记忆更危险。**

### 大小约束

- pitfall 条目 ≤ 60 行，playbook ≤ 150 行；超出时压缩：过程细节（报文、步骤明细）下沉到权威文档并保留指针，或按主题拆分条目。
- 压缩不删「状态/触发/根因结论」，只下沉过程细节。
- README 索引表一句话 ≤ 40 字。

### 时效规则

- 日期在 frontmatter：`created` 为定位/创建日期，`verified` 为最近验证日期（playbook 必填，即"最后有效"）。
- **用后即校**：任何 Agent 或人引用条目时发现与现状不符，必须当场矫正内容并更新 `verified`；无法确认的标记 `deprecated`。
- **定期巡检**：每个迭代周期或大功能开工前执行一次全量巡检；改代码前执行增量巡检（`memory-build --changed` 或 `/memory-check <符号>`）。

### 前置依赖（默认强制）

巡检必须使用结构化代码检索工具（代码知识图谱类，如 codebase-memory MCP；或提供符号级定位、调用关系、索引覆盖/新鲜度信号的等效工具），grep 仅限字面量、配置文件与非代码文件。工具不可用时报告缺失并终止巡检，除非用户明确要求降级（降级报告必须标注"grep 降级，置信度低"）。

### 巡检步骤（Agent 可执行）

1. **前置检查**：确认结构化检索工具可用、当前项目已索引且索引未陈旧（如 codebase-memory：`list_projects` → `index_status` → 对涉及文件 `check_index_coverage`）；索引缺失或陈旧先重建。
2. 逐条读取条目，无「校验点」小节的从正文提取可核验事实（接口路由、类/方法名、文件路径、配置键）生成该小节。
3. 对每个校验点比对当前代码库（符号定位用图工具；grep 仅用于字面量、配置文件与非代码文件）。核验路由是否仍存在、类/方法逻辑是否未变、文件是否还在。
4. 失配条目：修正正文与校验点，更新日期；无法确认的列出待人工决定是否 `deprecated`。
5. 超限条目按「大小约束」压缩；符号锚点增删改在条目 frontmatter 的 `anchors` 中进行；**最后统一运行 `memory-build` 刷新 README 索引与 `.anchors.json`**；`PROFILE.md`（L3，存在时）核对下钻链仍闭合并更新「最后蒸馏」日期。
6. 输出报告：本次矫正/压缩了什么、哪些存疑；巡检只改 `docs/memory/` 内文档，不改代码；`deprecated` 需人工确认。

### 增量巡检（事件驱动模式）

输入变更范围，只核验受波及的条目——用于 bug 修复合入前、大功能开工前、或快速确认某次重构没打断记忆链：

1. **确定变更面**：只知道改了哪些文件时，运行 `memory-build --changed [ref]` / `--since <ref>`（自动探测 git/svn），或任意来源用 `<变更命令> | memory-build --files [目标仓库]`（可加 `--report <路径>` 生成 Markdown 影响报告、`--strict` 供 CI 阻断）；知道符号时直接查 `.anchors.json`，表缺失或未命中时回退结构化检索文本反查（如 codebase-memory `search_code(符号, path_filter="docs/memory")`）。
2. **命中条目**执行全量巡检的核验与矫正步骤；**无命中**则报告"本次变更不波及任何记忆条目"。

### 锚点表（符号 → 条目映射）

`docs/memory/.anchors.json` 是"变更符号 → 受波及记忆条目"的精确映射，**由 `memory-build` 从各条目 frontmatter 的 `anchors` 字段聚合生成**（旧表中 `unresolved` 与 `file`/`signature` 提示字段自动保留），随仓库版本化，禁止手改。

**协议（所有写入方必须遵守，schema_version 兼容性依据）**：

- `schema_version`：当前为 `2`；读取方遇到不认识的版本应拒绝解析并提示重新生成。
- 键为**类型化限定定位符** `"<kind>:[<repo>@]<identifier>[<参数签名>]"`——裸类名/方法名在大型仓库必然碰撞（重载、同名类、save/build 这类通用名），禁止使用：
  - kind：`class` / `method` / `file` / `route` / `config`
  - `repo@` 前缀：跨仓库锚点必填，本地符号省略。**仓库进键（而非值）**——同一符号在本地与多个远程仓库都存在时分别建键即可区分：`class:com.x.A` 与 `class:other-repo@com.x.A` 是两个不同锚点
  - `method` 存在重载歧义时，identifier 必须带参数类型：`method:com.example.OrderService.create(OrderRequest)`
  - 示例：`class:com.example.OrderService`、`method:com.x.OrderService.create(OrderRequest)`、`file:src/x.yml`、`route:POST /api/v1/orders`、`config:server.servlet.context-path`、`class:other-repo@com.example.C`
- `anchors`：对象；值为 `{ "entries": [...], "file": "<可选，仓库相对路径>", "signature": "<可选，方法签名>" }`；`entries` 为条目相对路径数组（相对 `docs/memory/`），升序排序、去重。
- **跨仓库锚点**：键带 `repo@` 前缀标识目标仓库（如前端条目引用后端接口、平台条目引用 SDK 符号）。本仓库增量巡检只匹配无前缀的本地锚点；带 `repo@` 的锚点在巡检报告中列为"跨仓库待核验"，其核验在目标仓库进行。
- `unresolved`：对象；键同上，值为 `{ "entries": [...], "reason": "<漂移待修 | 跨仓库>" }`。
- 写入必须**原子**（先写临时文件再重命名），`anchors` 与 `unresolved` 各自按键升序排序。
- 刷新时机：条目 frontmatter 的 `anchors` 增删改后运行 `memory-build`。全量巡检时顺带核对：解析失败的符号从 `anchors` 移入 `unresolved`（或恢复）；`unresolved` 不可静默丢弃（符号改名 → 更新条目 frontmatter；跨仓库 → 用 `repo@` 前缀建正式跨仓库锚点或标注原因保留）。
- 锚点表是"记忆→代码"边的数据形态；若撞到精度或联动天花板（如需 detect_changes 原生覆盖记忆），再考虑图谱工具原生边扩展。

## 敏感信息误入库应急（"不删除"原则的唯一例外）

条目状态流转原则上不删除内容（保留可追溯），但**误写入凭证、密钥、真实 PII 或内网地址时，标记 `deprecated` 不够**，必须：

1. 立即从条目中删除敏感内容本身（而非仅标记过时）；
2. 清除版本历史中的该版本（git filter-repo / BFG），或评估仓库可见范围确认未外泄；
3. **轮换泄露的凭证**（视为已泄露处理）；
4. 另存一条 pitfall 记录事故根因与防范（不含敏感内容本身），保持可追溯。

## 条目格式（v3：frontmatter 单一事实源）

每个条目以 YAML frontmatter 开头，README 索引与 `.anchors.json` 均由 `memory-build` 从 frontmatter 自动生成——**索引与锚点表是生成物，禁止手改**。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| type | ✓ | pitfall / decision / playbook（须与所在目录一致） |
| status | ✓ | confirmed / unverified / deprecated / superseded |
| module | 建议 | 模块/服务 |
| created | ✓ | 定位/创建日期（YYYY-MM-DD） |
| verified | playbook 必填 | 最近验证日期（playbook 的"最后有效"） |
| verified_commit / evidence | 可选 | 验证所在提交 / 证据指针 |
| anchors | 可选 | 类型化定位符列表（见「锚点表」） |
| summary | 可选 | 索引一句话（缺省用标题） |
| supersedes / conflicts_with / related | 可选 | 替代/冲突/关联条目（相对路径）；status 为 superseded 的条目必须被某条的 supersedes 指向 |

正文：一级标题 + 内容小节 + 「校验点」（巡检记录与补充事实）。状态与日期**不再写在正文**——单一事实源在 frontmatter。模板：`pitfalls/_TEMPLATE.md`、`decisions/_TEMPLATE.md`、`playbooks/_TEMPLATE.md`、`_PROFILE_TEMPLATE.md`；命名 `YYYY-MM-DD-<中文标题>.md`（纯代码符号名可保留英文）。存量英文命名的条目可运行 `memory-build --rename-by-title` 按标题批量重命名，交叉引用与索引自动联动。
