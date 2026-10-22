# AEK 使用指南

先从 [README](README.md) 安装并运行第一个任务。本文供需要配置、维护或排查 AEK 的使用者查阅；设计理念与架构见 [Overview](overview.md)。

## 安装选项

以下命令在 AEK 源码目录执行，需要 Python 3.10+。原生 Windows 将 `./install.sh` 替换为 `python -m installer`。

```bash
# 只预览安装计划
./install.sh --dry-run /path/to/project

# Codex 从父级 workspace 启动时，让项目技能在该目录可见
./install.sh --codex-root /path/to/workspace /path/to/workspace/project

# 明确使用受管副本，适用于不能使用 symlink 的环境
./install.sh --codex-root /path/to/workspace --link-strategy copy /path/to/project

# 使用 lightweight 治理 profile
./install.sh --lightweight /path/to/project

# 安装或配置 codebase-memory MCP（需要联网）
./install.sh --with-codebase-memory /path/to/project

# SVN 项目同时初始化版本控制规则
./install.sh --svn /path/to/project
```

基础安装可离线执行；`--with-codebase-memory` 使用 [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) 的安装或配置入口。

POSIX 默认使用 workspace 技能链接，Windows 默认使用副本。Windows compatible 文件后端采用 reparse-point 预检；若需要针对恶意本机并发进程的 POSIX 文件安全保证，使用 WSL。macOS 使用 POSIX 后端，自动 CI 平台为 Ubuntu 和 Windows。

安装后重新打开目标项目的 Agent 会话。Codex 从 workspace 根目录启动时，应在更新时继续传 `--codex-root`，保证新技能链接或副本也被创建和核验。

## 项目记忆与代码图谱

AEK 的记忆 MCP 与 codebase-memory MCP 处理不同的信息：

| 查询内容 | 优先通道 | 回退条件 |
| --- | --- | --- |
| 历史决策、坑点、业务域知识、SDD、旧回执 | AEK `memory_recall` MCP | 调用前确认不可见或不兼容，才使用等价 CLI；两者不可用才做有界文本检索 |
| 符号、调用方、依赖、数据流、影响范围 | codebase-memory 图谱 | 对索引报告的缺口读精确源码；能力不足时披露原因与结论限制 |
| 字面量、错误文案、配置值、非代码文件 | 有界文本检索 | 不需要为了文本搜索刷新结构图谱 |

代码编辑、生成器写入、Git/SVN 更新、切分支或合并后，Agent 在下一次结构查询或交付前重新核对索引。后台已追平时复用；否则同一变更批次最多显式刷新一次，再检查覆盖情况。配置文件存在只证明 configured，不能证明工具 visible、仓库 indexed 或图谱 fresh。

AEK 注册的 MCP 工具为 `memory_recall`、`memory_build`、`domain_check`、`kit_status`，均绑定安装目标仓库。codebase-memory 的工具由其独立运行时提供。

语义检索的可选依赖是 `zvec`。将它安装到 MCP 使用的 Python 环境后，在项目目录重建索引：

```bash
python -m pip install zvec
.repo-memory-kit/bin/memory-build .
```

正常任务由 Agent 调用 MCP。需要本地诊断时，可以使用下列 CLI；Windows 用 `python` 显式启动这些无扩展名 Python 脚本。

```bash
.repo-memory-kit/bin/memory-recall "任务描述" .
.repo-memory-kit/bin/memory-recall --list .
.repo-memory-kit/bin/memory-build --changed .
.repo-memory-kit/bin/domain-check .
```

记忆保存在 `docs/memory/<业务模块>/`，类型由 frontmatter 声明：pitfall、decision、playbook。frontmatter 是事实源；本地索引、符号锚点和语义索引由工具生成。使用 `memory-capture` 提出候选，经维护者确认后入库；`memory-check` 巡检条目与当前代码的一致性。业务域地图登记能力入口与业务链路，playbook 以链接引用它。

## 工作流与设计文档

通常由 `repo-delivery` 从任务开始推进到交付。Claude Code 使用 `/repo-delivery`，Codex 使用 `$repo-delivery`；其余专项 Skill 使用同样的客户端前缀。

```text
任务与项目约束
  → 选择工作量路线 + 独立判断风险要求
  → 按需取证、澄清和设计
  → 实现与测试
  → 审查、验证与交付回执
  → 回写实际结果、保留可恢复状态
```

Direct/Bounded 不默认生成完整 SDD。Standard/Initiative 根据 ArtifactPlan 的 required/optional/skipped 决定产物；无 UI 或数据库变化时，可以通过可信事实跳过对应设计。

需要完整设计时，目录按需求与产物归属组织：

```text
docs/01-需求/
  README.md                       需求指派索引
  NNN-名称/原始需求.md              维护者提供的需求原文

docs/03-SDD/NNN-名称/
  需求分析.md                      场景、验收项与人工确认
  概要设计.md                      功能设计
  UI设计.md                        按需产出
  业务流程设计.md                   人工评审与 archify 流程图
  详细设计.md / API设计.md / 数据库设计.md
  任务清单.md / 测试方案.md
  线上联调功能验证清单.md / 终稿.md   真实验证与 As-Built
  reviews/                         评审输入、问题与门禁凭证
  diagrams/                        图表源、HTML 与验证证据
```

`design-pipeline` 编排概要设计 → 按需 UI → 业务流程 → 详细设计 → 计划。AI 评审提交问题与证据，确定性门禁检查结构、追踪链、阈值和凭证；需求确认与业务流程签核由维护者给出。通常只需回复评审结论和签核人，由 Agent 记录并推进。

问题修复后重新验证；只有匹配当前版本的 PASS gate 才能关闭问题。上游变化使相关下游证据失效；影响边界无法证明时进行完整复核。达到配置的迭代上限后保留问题历史并转人工处理。

1.0.5 在原有 `design-pipeline` 内部启用新版评审和简洁预检，旧默认调用保持兼容。
完整送审材料可在首轮发现问题后复用；目标与必要背景分别规划，背景正文仍交付，缓存不代表批准。
全局规则、真实上游内容或范围证明变化时完整复核；已全文覆盖却仍未定位的问题等待合法定位，
相同失败且无新相关证据时停止重试，恢复保留问题与原三轮上限。摘要仍可追溯完整检查，
实际交付字节与不可观察的宿主/模型用量分别报告，不据此承诺 Token 收益。

计划首次通过时保存执行前基线。执行期可更新 checkbox 和执行记录；任务定义、预计范围或验证方式变化仍需重新评审。完成后统一 closeout，绑定任务清单和测试方案的完整终态哈希。

### UI 截图与原型确认

UI reference 与 `doc-gate init` 使用相同的 `NNN-名称` 需求目录。例如目录 `003-查询列表` 对应机器 ID `CHG-003`：截图请求 JSON 记录 `change_id: "CHG-003"` 和 `requirement_dir: "003-查询列表"`；后续 helper 命令同时传 `--change-id CHG-003 --requirement-dir 003-查询列表`。原型的批准目录仍为 `docs/03-SDD/003-查询列表`。旧 schema1 索引没有 `requirement_dir` 时继续使用 `change_id` 作为目录，不迁移已有材料。

截图位于 `docs/01-需求/003-查询列表/原始材料/ui-reference/`。校验会分别报告技术有效性、视觉适用性、新鲜度和 Git 暂存风险；暂存提示是只读诊断，工具不会修改 Git 配置、索引或自动提交图片。原型确认仍须在交互终端绑定当前原型与依赖摘要。冻结 ArtifactPlan 要求 UI 原型时，文档删掉 marker 或确认清单仍会失败。

UI 文件操作在 POSIX 使用 no-follow 目录句柄，在原生 Windows 使用固定祖先句柄、reparse-point 拒绝、二进制文件句柄、原子发布及进程锁。Windows 目录没有 POSIX 的 fsync 保证；映射的 SMB 盘符无法取得安全卷身份时会拒绝，需改用受支持的本地路径。网络文件系统与跨主机并发不在本次验证范围内。

1.0.8 的用户入口仍是 `design-pipeline`，不需要手工串联 helper 命令：

- 按本次页面、关键状态和权限上下文收集最小截图集；可以先推进材料已齐的目标，不必等待无关占位图全部替换。
- Agent 按需将图片理解为结构化观察文本，区分观察、推断和未知；图片及提取契约未变、所需区域足够时直接复用，不重复调用视觉模型。截图适用性和人工批准不会因缓存命中而继承。
- 原型引用实际组件、布局、设计令牌和观察摘要，标清保留与修改区域。查询只读取当前授权的页面、状态和区域；观察文本与原图同级敏感，不自动进入 Git、通用记忆或远端。
- 通过原准备入口输出所选目标的集中静态 HTML 对照；缺少可信渲染时明确显示“不可用/可比性未知”，不会把参考截图当成原型渲染结果。预览不启动原型脚本、不自动批准，也不自动打开浏览器。
- 预览后对当前选择作一次明确人工决定；显式 v2 区分页面、区域、状态和权限上下文，逐项绑定当前摘要。材料变化只使相关项待复核，重试不要求重复确认仍有效的项。

本地观察查询默认使用有界文件过滤。可信宿主可提供已核验的 Python/SQLite 运行时和当前来源校验，启用私有内存索引；不接受请求自报 `adopted=true` 或任意数据库路径，也不因索引不可用而返回旧缓存为有效命中。静态渲染的首个受控运行时为 Linux，还需 Python 3.13+ 提供 `os.POSIX_SPAWN_CLOSEFROM`（及对应系统支持）以安全关闭未授权 FD；缺接口不启用、不自动升级 Python。基础 CLI 仍支持 Python 3.10+，没有合格渲染运行时也可准备、查看和确认静态 HTML，不声称已完成截图渲染。

### 中断恢复的边界

WorkUnit 已支持工作流状态恢复与 CAS 接管，恢复后按当前身份和回执决定复用、重验或转人工；
宿主 Agent 的完整对话、进程和工具会话恢复属于后续能力。

非幂等动作的工作流状态变为失败或失效，不代表动作被撤销。可信 COMMITTED 回执且当前身份有效时
复用结果；提交已确认但身份变化时保留提交事实并转人工核对当前任务。仅有 STARTED、缺回执或外部
结果未知时禁止自动重做，需查询外部事实。外部接口的安全重试依赖其提交查询或幂等键；本地状态
无法保证任意外部动作 exactly-once。MCP Dispatch 内存回执仅在同一服务进程内去重。

### 图表与验证

archify 随 AEK 分发，需要 Node.js ≥18。业务流程图的交付链为类型化 JSON → 校验 → 自包含交互 HTML 与哈希回执 → 视觉检查。自动截图需要 Chrome/Chromium；图表内容仍要核对业务事实。

仅在 Node.js 缺失或版本不足、doctor 明确报告 DEGRADED 时允许披露后回退 Mermaid；路径、权限或渲染失败需要排查。图表校验通过不能代替架构或业务正确性评审。

## 安全审查与依赖治理

1.0.4 扩展 `security-review`，按实际变更范围审查应用、Agent 配置和依赖。安全风险独立追加检查；存量全量审查需要明确范围请求。用户入口仍是 `/security-review` 或 `$security-review`。

依赖采用在设计阶段先核对准确包、拟版本、用途、作用域、适用公告、环境/API 兼容性及风险处置。真实解析或发布物尚未产生时，记录独立的交付待验项；交付再核对 RESOLVED/SHIPPED 清单、直接/传递关系和实际产物。缺可靠公告、清单或当前证据保持 PENDING，不能推导“无漏洞”。公告核验默认最长七天，项目策略可显式调整。

安全记录带 revision 与 CAS，JSON/Markdown 报告从同一 revision 派生，报告不反写状态。作者声明 FIXED 后，需要匹配当前材料与来源的独立复验才能 VERIFIED；风险接受需要真实直接用户审批，误报需要独立反证。关闭须通过原 Delivery Gate Composer 的真实 READY，并绑定同一记录快照，保留其他交付要求。业务安全不变量仅在需求/设计或可核验用户来源确认后形成约束；模型推导保留为候选。基线没再次命中但覆盖不足时保持未核验。

`security-check` 是 Agent 内部机械入口，仅接受有界 JSON 标准输入，提供只读 evaluate/report/doctor/plan。doctor 检查文件或 PATH 配置不启动版本探测，plan 不执行扫描。工具位置、报告中的角色/verified/READY 字段、姓名或摘要不能产生执行权、审批权或关闭权。

可选协议基线为 Trivy 0.75.0、Semgrep 1.180.0、Gitleaks 8.30.1，使用固定参数数组、本地配置和受限环境；不自动安装、下载数据库/规则、上传或主动攻击。协议测试不能证明真实扫描器检出能力，版本冒烟也不等于漏洞扫描。日常仍按变更与策略取证，不因启用本版而强制全量扫描。

1.0.8 提供本机终端宿主及固定 Linux 工具适配，供可信控制侧显式配置：展示固定计划、从真实终端获取限定批准，沿原 Authority/Source 记录完成并恢复；启动结果未知不自动重跑。运行时、当前身份、采用来源、私有宿主日志根和密钥不能由普通请求 JSON 指定。Maven 仅支持固定离线 tree 解析；模型/profile 闭包或扫描实例映射未证明时仍为 PARTIAL，不从依赖树推断 SHIPPED。Trivy 需要合格数据库及原授权版本探测证据，冷启动新 adapter 不能仅凭旧计划恢复版本缓存；既有来源仍可独立只读核验。

默认 `HostEventSource` 与通用进程执行器仍不自动提供宿主授权或文件/网络隔离；缺少合格固定 provider、当前来源或隔离时明确不可用。普通 CLI/MCP 保持只读，不授予扫描、审批或关闭权限。原生 Windows 尚无上述本机执行 profile，保留原只读/人工路径。源码审查和补验建议仍通过 `security-review` 完成，不需要用户操作 SDK 内部对象。

### 显式采用 v2 安全策略

安装、更新、repair 和 doctor 都不会把团队已有 v1 `governance.json` 自动迁移为 v2。团队决定采用后，在 AEK 源码目录运行以下只读预览，保留原 rules，并追加当前安装器生成的 capability guard 与完整安全配置：

```bash
python3 - /path/to/project/.repo-memory-kit/governance.json <<'PY'
import json
import sys
from pathlib import Path
from installer.governance import (
    compare_governance_defaults, default_security_governance_policy,
)

current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if (not isinstance(current, dict) or set(current) != {"version", "rules"}
        or type(current["version"]) is not int or current["version"] != 1
        or not isinstance(current["rules"], list)):
    raise SystemExit("需要人工核对当前策略；本例仅适用于标准 v1 结构")
compare_governance_defaults(current)
candidate = default_security_governance_policy()
candidate["rules"] = [candidate["rules"][0], *current["rules"]]
compare_governance_defaults(candidate)
print(json.dumps(candidate, ensure_ascii=False, indent=2))
PY
```

Windows 用 `python` 运行等价脚本。审阅候选后，手工更新团队策略并重跑安装 doctor。v2 顶层为 `version`、`rules`、`security_review`；guard 的 `require` 为 `{"schema_version":1,"capabilities":["security-review-v1"],"actions":[]}`，用于拒绝不认识该能力的旧加载器，不能简化成普通字符串数组。

默认安全策略为 targeted、dynamic_verification=disabled、required_scanners=[]、optional_scanners=[trivy, semgrep, gitleaks]，预算为 300 秒、2,000,000 字节、10,000 项；未知或缺失配置字段拒绝解析。工具可选不等于治理要求的证据可选：一旦当前策略要求某项扫描或验证，缺工具、未运行、部分结果或过期材料仍需补验，不能视为 PASS。策略显式采用也不授予执行权限或补足宿主隔离能力。

## 安装产物与团队协作

| 位置 | 用途与所有权 |
| --- | --- |
| `.claude/skills/`、`.agents/skills/` | 项目 Skills，两棵树由 AEK 同步 |
| `.repo-memory-kit/bin/`、`lib/` | 项目内工具与运行代码 |
| `.repo-memory-kit/governance` | strict/lightweight profile 权威标记 |
| `.repo-memory-kit/governance.json` | 首次写入的团队规则，后续由团队维护 |
| `AGENTS.md`、`CLAUDE.md` | AEK 维护受管区块，用户保留其余内容 |
| `docs/memory/` | 规则与模板由 AEK 管理，条目和 README 用户笔记归项目 |
| `docs/memory/constitution.md` | 首次补种的通用协作宪章，归用户维护；升级不覆盖、卸载保留 |
| `.mcp.json`、`.codex/config.toml` | MCP 配置片段，含本机路径 |
| `.repo-memory-kit/` 中的 manifest、事务、索引与状态 | 每机派生物，不提交版本库 |

更新和卸载依据 manifest 与内容血统操作。用户记忆不会被删除；被手工修改的受管文件会按冲突保护。卸载通常只移除容器中的 AEK 片段；若 manifest 证明根指引容器由 AEK 创建，且移除区块后没有用户内容，才删除该空容器。

copy 安装的安全工具与 reference 也受相同生命周期保护；更新、repair 或卸载不能仅凭文件名覆盖用户修改或未知血统内容。

strict/lightweight 控制治理与回执形态，任务路线控制工作深度。团队 `governance.json` 可以追加检查和凭证要求；非法规则会阻断，升级不会覆盖已有团队规则。SQL/ORM 变化先做数据库无关初查，确认目标方言、N+1、结果边界、索引和锁风险；涉及规模、热点或迁移风险时再要求专项性能证据。

团队共享 Skills、记忆条目与设计文档，每台机器各自安装本地工具/MCP。维护者统一升级 AEK 版本，其余成员重跑同版本安装。Git 使用受管 ignore 区块；SVN 使用 `--svn` 合并 ignore 属性、登记确定性产物及设置文本换行属性。

## 已有 Spec Kit 文档

基础安装只发现并报告 `.specify/specs`，不自动修改文档。使用 `spec-migrate` 检查和整理，显式迁移前先预览：

```bash
./install.sh --dry-run --migrate-specify /path/to/project
./install.sh --migrate-specify /path/to/project
```

迁移保留原文，补缺失结构并标为 `migration-pending`；Agent 仍需结合代码、测试和业务判断完成语义核验。附件不自动改写。

要将单个 feature 移入 SDD 布局，必须明确 feature 名与版本；该操作会移动原目录：

```bash
./install.sh --migrate-specify=001-demo --sdd-layout --sdd-version V1.0 /path/to/project
```

## 更新检查与卸载

从稳定版标签克隆的 AEK 处于 detached HEAD。新版本发布后先获取标签，切换到已核对的发布版本，再更新目标项目。下例以当前稳定版为例；升级时替换标签：

```bash
git fetch --tags
git switch --detach v1.0.5
./install.sh --update /path/to/project
./install.sh --doctor /path/to/project
```

从 main 分支克隆的开发副本可用 `git pull` 更新源码。存在本地改动时先检查并处理，避免覆盖。

doctor 为只读检查，可报告 HEALTHY、OUTDATED、DRIFTED、DEGRADED、CONFLICT、INCOMPLETE。宿主 MCP 的 visible/indexed/fresh 信息可能为 unknown，需要 Agent 实际调用工具核验。

doctor 还会比较团队治理规则与当前默认规则，列出缺失和不同的规则 ID。`policy_update_available`
是信息提示，不改变安装健康状态或退出码，也不意味着团队策略更弱。旧文件的默认规则来源没有
可靠记录时显示 `baseline=unknown`；该比较不修改文件。团队确认后才手工采用需要的规则。

```bash
# 按报告修复有 AEK 血统的缺失/漂移产物
./install.sh --repair /path/to/project

# 清单丢失时显式登记可识别的 legacy 安装
./install.sh --adopt-legacy /path/to/project

# 仅在报告要求人工处理未完成事务时，选择恢复策略
./install.sh --recover=rollback /path/to/project
# 或：./install.sh --recover=roll-forward /path/to/project

# 卸载受管资源，保留用户记忆和用户内容
./install.sh --uninstall /path/to/project
```

`--adopt-legacy` 拒绝覆盖已有 manifest；恢复策略需依据事务报告选择。卸载不会移除机器级 codebase-memory MCP。

## 开发与验证

[CI 工作流](.github/workflows/ci.yml) 是自动验证清单的来源，覆盖 Ubuntu 全量回归、ShellCheck、archify 实际执行和原生 Windows 生命周期。新增 Python 行为套件由测试运行器自动发现。

固定版本兼容测试会读取历史提交源码。若使用 README 的浅克隆，开发回归前先运行
`git fetch --unshallow` 取得完整历史；CI 使用 `fetch-depth: 0`。

```bash
python3 tests/run-python-tests.py
sh tests/skill-sync.test.sh
sh tests/doc-gate.test.sh
sh tests/installer.test.sh
sh tests/benchmark.test.sh
```

测试依赖按 CI 环境准备。性能与上下文 fixture 用于验证行为、容量和材料约束，不代表真实 Agent Token 节省或产品收益。开发侧离线诊断不要求连接模型服务。
