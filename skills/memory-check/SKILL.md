---
name: memory-check
description: "核验已有项目记忆与当前代码是否一致。支持全量、按符号增量或按 Git/SVN 变更巡检。"
---

# 记忆巡检

绑定用户指定的仓库。未指定时核对当前项目根。目标须同时有 `.repo-memory-kit/manifest.json` 与 `docs/memory/RULES.md`。专用工作区入口可通过技能真实路径确认所属仓库。目标仍不唯一时先确认。

按目标 `docs/memory/RULES.md` 的「巡检步骤」执行，先核对图谱新鲜度与覆盖。工具不可用时停止，除非用户明确授权降级。

- 无参数且明确发起巡检时核验全量条目。
- 给出类、方法、文件或路由时按 `.repo-memory-kit/.anchors.json` 反查。索引缺失或未命中时核实相关条目，只检查有证据表明受影响的内容。
- 按变更巡检用目标仓库的 `memory-build --changed [ref]`、`--since <ref>` 或 stdin 的 `--files`。需要影响报告时用 `--report <路径>`，`--strict` 遇影响项退出 2。需要符号级精度时再核对图谱。

只维护目标的 `docs/memory/` 条目。退役和替代状态须人工确认。锚点有调整时运行 `memory-build`。报告已矫正、已压缩和待确认项。代码缺陷另走修复流程。
