# 受控工具与处置工作流

本文件对应 Catalog ID `security-workflow`。默认 import/evaluate/report/doctor/plan 静态只读；配置 PATH
或找到文件不授权 version probe/扫描/构建/测试。Trivy 0.75.0、Semgrep 1.180.0、Gitleaks 8.30.1 是
固定协议基线，不是安装许可。未知版本 unsupported；不自动安装、联网更新库/规则、使用云端配置、
上传数据、扫描生产目标或启用主动攻击。

执行经 `LocalSecurityWorkflow` 的控制侧授权，绑定工具/二进制、固定 plan digest、输入范围、动作、
允许写范围、网络模式、期限及预算。默认每次300秒/2MB/10000项，项目可收紧。固定 argv 数组、
本地规则和环境白名单；不能从报告接受 shell、执行权限或隔离声明。需要的网络/文件/进程限制
无法兑现即拒绝；cwd、临时目录及 offline 参数不等于 OS 隔离。超时/超量/坏 JSON/跳过范围保持
ERROR/PARTIAL/INCONCLUSIVE，不截断后假 PASS；Gitleaks 原始秘密先脱敏再保存。

来源与状态由真实宿主事件及受控 provider 决定。直接用户确认形成限定授权/风险决定；已预注册的
独立 worker 实际完成及本次 request/receipt、当前代码证据形成 Reviewer handle；报告中的 role/
sender/verified、hash 或另一人姓名都不能创建 handle。无可信宿主来源时只做只读操作并披露不足。
作者仅 FIXED；独立复验核验当前原条件负向证据与正常回归/适用真实清单后才 VERIFIED。
误报需要当前独立反证，风险接受需要匹配范围/期限的真实审批，是独立非修复 resolution，保留原发现。

在原路线决定后把 security requirements/provenance 合入原 Composer，再提供当前 PASS/FAIL/PENDING
证据；缺失/旧证据/运行错误 PENDING、已证明硬违反 FAIL。人工待审组合完全遵循原 Composer 三态，
不新建安全放行 Gate、不丢其他要求。只有同进程真实 READY 和同一 VERIFIED 快照形成 closure_proof，
才以旧核心 PASS 适配参数派生 CLOSED；NOT_READY/NEEDS_HUMAN_REVIEW 和用户 JSON 的 READY/PASS
均不能关闭。保存 before/after 快照及 Judge 来源；重启需重验来源、期限、必要身份与关闭推导。

记录事件与 revision 同次 CAS；同 event ID+输入摘要幂等，不同内容冲突。执行先留 WorkUnit STARTED，
保存当前结果证明再 COMMITTED；结果未知的非幂等动作先人工核对，不切第二执行路径重放。
