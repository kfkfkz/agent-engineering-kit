#!/bin/sh
# shellcheck disable=SC2015,SC2188  # 断言惯用法
# doc-gate 测试：硬校验（结构/追踪链/薄输入/上游冻结）、gate 判定（阈值/迭代上限/
# 冻结哈希）、status 状态派生、governance-eval 引擎布线。
set -e
KIT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
pass=0; fail=0

ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }
assert_eq() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望=$3 实际=$2）"; fi; }
assert_grep() { if grep -q "$2" "$3"; then ok "$1"; else bad "$1: 在 $3 中未找到 $2"; fi; }
assert_exists() { if [ -e "$2" ]; then ok "$1"; else bad "$1: $2 不存在"; fi; }

run_rc() { rc=0; "$@" >/dev/null 2>&1 || rc=$?; }

# ════════════════ 夹具：完整合法需求链 ════════════════
REPO="$T/repo"
D="$REPO/docs/03-SDD/010-用户状态管理"
mkdir -p "$D"

cat > "$D/需求分析.md" <<'EOF'
# 需求：用户状态管理

| 字段 | 内容 |
| --- | --- |
| 需求序号 | 010-用户状态管理 |
| 指派人 | 实现人甲 |
| 状态 | 草拟 |
| 关联需求 | 无 |

## 背景

存量用户无停用能力，运营需要控制风险账号。

## 目标

1. 支持停用与启用用户。

## 非目标（边界）

- 不做注销。

## 场景与验收依据

### 场景 1：停用用户

- **前置**：管理员已登录
- **操作**：在用户列表点击停用
- **结果**：用户状态变为停用，列表即时刷新

### 场景 2：停用用户访问系统

- **前置**：用户已被停用
- **操作**：该用户请求任意接口
- **结果**：返回 403 错误码

## 验收项

- [ ] A1：列表页可停用用户（场景 1）
- [ ] A2：停用用户请求被拒（场景 1、场景 2）

## 非功能要求

- 性能：N/A-低频操作
- 安全与敏感信息：仅管理员可操作
- 兼容性：无存量数据迁移
- 数据一致性：N/A-单表变更

## 待确认事项

| ID | 问题 | 影响 | 阻塞 | 负责人 | 状态 |
| --- | --- | --- | --- | --- | --- |
| OPEN-1 | 无 | 无 | NO |  | OPEN |

## 冻结记录

- 冻结状态：已冻结
- 确认人：维护者甲
- 确认时间：2026-09-15
EOF

cat > "$D/概要设计.md" <<'EOF'
# 概要设计：用户状态管理

## 设计目标

本设计覆盖的验收项：A1、A2

## 现状分析

- 相关模块与职责：user 模块 | 用户管理 | 增加状态流转

## 总体方案

停用即置位。

### 主流程

1. 管理员操作
2. 状态落库

### 异常流程

无。

## 功能点设计

### F1：用户状态管理

- 覆盖验收项：A1、A2
- 输入 → 处理 → 输出：状态变更
- 异常行为：并发冲突报错

## 方案取舍

### DEC-1：状态字段位置

- 问题：状态放哪
- 候选：A 扩展现有表 / B 新表
- 选择与原因：A 简单
- 代价：无

## 数据与接口影响

| 对象 | 类型 | 变化 | 兼容性说明 |
| --- | --- | --- | --- |
| t_user | 数据 | 修改 | 无 |

## 风险

| ID | 风险 | 触发条件 | 应对 |
| --- | --- | --- | --- |
| RISK-1 | 无 |  |  |
EOF

cat > "$D/详细设计.md" <<'EOF'
# 详细设计：用户状态管理

## 设计范围

覆盖：F1；对应验收项：A1、A2。

## 业务逻辑

### D1：状态流转（对应 F1 / A1）

- 前置条件：用户存在
- 处理步骤：1. 校验 2. 置位
- 状态流转：正常到停用
- 事务边界：同事务
- 并发与幂等：版本号控制

## 异常处理

| 场景 | 系统行为 | 是否重试 | 对外结果 |
| --- | --- | --- | --- |
| 数据库异常 | 回滚 | 否 | 500 |

## 安全设计

仅管理员。

## 可观测性与配置

- 日志点与错误码：停用失败错误码
- 新增配置：无

## 发布与回滚

- 部署顺序与兼容窗口：先库后码
- 回滚条件与步骤：字段保留
EOF

cat > "$D/API设计.md" <<'EOF'
# API设计：用户状态管理

## 接口清单

| 编号 | 接口 | 类型 | 变化 | 关联功能点 |
| --- | --- | --- | --- | --- |
| D2 | 停用用户 | HTTP | 新增 | F1 |

## 接口详细设计

### D2：停用用户接口（对应 F1 / A1）

- 协议与路径：HTTP POST /user/disable
- 权限：管理员
- 请求字段：用户标识
- 响应字段：成功标志
- 错误与幂等：409
- 兼容性：无影响
EOF

cat > "$D/数据库设计.md" <<'EOF'
# 数据库设计：用户状态管理

## 表变更清单

| 编号 | 表/对象 | 变化 | 关联功能点 |
| --- | --- | --- | --- |
| D3 | t_user | 修改 | F1 |

## 详细变更

### D3：状态字段（对应 F1 / A2）

- 表结构：状态 短整型
- 索引与约束：无
- 历史数据与迁移：默认 0
- 回滚方案：字段保留
EOF


cat > "$D/业务流程设计.md" <<'EOF'
# 业务流程设计：用户状态管理

## 修订记录

| 日期 | 修订说明 | 评审变更 |
| --- | --- | --- |
| 2026-09-15 | 初版 |  |

## 1. 简介

### 1.1 目的

在既有用户管理基础上增加停用能力，形成账号风险控制闭环。

### 1.2 范围

本期支持停用与启用；不做注销。

### 1.3 术语说明

| 术语 | 说明 |
| --- | --- |
| 停用 | 账号进入不可登录状态，可恢复 |

## 2. 业务流程总览

### 2.1 端到端业务闭环

管理员发起停用，系统即时生效，被停用用户访问被拒。

### 2.2 业务流程图

```mermaid
flowchart TD
    A[管理员停用] --> B[状态生效]
    B --> C[访问被拒]
```

### 2.3 异常与兜底

停用失败时提示管理员，账号状态不变。

## 3. 功能设计

### F1：用户状态管理

#### 3.1.1 界面设计

用户列表增加停用操作。

#### 3.1.2 业务逻辑设计

停用即时生效，被停用用户请求被拒。

#### 3.1.3 性能设计

沿用现有基线。

#### 3.1.4 安全性设计

仅管理员可操作。

#### 3.1.5 三方依赖

无。

## 4. 数据与接口概览

| 对象 | 变化 | 业务影响 |
| --- | --- | --- |
| 用户 | 修改 | 停用后不可登录 |

## 5. 评审记录

- 评审结论：通过
- 评审人：评审人甲
- 评审时间：2026-09-15
EOF

cat > "$D/任务清单.md" <<'EOF'
# 任务清单：用户状态管理

## 任务

### T1：实现停用接口与状态流转

- 对应设计：D1、D2
- 目标：停用能力可用
- 预计修改范围：src/**/user/**
- 原则上不动：payment/**
- 实施内容：
  - [ ] DDL
  - [ ] 接口
- 验证方式：python -m pytest test/user/
- 完成标准：测试通过

## 执行记录

| 任务 | 状态 | 实际改动范围 | 验证结果 | 备注 |
| --- | --- | --- | --- | --- |
| T1 | TODO |  |  |  |

## 回归范围确认

- [ ] 用户列表回归

## 完成标准

- [ ] 全部任务 DONE 且验证证据已落执行记录
EOF

cat > "$D/测试方案.md" <<'EOF'
# 测试方案：用户状态管理

## 测试接缝

| 层级 | 接缝 | 说明 |
| --- | --- | --- |
| 单元 | service | 状态规则 |
| 集成 | api | 契约 |

## 用例清单

| ID | 用例 | 对应验收项 | 层级 | 前置数据 | 预期（可观察） | 自动化 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-1 | 停用用户 | A1 | 集成 | 存量用户 | 返回 200 状态停用 | YES |
| TC-2 | 停用用户请求被拒 | A2 | 集成 | 已停用用户 | 403 | YES |

## 边界与异常覆盖

- 空值 / 极值 / 非法状态：无
- 重复调用 / 并发 / 超时：并发停用
- 历史数据（迁移后的存量）：默认 0
- DB / 外部依赖 / 网络 / 权限 / 配置异常：DB
- 重试耗尽后的最终行为：500

## 环境与数据

本地库。

## 回归范围

| 模块 | 回归原因 | 回归方式 |
| --- | --- | --- |
| user | 状态变更 | 全量 |

## 通过标准

- [ ] 全绿
EOF

DG="$KIT/doc-gate"

# 生成合规的 issues.json（stage + doc_hashes 绑定——gate 强校验）
cat > "$T/mkissues.py" <<'PYEOF'
import hashlib, json, sys, pathlib
d, stage = sys.argv[1], sys.argv[2]
issues = [json.loads(a) for a in sys.argv[3:]]
docs = {"需求分析": ["需求分析.md"],
        "概要设计": ["概要设计.md"], "UI设计": ["UI设计.md"],
        "详细设计": ["详细设计.md", "API设计.md", "数据库设计.md"],
        "计划": ["任务清单.md", "测试方案.md"]}[stage]
hashes = {}
for f in docs:
    p = pathlib.Path(d) / f
    hashes[f] = hashlib.sha256(p.read_bytes()).hexdigest()
pathlib.Path(d, "reviews").mkdir(parents=True, exist_ok=True)
json.dump({"reviewer": "independent-subagent", "stage": stage,
           "doc_hashes": hashes, "issues": issues},
          open(pathlib.Path(d) / "reviews" / f"{stage}.issues.json", "w"),
          ensure_ascii=False)
PYEOF
write_issues() { python3 "$T/mkissues.py" "$@"; }

# ════════════════ S-1 需求登记脚手架（init：两处目录+模板+索引行，幂等）════════════════
run_rc python3 -m installer "$REPO" >/dev/null 2>&1   # 安装 _模板 与索引种子
"$KIT/install.sh" "$REPO" >/dev/null 2>&1
run_rc python3 "$DG" init 020-登记测试 --repo "$REPO"
assert_eq "init 退出码 0" "$rc" "0"
[ -f "$REPO/docs/01-需求/020-登记测试/原始需求.md" ] \
    && ok "init 建 01-需求 侧原始需求" || bad "init 缺原始需求"
N20=$(ls "$REPO/docs/03-SDD/020-登记测试" 2>/dev/null | wc -l)
[ "$N20" = "11" ] && ok "init 建 03-SDD 侧 11 件设计模板" || bad "init 设计模板 $N20 件"
grep -q "^| 020-登记测试" "$REPO/docs/01-需求/README.md" \
    && ok "init 登记索引行" || bad "索引行未登记"
run_rc python3 "$DG" init 020-登记测试 --repo "$REPO"
assert_eq "init 幂等（退出码 0）" "$rc" "0"
N20B=$(grep -c "^| 020-登记测试" "$REPO/docs/01-需求/README.md")
[ "$N20B" = "1" ] && ok "init 幂等（索引行不重复）" || bad "索引行重复 $N20B 行"
run_rc python3 "$DG" init "bad/name" --repo "$REPO"
assert_eq "非法需求名被拒" "$rc" "3"

# ════════════════ S0 需求冻结凭证（P1：文本"已冻结"不构成凭证）════════════════
run_rc python3 "$DG" check "$D" --stage 需求分析
assert_eq "check 需求分析 通过" "$rc" "0"
assert_exists "hard-checks.json 落盘" "$D/reviews/hard-checks.json"

run_rc python3 "$DG" freeze "$D" --by 维护者甲
assert_eq "freeze 落哈希凭证（人工签写冻结记录后）" "$rc" "0"
assert_exists "需求分析.gate.json 凭证" "$D/reviews/需求分析.gate.json"
assert_grep "凭证绑定 approved_by" '"approved_by": "维护者甲"' "$D/reviews/需求分析.gate.json"

# 未签写冻结记录 → freeze 拒绝（冻结权在人工）
D0="$REPO/docs/03-SDD/009-未签写"
cp -r "$D" "$D0"; rm -rf "$D0/reviews"
sed -i 's/^- 冻结状态：已冻结$/- 冻结状态：未冻结/' "$D0/需求分析.md"
run_rc python3 "$DG" freeze "$D0" --by 维护者甲
assert_eq "冻结记录未签写 → freeze 拒绝" "$rc" "3"

# 冻结后篡改需求 → 哈希失配，下游上游检查自动失效（P1 核心场景）
D_T="$REPO/docs/03-SDD/017-需求篡改"
cp -r "$D" "$D_T"
echo "被偷偷改掉的需求内容" >> "$D_T/需求分析.md"
OUT="$(python3 "$DG" status "$D_T")"
echo "$OUT" | grep -q "需求分析.*DRAFT.*哈希失配" && ok "冻结后篡改需求 → status 检出哈希失配" || bad "需求篡改未检出: $OUT"
run_rc python3 "$DG" check "$D_T" --stage 概要设计
assert_eq "需求篡改后下游上游检查失败" "$rc" "1"

# ════════════════ S1 check：各阶段硬校验 ════════════════

run_rc python3 "$DG" check "$D" --stage 概要设计
assert_eq "check 概要设计 通过（需求已冻结+追踪 A→F）" "$rc" "0"

# ════════════════ S2 gate：冻结与上游链 ════════════════
run_rc python3 "$DG" gate "$D" --stage 概要设计
assert_eq "gate 概要设计 PASS（review=optional 无 issues 可判）" "$rc" "0"
assert_grep "gate.json 状态 PASS" '"status": "PASS"' "$D/reviews/概要设计.gate.json"
assert_grep "gate.json 记录文档哈希" '"doc_hashes"' "$D/reviews/概要设计.gate.json"

# 业务流程设计：人工签核阶段（评审记录 + freeze；未签核时详细设计上游检查失败）
D_BP="$REPO/docs/03-SDD/018-业务流程未评审"
cp -r "$D" "$D_BP"; rm -rf "$D_BP/reviews"
run_rc python3 "$DG" freeze "$D_BP" --by 维护者甲
run_rc python3 "$DG" gate "$D_BP" --stage 概要设计
assert_eq "D_BP 概要先冻结" "$rc" "0"
run_rc python3 "$DG" check "$D_BP" --stage 业务流程设计
assert_eq "业务流程设计 check 通过（结构）" "$rc" "0"
sed -i 's/^- 评审结论：通过$/- 评审结论：未评审/' "$D_BP/业务流程设计.md"
run_rc python3 "$DG" freeze "$D_BP" --stage 业务流程设计 --by 评审人甲
assert_eq "评审未签写 → freeze 拒绝" "$rc" "3"
sed -i 's/^- 评审结论：未评审$/- 评审结论：通过/' "$D_BP/业务流程设计.md"
run_rc python3 "$DG" check "$D_BP" --stage 详细设计
assert_eq "业务流程未冻结 → 详细设计上游检查失败" "$rc" "1"
run_rc python3 "$DG" freeze "$D_BP" --stage 业务流程设计 --by 评审人甲
assert_eq "评审签写后 freeze 落凭证" "$rc" "0"
assert_exists "业务流程设计.gate.json" "$D_BP/reviews/业务流程设计.gate.json"
run_rc python3 "$DG" check "$D_BP" --stage 详细设计
assert_eq "业务流程冻结后详细设计上游检查通过" "$rc" "0"

# 主链：业务流程设计 freeze 后进入详细设计
run_rc python3 "$DG" freeze "$D" --stage 业务流程设计 --by 评审人甲
assert_eq "主链业务流程设计 freeze" "$rc" "0"
run_rc python3 "$DG" check "$D" --stage 详细设计
assert_eq "check 详细设计 通过（业务流程设计已签核冻结）" "$rc" "0"

run_rc python3 "$DG" gate "$D" --stage 详细设计
assert_eq "gate 详细设计 缺 issues.json 被拒" "$rc" "3"

write_issues "$D" 详细设计
run_rc python3 "$DG" gate "$D" --stage 详细设计
assert_eq "gate 详细设计 干净 issues → PASS" "$rc" "0"

# P1 负向：必审阶段拒绝 --no-review
run_rc python3 "$DG" gate "$D" --stage 详细设计 --no-review
assert_eq "必审阶段 --no-review 被拒" "$rc" "3"

# P1 负向：评审后改动文档 → 评审过期（哈希失配）
echo "x" >> "$D/详细设计.md"
run_rc python3 "$DG" gate "$D" --stage 详细设计
assert_eq "旧评审不能冻结改后文档（哈希失配拒绝）" "$rc" "3"
sed -i '$ d' "$D/详细设计.md"  # 还原后评审恢复有效
run_rc python3 "$DG" gate "$D" --stage 详细设计
assert_eq "还原后同一评审仍有效（PASS）" "$rc" "0"

# P1 负向：非法 severity / 缺字段 / stage 不符
run_rc python3 "$DG" gate "$D" --stage 计划   # 先让详细设计冻结（上面已 PASS）
assert_eq "gate 计划 PASS" "$rc" "0"
D_BAD="$REPO/docs/03-SDD/016-非法评审"
cp -r "$D" "$D_BAD"; rm -rf "$D_BAD/reviews"
run_rc python3 "$DG" freeze "$D_BAD" --by 维护者甲
run_rc python3 "$DG" gate "$D_BAD" --stage 概要设计
run_rc python3 "$DG" freeze "$D_BAD" --stage 业务流程设计 --by 评审人甲
mkdir -p "$D_BAD/reviews"
run_rc python3 "$DG" gate "$D_BAD" --stage 概要设计
assert_eq "D_BAD 概要先冻结" "$rc" "0"
printf '{"reviewer": "x", "stage": "详细设计", "doc_hashes": {"详细设计.md": "deadbeef"}, "issues": [{"id": "R1", "severity": "critical", "location": "x", "problem": "p", "required_change": "r", "upstream": false}]}' > "$D_BAD/reviews/详细设计.issues.json"
run_rc python3 "$DG" gate "$D_BAD" --stage 详细设计
assert_eq "非法 severity 被拒" "$rc" "3"
printf '{"reviewer": "x", "stage": "概要设计", "doc_hashes": {"详细设计.md": "deadbeef"}, "issues": []}' > "$D_BAD/reviews/详细设计.issues.json"
run_rc python3 "$DG" gate "$D_BAD" --stage 详细设计
assert_eq "stage 不符被拒" "$rc" "3"
printf '{"reviewer": "x", "stage": "详细设计", "doc_hashes": {"详细设计.md": "deadbeef"}, "issues": [{"id": "R1", "severity": "major", "problem": "p", "required_change": "r", "upstream": false}]}' > "$D_BAD/reviews/详细设计.issues.json"
run_rc python3 "$DG" gate "$D_BAD" --stage 详细设计
assert_eq "缺 location 字段被拒" "$rc" "3"

run_rc python3 "$DG" check "$D" --stage 计划
assert_eq "check 计划 通过（追踪 A→TC、T→D）" "$rc" "0"
run_rc python3 "$DG" gate "$D" --stage 计划
assert_eq "gate 计划 PASS" "$rc" "0"

# ════════════════ S3 status：状态派生与哈希失配 ════════════════
OUT="$(python3 "$DG" status "$D")"
echo "$OUT" | grep -q "概要设计.*FROZEN" && ok "status 显示概要设计 FROZEN" || bad "status 概要设计: $OUT"
echo "$OUT" | grep -q "需求.*FROZEN" && ok "status 显示需求 FROZEN" || bad "status 需求: $OUT"

echo "x" >> "$D/详细设计.md"
OUT="$(python3 "$DG" status "$D")"
echo "$OUT" | grep -q "详细设计.*DRAFT.*哈希失配" && ok "冻结后改动 → status 检出哈希失配" || bad "哈希失配未检出: $OUT"
sed -i '$ d' "$D/详细设计.md"  # 还原

# ════════════════ S4 NEEDS_REVISION 与 BLOCKED ════════════════
D2="$REPO/docs/03-SDD/011-评审失败"
cp -r "$D" "$D2"
rm -rf "$D2/reviews"
run_rc python3 "$DG" freeze "$D2" --by 维护者甲
run_rc python3 "$DG" gate "$D2" --stage 概要设计
run_rc python3 "$DG" freeze "$D2" --stage 业务流程设计 --by 评审人甲
mkdir -p "$D2/reviews"
run_rc python3 "$DG" gate "$D2" --stage 概要设计
assert_eq "D2 概要设计先冻结" "$rc" "0"
write_issues "$D2" 详细设计 '{"id": "REV-001", "severity": "blocker", "category": "requirement_gap", "location": "D2", "problem": "未定义迁移", "evidence": "无默认值", "required_change": "补迁移规则", "upstream": false}'

run_rc python3 "$DG" gate "$D2" --stage 详细设计
assert_eq "blocker 超阈值 → NEEDS_REVISION" "$rc" "1"
assert_grep "gate.json 指向修复目标" '"REV-001"' "$D2/reviews/详细设计.gate.json"

# P2 修复验证：历史 PASS 不计入连续失败（从尾部数，PASS 清零）
D2B="$REPO/docs/03-SDD/012B-迭代计数"
cp -r "$D" "$D2B"; rm -rf "$D2B/reviews"
run_rc python3 "$DG" freeze "$D2B" --by 维护者甲
run_rc python3 "$DG" gate "$D2B" --stage 概要设计
run_rc python3 "$DG" freeze "$D2B" --stage 业务流程设计 --by 评审人甲; mkdir -p "$D2B/reviews"
for i in 1 2 3; do printf '{"time":"t%d","stage":"详细设计","status":"NEEDS_REVISION","iteration":%d}\n' "$i" "$i" >> "$D2B/reviews/详细设计.gate.log"; done
printf '{"time":"tp","stage":"详细设计","status":"PASS","iteration":4}\n' >> "$D2B/reviews/详细设计.gate.log"
printf '{"time":"tq","stage":"详细设计","status":"NEEDS_REVISION","iteration":5}\n' >> "$D2B/reviews/详细设计.gate.log"
write_issues "$D2B" 详细设计 '{"id": "REV-009", "severity": "blocker", "category": "risk", "location": "D1", "problem": "p", "evidence": "e", "required_change": "r", "upstream": false}'
run_rc python3 "$DG" gate "$D2B" --stage 详细设计
assert_eq "PASS 清零后一次失败 = NEEDS_REVISION（非 BLOCKED）" "$rc" "1"

# 第七轮审计 P2：第 max_iterations(3) 次失败本身即 BLOCKED（此前晚一轮）
printf '{"time":"tz","stage":"详细设计","status":"NEEDS_REVISION","iteration":6}\n' >> "$D2B/reviews/详细设计.gate.log"
run_rc python3 "$DG" gate "$D2B" --stage 详细设计
assert_eq "第 3 次失败即 BLOCKED（连续 2 失败 + 本次）" "$rc" "2"

for i in 1 2 3; do printf '{"time":"t%d","stage":"详细设计","status":"NEEDS_REVISION","iteration":%d}\n' "$i" "$i" >> "$D2/reviews/详细设计.gate.log"; done
run_rc python3 "$DG" gate "$D2" --stage 详细设计
assert_eq "连续 3 轮不达标 → BLOCKED" "$rc" "2"

# P1：upstream 打回未决 → 不冻结（BLOCKED——下游建立在被质疑的上游上）
D6="$REPO/docs/03-SDD/015-上游打回"
cp -r "$D" "$D6"
rm -rf "$D6/reviews"
run_rc python3 "$DG" freeze "$D6" --by 维护者甲
run_rc python3 "$DG" gate "$D6" --stage 概要设计
run_rc python3 "$DG" freeze "$D6" --stage 业务流程设计 --by 评审人甲
mkdir -p "$D6/reviews"
run_rc python3 "$DG" gate "$D6" --stage 概要设计
assert_eq "D6 概要设计先冻结" "$rc" "0"
write_issues "$D6" 详细设计 '{"id": "REV-002", "severity": "blocker", "category": "upstream", "location": "需求.md", "problem": "场景缺失", "evidence": "e", "required_change": "维护者裁决", "upstream": true}'
run_rc python3 "$DG" gate "$D6" --stage 详细设计
assert_eq "upstream 打回未决 → BLOCKED（不冻结）" "$rc" "2"
assert_grep "gate.json 记录 upstream_pending" '"upstream_pending": 1' "$D6/reviews/详细设计.gate.json"
# 上游裁决后（同评审重跑无 upstream issue）→ PASS 解冻
write_issues "$D6" 详细设计
run_rc python3 "$DG" gate "$D6" --stage 详细设计
assert_eq "上游解决后重评 → PASS（BLOCKED 可由 PASS 解除）" "$rc" "0"

# ════════════════ S5 破损夹具：结构/占位/悬空/薄输入/上游未冻结 ════════════════
D3="$REPO/docs/03-SDD/012-破损"
mkdir -p "$D3"
cp "$D/需求分析.md" "$D3/需求分析.md"
sed -i 's/^- 冻结状态：已冻结$/- 冻结状态：未冻结/' "$D3/需求分析.md"
cat > "$D3/概要设计.md" <<'EOF'
# 概要设计：破损

## 设计目标

未冻结就往下走。

## 现状分析

〈待补充〉

## 总体方案

无。

## 功能点设计

### F1：孤岛功能点

- 覆盖验收项：A9

## 方案取舍

无。

## 数据与接口影响

无。

## 风险

无。
EOF
OUT="$(python3 "$DG" check "$D3" --stage 概要设计 2>&1)" && rc=0 || rc=$?
assert_eq "破损概要设计 check 失败" "$rc" "1"
case "$OUT" in *"上游已冻结"*) ok "上游未冻结被检出";; *) bad "上游检查缺失: $OUT";; esac
case "$OUT" in *"占位符"*) ok "占位符残留被检出";; *) bad "占位符检查缺失: $OUT";; esac
case "$OUT" in *"悬空"*) ok "功能点悬空引用被检出";; *) bad "悬空检查缺失: $OUT";; esac

# 薄输入：场景/验收为占位
D4="$REPO/docs/03-SDD/013-薄输入"
mkdir -p "$D4"
cat > "$D4/需求分析.md" <<'EOF'
# 需求：薄输入

## 背景

〈当前现状〉

## 目标

〈目标〉

## 非目标（边界）

无。

## 场景与验收依据

### 场景 1：〈场景名称〉

- **Given**：〈前置条件〉
- **When**：〈操作或事件〉
- **Then**：〈系统可观察产出〉

## 验收项

- [ ] A1：〈验收条件〉（场景 1）

## 非功能要求

- 性能：N/A-无

## 待确认事项

| ID | 问题 | 影响 | 阻塞 | 负责人 | 状态 |
| --- | --- | --- | --- | --- | --- |

## 冻结记录

- 冻结状态：未冻结
EOF
OUT="$(python3 "$DG" check "$D4" --stage 需求分析 2>&1)" && rc=0 || rc=$?
assert_eq "薄输入 check 失败" "$rc" "1"
case "$OUT" in *"需求场景完整"*) ok "薄输入拒绝：场景占位被检出";; *) bad "意图检查缺失: $OUT";; esac

# ════════════════ S6 policy 覆盖 ════════════════
D5="$REPO/docs/03-SDD/014-策略覆盖"
cp -r "$D" "$D5"; rm -rf "$D5/reviews"
run_rc python3 "$DG" freeze "$D5" --by 维护者甲
run_rc python3 "$DG" gate "$D5" --stage 概要设计
run_rc python3 "$DG" freeze "$D5" --stage 业务流程设计 --by 评审人甲
mkdir -p "$REPO/.repo-memory-kit"
printf '{"max_blocker": 1, "max_major": 0, "max_minor": 5, "max_iterations": 3}' \
    > "$REPO/.repo-memory-kit/doc-policy.json"
mkdir -p "$D5/reviews"
run_rc python3 "$DG" gate "$D5" --stage 概要设计
assert_eq "policy 覆盖下概要先冻结" "$rc" "0"
write_issues "$D5" 详细设计 '{"id": "REV-001", "severity": "blocker", "category": "risk", "location": "D2", "problem": "p", "evidence": "e", "required_change": "r", "upstream": false}'
run_rc python3 "$DG" gate "$D5" --stage 详细设计
assert_eq "policy 覆盖（max_blocker=1）→ 1 个 blocker 不再阻断 → PASS" "$rc" "0"
assert_grep "gate.json 记录实际 policy" '"max_blocker": 1' "$D5/reviews/详细设计.gate.json"
rm "$REPO/.repo-memory-kit/doc-policy.json"

# ════════════════ S7 governance-eval 布线 ════════════════
printf '{"version": 1, "profile": "strict", "default": {"require": ["inline_review"]}, "rules": [{"id": "SEC-001", "require": ["receipt", "independent_review"], "match": {"paths": ["src/**/security/**"]}}]}' \
    > "$REPO/.repo-memory-kit/governance.json"
cat > "$T/sec.diff" <<'EOF'
diff --git a/src/main/security/Auth.java b/src/main/security/Auth.java
index 1111111..2222222 100644
--- a/src/main/security/Auth.java
+++ b/src/main/security/Auth.java
@@ -1,3 +1,5 @@
 public class Auth {
+    public boolean skipCheck = true;
+    // bypass
 }
EOF
OUT="$(python3 "$KIT/governance-eval" "$REPO" --diff "$T/sec.diff")"
case "$OUT" in *"SEC-001"*) ok "governance-eval 命中 security 规则";; *) bad "规则未命中: $OUT";; esac
case "$OUT" in *"independent_review"*) ok "required_actions 含独立审查";; *) bad "动作缺失: $OUT";; esac

python3 "$KIT/governance-eval" "$REPO" --diff "$T/sec.diff" --json > "$T/gov.json"
if python3 -c "import json; d=json.load(open('$T/gov.json')); assert d['matched_rules'][0]['rule_id']=='SEC-001' and d['needs_independent_review']"; then
    ok "governance-eval --json 机器可读"
else
    bad "governance-eval --json 结构异常"
fi

printf 'diff --git a/docs/x.md b/docs/x.md\nindex 1..2 100644\n--- a/docs/x.md\n+++ b/docs/x.md\n@@ -1 +1,2 @@\n doc\n+line\n' > "$T/doc.diff"
OUT="$(python3 "$KIT/governance-eval" "$REPO" --diff "$T/doc.diff")"
case "$OUT" in *"无规则命中"*) ok "无关 diff 无规则命中";; *) bad "误命中: $OUT";; esac

# P1 布线验证：governance.json 由安装器写入（步骤 11.5，**只存规则**）
rm -f "$REPO/.repo-memory-kit/governance.json"   # 清掉预置，验证安装器真写入
run_rc "$KIT/install.sh" "$REPO" >/dev/null 2>&1
if [ -f "$REPO/.repo-memory-kit/governance.json" ]; then
    ok "安装器写入 governance.json（规则载体）"
    if grep -q '"profile"' "$REPO/.repo-memory-kit/governance.json" 2>/dev/null; then
        bad "json 仍存 profile（双事实源回归）"
    else
        ok "json 只存团队规则（profile 唯一权威在 marker）"
    fi
    grep -q '"SEC-001"' "$REPO/.repo-memory-kit/governance.json" \
        && ok "缺省规则 SEC-001/DB-001 在位" || bad "缺省规则缺失"
else
    bad "安装后无 governance.json——引擎无规则可用"
fi

# P1-5 单一事实源：strict → lightweight 切换即时生效（marker 每次安装重写；
# 此前 json 只在首建时写 profile，切换后引擎仍读旧 strict）
printf 'lightweight\n' > "$REPO/.repo-memory-kit/governance"
OUT="$(python3 "$KIT/governance-eval" "$REPO" --diff "$T/sec.diff" --json)"
if echo "$OUT" | grep -q '"profile": "lightweight"'; then
    ok "marker 切换 lightweight → 引擎即时生效（单一事实源）"
else
    bad "profile 切换失效: $OUT"
fi
echo "$OUT" | grep -q '"is_inline_only": true' \
    && ok "lightweight 默认动作 = inline_review" \
    || { echo "$OUT" | grep -q '"inline_review"' \
         && ok "lightweight 默认动作 = inline_review"          || bad "lightweight 默认动作异常: $OUT"; }

# marker 回退：删掉 json 只留 marker → 引擎按 marker 判（json 非必需）
rm -f "$REPO/.repo-memory-kit/governance.json"
OUT="$(python3 "$KIT/governance-eval" "$REPO" --diff "$T/sec.diff" --json)"
if echo "$OUT" | grep -q '"profile": "lightweight"'; then
    ok "json 缺失时按 marker 判（规则可选、profile 必在）"
else
    bad "marker 回退失效: $OUT"
fi

# 非法正则 → fail-closed 阻断（exit 3）
mkdir -p "$REPO/.repo-memory-kit"
printf '{"version":1,"profile":"strict","rules":[{"id":"BAD-1","require":["receipt"],"match":{"added_lines_regex":["([unclosed"]}}],"default":{"require":[]}}' \
    > "$REPO/.repo-memory-kit/governance.json"
run_rc python3 "$KIT/governance-eval" "$REPO" --diff "$T/sec.diff"
assert_eq "非法正则 fail-closed（阻断非静默跳过）" "$rc" "3"

# ════════════════ 汇总 ════════════════
echo
echo "doc-gate 测试: $pass 通过, $fail 失败"
[ "$fail" -eq 0 ] || exit 1
