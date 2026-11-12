# 业务流程图交付

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
