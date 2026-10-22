# 截图观察：按需准备、记录与复用

仅当前任务需要 UI 观察文本时读取。以下入口由 Agent 内部编排，用户继续入口仍为 design-pipeline；不要求用户学习 CLI。无 UI 影响不加载本资源、不调用这些入口。

## 取得当前材料

使用当前获准仓库与需求材料根，不从候选文本接收任意路径。沿用截图 helper 的 `--repo-root`、`--change-id`、`--requirement-dir`；编号中文目录仍对应 CHG-NNN。先完成原图技术校验，读取当前截图索引实际 SHA。不要手改文件、摘要或状态来绕过缺口。

## 准备

内部调用 helper 的 `observation-prepare`，携带当前 `--expected-index-sha256`，UTF-8 JSON stdin：

```json
{"schema_version":2,"evidence_id":"P1","context_ref":"P1-CAPTURED","required_regions":["QUERY"]}
```

ID 必须来自当前索引与本轮真实区域需要；context_ref 只能缩小到已有上下文，不授予跨权限访问。

- `REUSED`：检查返回引用的实际字节 SHA 后消费观察文本，不再重复看图。复用只说明当前图像/契约和区域覆盖匹配，不代表适用性或批准。
- `NEEDS_EVIDENCE`：成功准备返回 prepare_ref、prepare_sha256 和不可变 snapshot_ref。确认具备现有获准图像能力后，对该快照按需读取[提取契约](screenshot-observation.md)并观察；没有能力就保留缺证，不虚构描述或通道。

## 记录

内部调用 `observation-record`，携带同一当前截图索引 SHA；输入 schema_version=2、原 prepare_ref/prepare_sha256、candidate 和 provenance。candidate 只包含提取契约要求的 schema_version/regions/uncertainties；不得自带来源、适用性或批准。provenance 由实际观察方给出 channel（agent_visual/maintainer_visual）、model（未知为 null）、带时区的 observed_at。程序绑定准备摘要，候选不自行填写。

程序重核当前材料、快照与契约，先不可覆盖保存观察，再 CAS 关联。`RECORDED` 表示本次必要区域已观察；`PARTIAL` 仍要消费 coverage.missing_regions，不能说已齐全。association_status=PENDING 时文件只是未关联数据，不可用于基线；重读当前事实后重试关联，不能强行覆盖或机械重跑视觉提取。

新观察及绑定位于已批准 ui-reference/observations 内；显式 v2 侧索引不改旧截图索引 schema1。不得手写这些派生文件、跨项目按 SHA 搜库、自动纳入 Git 或写入通用记忆。权限/需求变化另核适用性；最终必要视觉对照及真实人工确认不因缓存豁免。

## 作用域内检索

只在本轮需要定位已有观察时，内部调用 `observation-query`。沿用当前获准仓库/需求参数；从本任务已明确的权限上下文确定控制参数 `--context-ref`，不能把候选自报的上下文反抄成控制参数。UTF-8 stdin 示例：

```json
{"schema_version":2,"query":"查询按钮","context_ref":"P1-CAPTURED","limit":8}
```

请求 context_ref 必须与控制参数一致，并在当前索引中核实权限、版本、视口及各图的视觉适用性/新鲜度。可用 page（当前截图页面名）和 state 进一步缩小；同名上下文出现不同权限/版本/视口时先补证。area 使用 AREA-N，从当前 UI设计.md 的 aek-ui-baseline 取得截图/观察区域映射，核实际来源后先收窄再查询；它不能扩展控制上下文。同 AREA 跨 PAGE 有歧义、映射缺失或来源过期时明确 NEEDS_EVIDENCE，不从名称猜映射。

状态专属基线先核截图引用与当前实际允许证据的交集，并按控制 context 和实际截图状态筛选，再读取所选来源和观察；声明 context 不能授予来源读取权限。未指定 state 时可以查询该权限下多个实际状态，但不能把 default 图片当 loading 等其他状态的证据。旧共享布局映射也只能缩小原已授权范围，不授予其他权限。

- 返回有界片段、实际观察/图像摘要、来源及 applicability_ref；至多20条、每片段512字，超限或来源失效显示 PARTIAL/gaps/truncated。无命中仅针对 scope_ref 指向的范围，不证明全项目不存在。
- 独立 CLI 尚无 SQLite 运行时采用凭证，明确返回 backend=bounded_files 和 UNAVAILABLE（可带已核实片段）。只有可信宿主提供已采用的内存查询工厂才使用 sqlite_memory；SQLite 故障显式降级，不开放 stdin 后端/数据库路径开关，不把文件降级当 SQLite 验收通过。
- 检索不写索引、不重新看图、不调用模型；返回前再核当前图像、所选上下文、绑定和提取契约。只消费必要命中，勿为同一问题再全库展开；必要评审正文、最终视觉对照和真实确认不能用片段或缓存命中豁免。

## 失败

新入口退出2=输入/路径拒绝，3=CONFLICT/STALE，4=缺材料/能力，5=超时/限额/内部失败；读取 reason_code，不把非零当成功。退出0也只代表本次操作完成，PARTIAL 和未决适用性仍不通过设计门禁。旧入口退出码含义不变。
