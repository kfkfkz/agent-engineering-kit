# 原型确认：选择当前状态与权限上下文

仅本轮涉及 UI 原型确认时读取；用户继续入口是 design-pipeline，不让用户复制内部命令。原型和引用截图都需实际预览，观察文本、技术有效或缓存命中不代替视觉核对。没有可用预览/视觉通道时保留缺证，不伪造截图或 ACCEPTED。

## 版本与身份

- 旧 schema1 清单、绑定及确认继续原流程：维护者明确接受实际预览后，记录 maintainer_reply、姓名/时间和摘要，再由维护者在交互终端确认；不把旧批准迁成新状态/权限的批准。
- 新流程显式使用 aek-prototypes schema_version=2。每项保留 prototype_path/prototype_sha256、PAGE/AREA、F/A、change_intent、depends_on、artifact_plan_id/gate_digest/approved_root、evidence_contexts、design_system_sha256，另加 state_id/context_id。state_id 使用实际截图的 default/loading/empty/error/dialog/drawer/permission；context_id 必须是各依赖中实际适用的同一上下文。不同状态或权限各一项，不改 AREA 来假装身份独立。
- v2 候选清单不填 status/reviewer/reviewed_at/confirmation_source/dependency_sha256；程序从实际内容计算依赖，真实交互凭证是接受事实，文档自身不批准自己。截图反向引用使用 bind-prototype 显式 schema2，携带 state_id/context_id；当前索引 SHA 做 CAS，不手写派生绑定。

## 准备与一次决定

Agent 内部先调用只读入口：

```text
doc-gate prototype-prepare <SDD目录> --page PAGE-1 --area AREA-1 --state default --context <当前上下文ID> --json
```

PREPARED 只说明指定对象在本次读取时通过核验，不是批准。保留 prepare_sha256 及 per_item 中实际原型/依赖/域摘要；缺证或变化时补当前材料后重新准备，不自己编摘要。

需要将多个状态或权限上下文集中展示时，沿同一准备入口以 `--preview-html` 替代 `--json`，所选单项或 `--target` 集合不变。返回完整 UTF-8 静态 HTML，只展示选中证据；原无格式参数的人类摘要、显式 JSON 对象及准备摘要不变，两种格式参数不可同时使用。

- HTML 含引用截图，按敏感业务材料在授权的本机位置预览，不自动纳入 Git、上传或注入通用记忆。入口不写文件、不启动浏览器，也不写确认凭证。
- 缺少当前可信渲染结果时明确显示 UNAVAILABLE/unknown；参考图存在不等于已渲染原型，更不等于视觉一致或 ACCEPTED。仍需实际可视核对，不能用观察文本或报告摘要代替。
- 生成和最终依赖复核失败时退出3，stdout为空，stderr有界诊断；重新核对当前材料后重试。尚未形成合法请求的原 CLI 语法错误仍退出2，不据此降低证据要求。

向维护者展示实际 HTML、截图与所选状态/权限，再在维护者可直接操作的输入和显示终端准备内部确认：

```text
doc-gate prototype-confirm <SDD目录> --schema-version 2 --page PAGE-1 --area AREA-1 --state default --context <当前上下文ID> --prepared-sha256 <实际准备摘要> --reviewer <维护者姓名>
```

维护者仅在该终端作一次明确选择：接受输入显示的精确 ACCEPT 短语；REVISE/BLOCK/CANCEL 不写接受凭证。Agent 不代输、不用管道或伪终端自动接受，不再要求先在聊天写一次 ACCEPTED。TTY 约束不等同 OS 实名或密码学签名。

- 接受前程序重验实际原型及依赖；发生变化则 CONFLICT，重新预览准备，不套用旧短语。
- 凭证逐项不可覆盖；当前有效项重试直接恢复派生结果，不要求再次同意。PENDING/CONFLICT 时重读当前材料及原凭证，不删除历史文件来强制通过。
- REVISE/BLOCK 由现有 Review Issue/Doc Gate 修订、复核和判断；返回状态不自动新建或关闭 Issue。取消也不算通过。

## 多项选择与恢复

同一轮可用重复 `--target PAGE/AREA/STATE/context` 显式选择1–32项，不能与单项参数混用。不默认选择全部页面；只确认维护者实际预览的项目。Agent 内部准备示例：

```text
doc-gate prototype-prepare <SDD目录> --target PAGE-1/AREA-1/default/CTX-A --target PAGE-1/AREA-1/loading/CTX-A --json
```

确认仍需显式 `--schema-version 2`、本次 `--prepared-sha256`、真实维护者姓名和输入/显示终端；使用相同 `--target` 集合代替单项参数。程序排除已有当前凭证，展示尚待确认的完整集合及摘要，维护者一次输入显示的精确短语。Agent 不代输。集合超过48KiB时缩小选择，不截断后假称完整。

- 每项凭证保留其身份、内容/依赖摘要和本次实际选择集合；一项成功不是整批原子通过。逐项查看 `per_item`，`CONFLICT/PENDING` 不算接受，不能仅凭退出码或其他项成功放行。
- 某项在等待期间变化或保存失败时，不批准其旧版本；其余已完成事实保留。重读/预览未完成项后重新准备，重试只请求这些项，不删除或覆盖历史凭证。
- 已完成项的当前性仍由原 Gate 重验。旧集合内另一项发生变化，不自动撤销无依赖关系的当前项；共享截图、Token等实际依赖变化仍会传播。

新旧确认均绑定实际原型与依赖；截图、观察、组件/Token、权限或文档依据变化，只使依赖它们的结论失效。共享依赖可以影响多个对象，无关上下文保留。最终仍走原 Doc Gate，不新增审批框架。
