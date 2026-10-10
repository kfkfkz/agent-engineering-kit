# AEK 1.0.3 — 工具可靠性与开发侧核验

规划依据：用户提供的六个工作包，2026-10-08 授权评估后开始开发。原始需求与 SDD 在
`docs/01-需求/003-AEK-1.0.3真实Agent评测/`、`docs/03-SDD/003-AEK-1.0.3真实Agent评测/`。
本计划记录实施边界和进度，未完成项不代表已实现能力。

## 版本范围与裁决

- 本轮发布范围：已知可靠性修复、独立行为核验、六个公开合成场景、离线诊断/报告、有界流程分析，
  以及性能/容量、安装生命周期和 CLI/MCP 兼容性回归。
- 不实现或运行在线模型配对控制器，不以付费模型、竞品对比或 Token 节省证明作为发布门槛。
  live 网络/认证/treatment/capture 与真实收益实验移出本轮；既有离线接缝保留，不冒充 live-ready。
- P2 消融和改进任务自动导出为 advisory；可产出改进候选，不自动改源码，不影响发布核心判定。
- 先使用严格 JSON 场景文件与 Python 标准库；YAML、静态 HTML 是可选增强，不引入默认运行时依赖。
- Outcome Quality、Process Quality、Resource Cost 分开报告。文档数量和流程合规不能替代独立正确性。
- 同一 Agent、模型及设置内配对；Codex 与 Claude 的结果分别报告，不混成模型等价实验。
- Vanilla/AEK 使用相同代码基线、任务信息、可信验证器和预算，实验顺序轮换；不同能力配置须披露。
- workspace/session/cache 隔离不等于隐藏测试不可读。正式实验必须有文件与网络访问边界证明；
  未证明或检测到泄漏时实验无效，不能据此发布收益结论。
- 未提供遥测记 UNKNOWN，字节数不换算成真实 Token。客户端估算费用不当作账单；缓存口径逐项记录。
- 新框架不得暗中覆盖旧 CLI 契约；确有旧误判时保留字段形状，增加明确状态并记录修正。
- 之前的 WorkUnit/策略/Skill 修复作为前置稳定性工作保留，禁止覆盖或混入实验效果基线而不披露。
- 正式运行需固定 AEK commit、场景 digest、验证器 digest、模型/CLI 版本、权限和环境。工作区有
  未提交修改时只能做带快照标识的开发验证，不能假装是已发布版本实验。
- 范围修订后重评适用文档/凭证，旧版签核不证明当前修订；不把移出项标记为测试通过。

## 当前收口路径

1. 同步需求、设计、任务和测试中的发布边界，明确取消的在线验收与保留的核心验收。
2. 完成 Memory 输入/身份/恢复/报告等变更审查与全部适用回归，不降低断言或既有容量阈值。
3. 写真实验证清单与 As-Built，统一收口，版本与变更记录保持一致。
4. 验收后更新本地 backend 的 AEK 受管资源并检查健康；不修改业务代码或团队策略。
5. 提交、推送与发布；未运行的平台或能力明确披露，保留 v1.0.2 历史标签。

## 发布候选核验（2026-10-10）

工具实现与本地验证已完成：52 套 Python 行为测试、安装/事务恢复、CLI/MCP、文档门禁、
技能一致性、容量/旧 Benchmark 和静态检查通过。文档已按当前范围重新审查并绑定。
本地受管安装更新后健康检查和 Memory 输入契约通过；业务代码不属于本次修改范围。
以下表格和切片记录保留开发时状态，不作为当前待办。外部平台 CI、源提交与最终发布状态
以 GitHub 同一提交的 workflow 和版本标签为准；离线基础不等于真实模型实验。

## 历史里程碑（切片阶段记录）

| ID | 交付与依赖 | 验收 | 当前状态 |
| --- | --- | --- | --- |
| M0 | 前置稳定性修复收口、冻结可重现基线 | 相关回归与最终索引核验，保留原发布标签 | 本地回归通过；未提交 |
| M1 | BM-001～006：场景 schema、独立检查、分层结果、产物与身份、Memory 行为、异常分类 | 空测试/陈旧凭证/伪造文档/不完整检查不产生 PASS；旧入口兼容 | 开发侧基础完成；原生平台/真实运行留在 M2/M6 |
| M2 | 离线执行与 CLI 契约，依赖 M1 | fake 故障/恢复、进程回收、独立目录、Linux 可选 canary、live 拒绝 | 开发侧已实现；live Adapter/网络/treatment 不在本轮 |
| M3 | SC-001～006 合成工程场景，依赖 M1/M2 | 正确/错误实现均有固定独立预期 | 六个 public 场景正/负验证通过；不宣称隐藏或真实 Agent 实验 |
| M4 | 离线事件与身份，依赖 M2/M3 | source/coverage/unknown、身份/顺序/重复样本可追溯 | 两 CLI 解析、身份指纹和轮换计划已实现；不要求在线采集 |
| M5 | 离线 JSON/Markdown 及 WA-001～007，依赖 M4 | 无总分，缺证据 NOT_EVALUATED，流程/产物不覆盖 Outcome | 已连接显式事件/产物输入与汇总；最终回归待收口 |
| M6 | 工具可靠性/性能/兼容回归与发布，依赖 M0～M5 | 安装/升级/卸载、CLI/MCP、容量、证据与恢复回归；更新 backend 后发布 | 待统一验证和收口；无模型配置阻塞 |

## 历史开发记录（不作为当前范围或完成声明）

以下按当时范围保留切片证据，文中 live/成本的“下一步”“待完成”属于历史计划；
当前发布范围和剩余工作只以本文件前两节及修订后 SDD 为准，不能据此恢复在线调用。

### 首个切片：BM-006 缺测试不等于正确

- 稳定接缝：现有 `evaluate_scenario`、`run_tests` 与 `BenchmarkResult.to_dict`。
- 独立预期：空命令/空白命令为 NOT_EVALUATED，执行通过为 PASS，断言失败为 FAIL；超时、缺环境
  等不能取得可信结论的情况为 INFRA_ERROR。旧 success 字段仅在 PASS 时为 true。
- 保留旧入口和布尔返回包装；新结构化结果新增 status。旧 shell 字符串仅来自受信任场景，不接收
  Agent 提供的新验证命令；后续 schema 使用 argv 检查接口。
- 本切片不解决 Artifact 身份与关键词 Memory 的其他误判，不宣称完成 M1。
- 2026-10-08：首个切片已实现，6 个回归通过；空验证 NOT_EVALUATED、超时/启动异常
  INFRA_ERROR、正常 PASS/FAIL 及旧布尔包装均验证。旧 shell 入口的 126/127 与信号退出可标基础设施
  错误，其余非零仍按旧约定为 FAIL；完整环境故障归因由后续受信任检查协议完成。
- 扩展基准曾发现 Standard 材料成本低于原阈值；精简根 Skill 的重复说明后四路线均达标。
  独立原子/CLI/通道等价检查已重做，记录于 complete-equivalence-v1.0.3.json 候选凭证；
  v1.0.2 凭证保留。该结果是受控材料基准，不是真实 Agent Token 或收益实验。

### 当时的实现接缝

M1 已提供 core/scenario、core/result、core/evidence 与 scenario_file/process/check_runner。
旧 evaluate.py 新增 --scenario-file 开发诊断入口，使用固定检查脚本，不执行 Agent 自报测试。
默认不具备可信运行锚时 Artifact evidence 与 experiment validity 保持 NOT_EVALUATED，实际 Token
仍为 UNKNOWN；不从开发检查通过推导真实 Agent 配对收益。

当前目标回归：Scenario 7 tests、independent checks 9 tests、Artifact/receipt 7 tests；根 Python
47 suites 通过；完整 benchmark 在运行器切片通过。后续仍须验证两官方 CLI 的执行、隔离、六场景与实际成本。

### M2 首切片：CLI 能力诊断

- `run.py doctor --agent codex|claude [--cli PATH]` 只执行版本与帮助查询；在独占临时目录中
  执行，有界超时/输出，不读认证值，不启动模型。COMPATIBLE 只表示必需参数被公开帮助声明，
  不是 live-ready；认证和隔离始终 NOT_EVALUATED。
- 本机只读实测：Codex 0.162.0-alpha.2、Claude Code 2.1.294；两者均声明结构化输出、模型选择
  与不持久化会话参数。当前公开帮助均未声明 `--max-turns`，后续不得猜测共同轮数预算可执行。
  未来执行仍须绑定 CLI/配置身份、实际预算能力与隔离证明。
- 7 个回归覆盖两厂商声明、未知版本、错误/超限帮助、非声明文本、未安装程序、非法预算及
  任意 cwd；probe 数据只保留状态/摘要，原始输出不留在结果对象或报告中。
- T5 尚未完成：上述探测不替代 fake/live Adapter、manifest、独立 workspace/session/cache、
  原生进程回收与外部文件/网络边界证明。

### M2 后续切片：固定基线与离线恢复（2026-10-09）

- workspace 从显式 fixture Git 提交读取 blob，拒绝 symlink、submodule、非便携路径与大小/数量
  超限。fixture SHA-256 是 `{"files":[{"path","mode","sha256"},...]}` 的排序 JSON 摘要：按 path
  排序，sort_keys=True、默认 ASCII escaping、紧凑 separators；mode 为 Git 100644/100755，
  sha256 为原始 blob bytes。Git commit 身份单独核验。不能混入 dirty/untracked 文件。
- 两端重新建立相同的 Git 文件基线，各自有 home/cache/session。记录 source fixture commit 和
  新 workspace baseline commit；不复制原仓库全量 Git 历史或宿主配置，不冒充历史复现。
- `run.py paired` 仅执行显式 pinned synthetic Python agent；`--live` 在准备前拒绝。fake 当前只支持
  一次配对、无 turn limit、最长 3600 秒；不支持的预算显式拒绝，不静默丢弃 repeat/max_turns。
- 非幂等执行复用既有 WorkUnit/CAS：STARTED 先于进程，结果/COMMITTED 先于最终状态。未知中断、
  丢状态但有证据、缓存损坏或请求身份变化均不重跑；完成的失败/超时也不是重试授权。
- `--resume-pair` 必须给原 manifest 的外部 SHA-256 锚。跨进程、并发和提交后状态写入中断均有
  回归；真实子进程在控制器 KeyboardInterrupt 时回收，保留异常上抛。
- 19 tests 与根 45 suites 通过，完整 benchmark 和 Ruff I/F/E9/TRY203 通过。formal hidden isolation、
  AEK treatment、live CLI、原生 Windows 回收与实报成本仍未完成；fake 配对始终 NOT_EVALUATED。

### M3 首三场景（2026-10-09）

- 已实现 public synthetic SC-001 分页局部 Bug、SC-002 内存 CRUD、SC-003 并发防重；原始错误
  实现全部 FAIL，独立参考实现全部 PASS。并发样例用同步屏障稳定暴露 check-then-act，
  不依赖“偶然发生竞态”。这些不证明业务性能或其他数据库行为。
- Catalog 只接受已有 ID，在维护者控制的目录生成固定 Git/JSON；整个 verifier 程序（有界协议
  probe + 独立预期数据）作为一个 SHA-256 pin。公开 good reference 不参与运行时预期计算，
  Candidate 不和 verifier 共享 Python 进程；checks/expected 不复制到 candidate workspace。
- 5 tests 与根 46 suites 通过：固定场景身份、三场景正/负行为、假 PASS/文档不得替代行为、
  输出超限为 INFRA_ERROR、不泄漏 canary、未知 ID 拒绝。Case 资源是公开测试，不冒充隐藏实验。
- 下一个切片为 SC-004 SQLite 合成迁移，然后 SC-005 同源历史约束与 SC-006 可查询外部提交。
  此处仍未完成六场景、T5 live Adapter/真实隔离或 T7/T8/T9。

### M3 后三场景（2026-10-09）

- SC-004 SQLite 合成迁移验证历史行/列/唯一性、旧 INSERT 兼容、重复迁移与 schema 后故障回滚；
  不从这个场景推论其他数据库或生产性能。
- SC-005 两端共享同一历史决策文档，基本 Outcome 与约束落实分开检查。错误行为即使写满
  memory 关键词也失败，正确行为无需引用关键词。此结果不推断调用过 Memory/MCP。
- SC-006 使用 verifier 控制的临时回环服务记录非幂等提交，receipt 为随机不透明值；新 worker
  进程必须恢复既有提交而非重复 POST。候选自报计数/回执不作为外部提交真相。
- 六场景 8 tests 与根 46 suites 通过（含 Root 19 runner / CLI 7 tests）；普通沙箱禁止 socket
  时 SC-006 留 INFRA_ERROR，经批准的仅测试环境可验证真实本地服务。未跳过或假判通过。
- T6 开发资源完成；T5 real CLI、隔离/AEK treatment、T7 使用量/事件、T8 报告/分析、T9 原生平台
  与真实实验仍未完成。接下来先实现可离线验证的结构化事件/用量接缝，再推进 live 条件。

### M4 首切片：有来源的用量与事件（2026-10-09）

- core/telemetry 与 collectors/cli_events 解析 single-shot JSONL；run.py collect 只读显式文件，
  默认 capture/session 范围未知。输出绑定 run/pair/task/manifest，正文/错误/本机路径不进报告；
  原始 session ID 仅保留摘要。stage/timestamp 没有可靠来源时为 null，不从工具文字猜阶段。
- 16 tests 覆盖未知/0、缓存口径、全树/主循环、恢复会话、错误/崩溃、截断/重复终态、坏 JSON、
  surrogate/非有限/错误类型、容量/文件边界及任意 cwd；47 suites 全部通过，最后单行类型校验
  补充后目标 16 tests 再次通过。无实际模型调用、无字节换 Token、无综合分。
- [Codex 非交互协议](https://learn.chatgpt.com/docs/non-interactive-mode)的 turn.completed.usage 保留
  CLI 报告范围，缓存/推理不重复加总。[Claude 官方口径](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
  用 final modelUsage 覆盖全树，只有 usage 时标主循环 partial；assistant output 占位不计成本。
  resumed/unknown 不猜本轮增量，错误/崩溃 zeroed totals 不当免费；客户端 USD 始终 is_estimate。
- capture flag 和 fresh mode 在 CLI 诊断中是调用方声明，execution_scope=posthoc_cli_transcript，
  不构成可信 live capture、隔离、配对有效性或 Outcome 证明。Outcome 始终 NOT_EVALUATED。
- 当前是解析器切片，不能标 T7/M4 完成：CLI 版本/配置和真实进程输出绑定、调用时刻记录、完整
  Source/Semantic bridge、配对身份/轮换/重复与实际数据还需实现。多输入流/reset/多 final 不支持且
  fail closed，不能拿 single-shot parser 对累计流做求和。

### M4/M5：配对身份、汇总与有界分析（2026-10-09）

- RunControls/RunManifest 固定 Agent/模型、CLI/配置/环境/权限/预算/fixture/verifier 等指纹；
  严格 JSON 与外部锚独立。不同条件 INVALID，未观测实际模型或缺隔离证明 NOT_EVALUATED。
- schedule 是纯计划：按场景/重复次数轮换顺序，稳定 pair/run ID，限制任务/样本容量；
  不把生成计划当作已启动模型，仍显示 NOT_STARTED。
- 配对 JSON/Markdown 重新核验完整独立检查、脚本/场景身份、外部结果锚；PASS+非零退出码或
  非 CheckResult 输入 INVALID。汇总保留异常原因与所有样本，不混算不同控制条件；重用 run 或
  pair 身份 INVALID，不靠重命名扩大样本。只有有效且两端 PASS/FAIL 的配对计入比较。
- run.py report 只读取显式报告文件，可选外部 canonical SHA-256，缺锚不自动信任内容；
  JSON 为事实源，Markdown 同源渲染。posthoc 汇总不构成真实运行、当前源码或隔离证明。
- WA-001～007 仅基于完整、有外部摘要锚的语义事件；变化后的材料/代码/用途、必要治理和用户
  override 不误报。不同 pair 不合并；finding 有稳定 ID、证据和建议，不自动修改源码。
  原生 CLI tool 名称不等于阶段/用途，无 semantic bridge 时不猜测行为。
- Pair 14、Report 10、Workflow 12 tests 与根 51 suites 通过，原 benchmark、Ruff I/F/E9、
  compileall/diff check 通过。live capture、treatment、过程/产物连接与真实实验仍待完成。

### M2：Linux 离线隔离能力探测（2026-10-09）

- 复用可选 bubblewrap，不自建沙箱或增加安装器默认依赖。隔离策略参照
  [上游安全模型](https://github.com/containers/bubblewrap/blob/main/README.md)，不把装了后端当安全证明。
- isolation-doctor 只在临时目录探测：外部文件、symlink 逃逸、父进程环境、宿主回环网络不可达；
  /usr 只读、workspace 独占可写，PID/user/network 隔离，new-session、cap-drop/clearenv，禁嵌套 userns。
- 本机在获批测试环境四项真实 canary 通过；普通执行沙箱拒绝 namespace。当前后端要求显式
  --unshare-user 才接受 --disable-userns，已最小复现并回归。FIFO/非普通后端不阻塞诊断。
- 仅记录后端/策略/probe/输出摘要，不记录 canary、原始 stderr 或本机路径；live_ready=false，
  model_calls=0。Windows/未安装/无 namespace 权限显式 UNAVAILABLE，不擅自改宿主配置。
- PROBED 只证明这次离线探测；不证明实际 CLI/treatment、受控模型网络、seccomp 或原生 Windows。
  4 tests 通过，后端没有加入 installer REGISTRY；正式隔离证据必须绑定实际运行。

### 本轮核心可靠性与性能增量（2026-10-09）

- 修复 Memory MCP context.purpose 与预算层契约不一致：schema、CLI、Application 共用五个
  受控用途；业务描述放 query。非法值在候选检索前拒绝，不输出 traceback 或错误原文。
- 会话/摘要规则复用 ContextEvent 的既有约束，并前移到文件访问前；会话路径遍历曾导致测试
  目录外的无换行 JSONL 被恢复逻辑截断，已用独立 canary 复现和回归关闭。
- 报告接入 manifest-bound 流程证据并保留到实验汇总；Process/Outcome 保持独立。仅受信任完整
  语义有结论，缺失/陈旧/跨运行不计完成。Report 15、Workflow 13 tests 通过。
- WA-007 改为身份索引，不逐 hidden 结果全表扫描；重复样本改 Counter 计数。256 组配对的
  身份比较红测试为 65792 次，修复后满足 16N 上限；不是对真实 Agent 性能/收益的推论。
- 52 Python suites、MCP 32/0、Skill sync 68/0、旧 benchmark、Ruff/compile/diff check 通过。
  根指引/Skill 只补必要参数契约并压缩重复说明；候选等价凭证重新独立验证，旧阈值/基线不改。
  本轮未更新 backend、未提交/推送/发布，真实运行和产物连接仍未完成。

### 当前发布顺序与产物闭环（2026-10-09）

- 用户明确执行顺序：先修复已知 Bug 与完成 1.0.3，再更新 backend，最后提交/推送/发布；
  不先把零散未收口修复部署到业务安装。不改变原 v1.0.2 标签。
- 产物结果接入单次和汇总报告，复用 IdentityEnvelope/Bound Receipt/evaluate_receipt。
  固定 subject.code/scenario、artifact.plan/documents、policy.governance、context.manifest、
  evidence.verification；缺外部锚保持 NOT_EVALUATED，漂移 STALE，失败 FAIL，错误/重签 INVALID。
  仅保留状态/原因/摘要，不把回执正文或私有字段复制到报告，不覆盖独立 Outcome。
- 新红测试证明 schema_version=true 会被误认作 1；已在共享身份/凭证解码与运行时对象认证层
  修复整数类型检查，不在报告处绕行。Report 19 tests、Identity 6 tests 通过。
- 扩展已有 CLI 模块构造 argv，不自建推理 runtime。模型/提示/容量/选项均校验，提示独立参数，
  拒绝未声明的 turn limit；Claude 显式只读 MCP 配置位置与 settings sources，Codex 控制用户
  配置/规则继承。probe 的命令摘要与启动参数绑定，不能借其他程序的能力声明放行。
- 依据 [Codex CLI 官方参考](https://learn.chatgpt.com/docs/cli/reference)与
  [Claude 官方非交互说明](https://code.claude.com/docs/en/headless)，本机两 CLI 参数解析的只读
  自检 exit 0，未调用模型或读取认证值。CLI 10 tests、完整 52 suites、旧 benchmark、Ruff、
  compileall/diff check 通过。参数解析成功不是认证、实际 CLI 完成或真实隔离证明。
- 仍需完成实际 CLI/受控模型网络与安全认证、live/semantic/evidence 连接、原生兼容和真实
  正确性验收；配置需明确模型/通道/运行上限和安全凭证来源，不能使用聊天中泄露的旧凭证。
  backend 尚未更新，本轮未提交/推送/发布，VERSION 1.0.2 保留。

### 隔离策略到实际执行的连接（2026-10-09）

- 将 canary 与执行器共用 bubblewrap 基础策略，新增 run_isolated_process；后端摘要 pin、
  参数/时限/输出上限、实际 canonical 子目录与四目录不重叠均检查，不允许挂载整个 controller。
  workspace/home/cache/session 映射到独立 namespace 路径，清空父环境，网络固定 deny；
  后端/平台/namespace 不可用不回退到宿主执行。
- 执行结果保留 backend/policy 摘要，原始 process body 不在 repr 中；过程退出码不等于 Outcome
  或模型调用完成，策略摘要也不能独自充当正式 live isolation credential。
- 独立真实 smoke：候选 workspace 可写，controller 下隐藏 canary 不可读且原字节保持，
  /home、/cache、/session 与固定私有环境一致；仅执行本地 Python，未调用模型/使用凭证。
- Isolation 6 tests 通过，仍只实现 closed-network profile；模型专用受控网络、安全认证、实际
  CLI capture/AEK treatment 及原生 Windows live 隔离尚未完成。发布顺序/验收要求不改变。

- `run.py`：list、doctor、paired、report；普通 CI 只运行离线测试，真实模型调用显式触发。
- `core`：Scenario、RunManifest、CheckResult、PairResult。检查与实例身份独立于 Agent 输出。
- `adapters`：复用官方 CLI，不自建推理 runtime；支持的事件/预算由 CLI 版本和能力探测证明。
- `collectors`：默认仅保留结构化摘要与 digest，原始日志脱敏后按显式保留策略存储。
- `analyzers`：重复调用须有同一代码/材料身份与用途证据，不把合理重验误报为重复；阶段或事件
  缺失时不从自然语言猜次数。
- `reports`：配对有效性、成功/失败/未知、分布和证据引用；基础设施异常独立列出，不静默丢弃。
- `benchmark-results/` 默认忽略；不在安装器资源清单中部署实验运行结果或 CLI 依赖。

## 复用依据（2026-10-08 核对）

- 扩展仓库现有 evaluate/context/equivalence/change-scope/Identity/ArtifactPlan，避免平行框架。
- [Codex 非交互文档](https://learn.chatgpt.com/docs/non-interactive-mode)：`codex exec --json` 提供
  JSONL 事件和使用量，支持 ephemeral 执行；使用显式沙箱。隔离能力需另行证明。
- [Claude Code 程序化执行文档](https://code.claude.com/docs/en/headless)：`claude -p` 与
  stream-json 提供结构化运行数据；费用估算和恢复会话的累计口径须分别处理。
- 两种 Adapter 共用可信场景/检查协议，厂商字段只在 Adapter 内转换；未知字段不伪造通用能力。

## 本轮发布声明边界

普通安装与 CI 无在线模型前置条件。保留的解析器、fake、隔离自检和报告 API 是开发诊断，
不等于实际模型任务正确率、Token 节省或竞品排名；未经观测的用量和收益继续保持未知。
