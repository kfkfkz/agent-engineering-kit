#!/bin/sh
# installer/ 受管资源生命周期测试（设计文档 v12·冻结版 §20.1 冻结版测试清单）。
# 覆盖：根目录路径全链路 / 首次安装 / 二次幂等 / 故障注入全套（写序不变量
# 三窗口、HMAC、恢复分类、外部步骤补偿、secure_* 拒绝面、伪造 Manifest）。
set -u
SRC="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
T_HOME="$(mktemp -d)"          # HOME 隔离：tx key 不落真实用户配置目录
trap 'rm -rf "$T" "$T_HOME"' EXIT
# zvec 位于真实用户 site-packages——先于 HOME 隔离捕获，之后由 PYTHONPATH 补回
USER_SITE="$(python3 -c 'import site; print(site.getusersitepackages())' 2>/dev/null || true)"
HOME="$T_HOME"
export HOME
cd "$SRC"

pass=0; fail=0
ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }
check() { # $1=描述 $2=期望(0/1) $3=实际
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 $2 实际 $3）"; fi
}

# ══════════ 一、secure_* 拒绝面（§20.1 故障注入·拒绝面） ══════════
python3 - <<'PY' || echo "secure_* 拒绝面存在 FAIL"
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from installer.registry import (SecurityError, secure_open, secure_mkdir,
                                 validate_relative_path, validate_tx_id)

t = Path(tempfile.mkdtemp())
cases_pass, cases_fail = [], []
for bad_rel in ("/etc/passwd", "..", "a/../b", ".", ""):
    try:
        validate_relative_path(bad_rel)
        cases_fail.append(repr(bad_rel))
    except SecurityError:
        cases_pass.append(repr(bad_rel))
# 最后组件为 .. 的逃逸：secure_open 先校验完整路径
for bad_rel in ("docs/..", "trailing/.."):
    try:
        secure_open(t, bad_rel, os.O_RDONLY)
        cases_fail.append(repr(bad_rel))
    except SecurityError:
        cases_pass.append(repr(bad_rel))
# 中间目录符号链接
os.mkdir(t / "realdir")
os.symlink("realdir", t / "linkdir")
try:
    secure_mkdir(t, "linkdir/sub")
    cases_fail.append("symlink middle dir")
except SecurityError:
    cases_pass.append("symlink middle dir")
# UUID 校验
for bad_id in ("not-a-uuid", "ABCDEF00-0000-4000-8000-000000000000".upper(),
               "00000000-0000-3000-8000-000000000000"):
    try:
        validate_tx_id(bad_id)
        cases_fail.append(f"uuid {bad_id}")
    except SecurityError:
        cases_pass.append(f"uuid {bad_id}")
if cases_fail:
    print("secure_* 拒绝面 FAIL: 未拒绝", cases_fail)
    sys.exit(1)
print(f"secure_* 拒绝面: {len(cases_pass)} 项全部拒绝")
import shutil; shutil.rmtree(t)
PY
[ $? = 0 ] && ok "secure_* 拒绝面（绝对路径/../最后组件 ../符号链接目录/./非 v4 UUID）" || bad "secure_* 拒绝面"

# ══════════ 二、全新空目录首次安装（secure_mkdir 全接入点） ══════════
P="$T/empty"
mkdir -p "$P"
python3 -m installer "$P" > "$T/install1.log" 2>&1
check "空目录首次安装退出码 0" 0 $?

[ -f "$P/docs/memory/RULES.md" ] && ok "RULES.md 已装" || bad "RULES.md 缺失"
[ -f "$P/docs/memory/README.md" ] && ok "seed README 已创建" || bad "seed README 缺失"
[ -f "$P/.claude/skills/tdd/SKILL.md" ] && ok "staged 父目录（.claude/skills/tdd）已创建" || bad "技能目录缺失"
[ -f "$P/.agents/skills/tdd/SKILL.md" ] && ok ".agents 技能已装" || bad ".agents 技能缺失"
[ -f "$P/.repo-memory-kit/bin/agent-engineering-mcp" ] && ok "bin 工具已装" || bad "bin 工具缺失"
[ -x "$P/.repo-memory-kit/bin/memory-build" ] && ok "bin 工具可执行（0755）" || bad "bin 工具不可执行"
[ -f "$P/.repo-memory-kit/manifest.json" ] && ok "Manifest v2 已写" || bad "Manifest 缺失"
[ -f "$P/.repo-memory-kit/install.lock" ] && ok "install.lock 已创建（首次安装）" || bad "install.lock 缺失"
[ -f "$P/.cbmignore" ] && grep -q "# repo-memory-kit:end" "$P/.cbmignore" \
  && ok ".cbmignore 托管区块（含 end 标记）" || bad ".cbmignore 区块异常"
[ -f "$P/.mcp.json" ] && ok ".mcp.json 已创建" || bad ".mcp.json 缺失"
[ -f "$P/.codex/config.toml" ] && ok ".codex/config.toml 已创建" || bad ".codex 缺失"
[ -f "$P/.claude/settings.json" ] && ok "settings.json 已创建" || bad "settings.json 缺失"
# create_container=False：不擅建用户容器
[ ! -f "$P/CLAUDE.md" ] && [ ! -f "$P/AGENTS.md" ] && [ ! -f "$P/.gitignore" ] \
  && ok "CLAUDE.md/AGENTS.md/.gitignore 不存在时不擅建" || bad "擅建了用户容器文件"
# 事务目录清理（done 后 tx/<id> 应删除）
n_tx=$(ls "$P/.repo-memory-kit/tx" 2>/dev/null | wc -l)
check "done 后 tx 目录已清空" 0 "$n_tx"
# staged 残留
n_stage=$(find "$P" -name "*.kit-stage-*" 2>/dev/null | wc -l)
check "无 staged 残留" 0 "$n_stage"

# ══════════ 三、二次安装幂等（§20.1） ══════════
H_BEFORE=$(find "$P" -type f -not -path "*/.repo-memory-kit/*" -exec sha256sum {} \; | sort | sha256sum)
python3 -m installer "$P" > "$T/install2.log" 2>&1
check "二次安装退出码 0" 0 $?
H_AFTER=$(find "$P" -type f -not -path "*/.repo-memory-kit/*" -exec sha256sum {} \; | sort | sha256sum)
[ "$H_BEFORE" = "$H_AFTER" ] && ok "二次安装幂等（受管内容字节不变）" || bad "二次安装改变了内容"

# doctor HEALTHY
python3 -m installer --doctor "$P" > "$T/doctor1.log" 2>&1
check "doctor 退出码 0" 0 $?
grep -q "总体状态: HEALTHY" "$T/doctor1.log" && ok "doctor 总体 HEALTHY" || bad "doctor 非 HEALTHY: $(grep 总体 "$T/doctor1.log")"

# ══════════ 四、根目录文件全链路（§20.1 P1 回归：stage→commit→doctor→uninstall） ══════════
R="$T/rootfiles"
mkdir -p "$R"
printf '# my project\n\nuser line A\n' > "$R/CLAUDE.md"
printf '# agents doc\n\nuser line B\n' > "$R/AGENTS.md"
printf 'node_modules/\n*.pyc\n' > "$R/.gitignore"
python3 -m installer "$R" > "$T/root1.log" 2>&1
check "含根目录文件的安装退出码 0" 0 $?
grep -q "user line A" "$R/CLAUDE.md" && grep -q "repo-memory-kit:start" "$R/CLAUDE.md" \
  && ok "CLAUDE.md：用户内容保留 + 托管区块追加" || bad "CLAUDE.md 内容异常"
grep -q "user line B" "$R/AGENTS.md" && grep -q "repo-memory-kit:end" "$R/AGENTS.md" \
  && ok "AGENTS.md：用户内容保留 + 托管区块追加" || bad "AGENTS.md 内容异常"
grep -q "node_modules/" "$R/.gitignore" && grep -q "agent-engineering-kit:end" "$R/.gitignore" \
  && ok ".gitignore：用户行保留 + 语义索引区块（带 end）" || bad ".gitignore 内容异常"
grep -q '"command": "'"$R"'/\.repo-memory-kit/bin/agent-engineering-mcp"' "$R/.mcp.json" \
  && ok ".mcp.json 片段含目标绝对路径" || bad ".mcp.json 片段异常"
python3 -m installer --doctor "$R" > "$T/rootdoc.log" 2>&1
check "根目录文件场景 doctor 退出码 0" 0 $?
python3 -m installer --uninstall "$R" > "$T/rootun.log" 2>&1
check "根目录文件场景卸载退出码 0" 0 $?
grep -q "user line A" "$R/CLAUDE.md" && ! grep -q "repo-memory-kit:start" "$R/CLAUDE.md" \
  && ok "卸载后 CLAUDE.md：用户内容保留、区块移除、容器不删" || bad "卸载后 CLAUDE.md 异常"
grep -q "node_modules/" "$R/.gitignore" && ! grep -q "agent-engineering-kit: 语义索引" "$R/.gitignore" \
  && ok "卸载后 .gitignore：用户行保留、区块移除" || bad "卸载后 .gitignore 异常"
[ -f "$R/.mcp.json" ] && [ "$(cat "$R/.mcp.json")" = "{}" ] \
  && ok "卸载后 .mcp.json 容器保留（空容器 {}）" || bad ".mcp.json 容器被删或未清空"
[ ! -f "$R/docs/memory/RULES.md" ] && ok "卸载后 owned_file 已删（RULES.md）" || bad "RULES.md 未删"

# ══════════ 五、漂移与冲突保护 / 伪造 Manifest（§20.1） ══════════
D="$T/drift"
mkdir -p "$D"
python3 -m installer "$D" > /dev/null 2>&1
# 漂移 = kit 血统但与记录不符：换成 kit 历史版本（legacy 集合可验血统）
OLD_REV=$(git -C "$SRC" log --format=%H -- templates/memory-RULES.md | sed -n '3p')
[ -n "$OLD_REV" ] || OLD_REV=$(git -C "$SRC" log --format=%H -- templates/memory-RULES.md | tail -1)
git -C "$SRC" show "$OLD_REV:templates/memory-RULES.md" > "$D/docs/memory/RULES.md"
python3 -m installer "$D" > "$T/drift1.log" 2>&1
check "漂移时安装退出码 0（跳过报告）" 0 $?
grep -q "\[drifted\]" "$T/drift1.log" && ok "漂移被报告" || bad "漂移未报告"
git -C "$SRC" show "$OLD_REV:templates/memory-RULES.md" | diff -q - "$D/docs/memory/RULES.md" >/dev/null \
  && ok "漂移文件未被覆盖（默认跳过）" || bad "漂移文件被覆盖"
python3 -m installer --doctor "$D" > "$T/drift-doc.log" 2>&1
grep -q "DRIFTED" "$T/drift-doc.log" && ok "doctor 报 DRIFTED" || bad "doctor 未报 DRIFTED"
python3 -m installer --repair "$D" > "$T/repair.log" 2>&1
check "--repair 退出码 0" 0 $?
cmp -s "$SRC/templates/memory-RULES.md" "$D/docs/memory/RULES.md" \
  && ok "--repair 恢复 kit 当前版本" || bad "--repair 未恢复"

# 用户自有内容（无 kit 血统）→ conflict，不覆盖不删除（含伪造 Manifest 场景）
F="$T/forged"
mkdir -p "$F"
python3 -m installer "$F" > /dev/null 2>&1
printf 'totally user content\n' > "$F/docs/memory/RULES.md"
python3 - "$F" <<'PY'
import hashlib, json, sys
from pathlib import Path
p = Path(sys.argv[1]) / ".repo-memory-kit" / "manifest.json"
data = json.loads(p.read_text())
user_hash = hashlib.sha256(b"totally user content\n").hexdigest()
for e in data["entries"]:
    if e["spec_id"] == "memory-rules":
        e["installed_hash"] = user_hash     # 伪造：让记录声称管理用户内容
p.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
PY
python3 -m installer "$F" > "$T/forged1.log" 2>&1
grep -q "\[conflict\]" "$T/forged1.log" && ok "伪造清单 → conflict 报告" || bad "伪造清单未报 conflict"
grep -q "totally user content" "$F/docs/memory/RULES.md" \
  && ok "伪造清单场景不覆盖用户内容" || bad "用户内容被覆盖"
python3 -m installer --uninstall "$F" > "$T/forged-un.log" 2>&1
grep -q "totally user content" "$F/docs/memory/RULES.md" \
  && ok "伪造清单场景不删除用户内容（残留保护）" || bad "用户内容被删除"

# 旧版 .gitignore 行组（有 start 无 end，v6 前）→ 采纳升级为带 end 标记区块
G="$T/gitignore-legacy"
mkdir -p "$G"
printf 'node_modules/\n\n# agent-engineering-kit: 语义索引派生数据(可随时重建)\n.repo-memory-kit/zvec/\n' > "$G/.gitignore"
python3 -m installer "$G" > "$T/gitignore1.log" 2>&1
check "旧 .gitignore 行组升级退出码 0" 0 $?
grep -q "agent-engineering-kit:end" "$G/.gitignore" \
  && ok "旧 .gitignore 行组升级为带 end 标记区块" || bad ".gitignore 未升级"
grep -q "node_modules/" "$G/.gitignore" \
  && ok ".gitignore 用户行保留" || bad ".gitignore 用户行丢失"
n_ign=$(grep -c "语义索引派生数据" "$G/.gitignore")
check "升级后区块数=1（不重复追加）" 1 "$n_ign"
python3 -m installer --uninstall "$G" > "$T/gitignore-un.log" 2>&1
grep -q "node_modules/" "$G/.gitignore" && ! grep -q "语义索引派生数据" "$G/.gitignore" \
  && ok "卸载后旧形态区块移除、用户行保留" || bad ".gitignore 卸载异常"

# 符号链接占位（preflight 拒绝面）
L="$T/symlink"
mkdir -p "$L/real" "$L/docs"
ln -s "real" "$L/docs/memory"
python3 -m installer "$L" > "$T/symlink1.log" 2>&1
grep -q "符号链接" "$T/symlink1.log" && ok "受管路径符号链接被拒绝" || bad "符号链接路径未拒绝"

# 目录路径被普通文件占位 → plan 阶段干净失败（事务 failed，无残留副作用）
FP="$T/fileplaceholder"
mkdir -p "$FP"
printf 'not a dir\n' > "$FP/.claude"
python3 -m installer "$FP" > "$T/fp.log" 2>&1
[ $? != 0 ] && grep -q "plan 阶段\|占位" "$T/fp.log" \
  && ok "目录被文件占位 → plan 阶段干净失败" || bad "文件占位处理异常: $(tail -3 "$T/fp.log")"
grep -q '"status": "failed"' "$FP/.repo-memory-kit/tx"/*/record.json 2>/dev/null \
  && ok "占位失败事务记录为 failed（无 staged 副作用）" || bad "占位失败事务状态异常"

# ══════════ 六、故障注入（§20.1 冻结版清单） ══════════

# 6.1 写序不变量 1/2：staging 中途崩溃 → 清理；committing 落盘前崩溃 → 无已提交项
X="$T/crash-staging"
mkdir -p "$X"
python3 - <<'PY'
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm
from installer.install import plan_groups
from installer.registry import resolve_spec

t = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "install")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}/backups")
tx.status = "planning"; tx.write(t)
plans = plan_groups(t, [resolve_spec("memory-rules"), resolve_spec("pitfall-template"),
                       resolve_spec("decision-template")])
tx.plan = [gp.step for gp in plans]
tx.write(t)                       # ★ 完整 plan 先行落盘（不变量 1）
tx.status = "staging"; tx.write(t)
txm.stage_resource(t, plans[0].step.spec_id, tx.tx_id, plans[0].content, plans[0].mode)
# —— 崩溃（staging 中途；committing 未落盘 → 不变量的保证：无已提交项）——
os.close(lock)
r = txm.recover(t)
assert r.status == "rolled_back_to_clean", f"期望 rolled_back_to_clean，实际 {r.status}"
from installer.registry import staged_rel
staged = t / staged_rel(resolve_spec(plans[0].step.spec_id), tx.tx_id)
assert not staged.exists(), "staged 文件未被清理"
assert not (t / "docs" / "memory" / "RULES.md").exists(), "崩溃前不应有已提交项"
record = t / ".repo-memory-kit" / "tx" / tx.tx_id / "record.json"
assert record.is_file(), "恢复后 failed 记录保留（审计）"
assert '"status": "failed"' in record.read_text(), "记录状态应为 failed"
print("staging 崩溃恢复 OK")
PY
[ $? = 0 ] && ok "写序不变量：staging 崩溃 → rolled_back_to_clean（staged 清理、无已提交项）" || bad "staging 崩溃恢复"

# 6.2 committing 部分提交 → 分类回滚（NEW 回滚、OLD 未动）
X2="$T/crash-partial"
mkdir -p "$X2"
python3 - "$X2" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm
from installer.install import plan_groups
from installer.registry import resolve_spec

t = Path(sys.argv[1])
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "install")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}/backups")
tx.status = "planning"; tx.write(t)
plans = plan_groups(t, [resolve_spec("memory-rules"), resolve_spec("pitfall-template"),
                       resolve_spec("decision-template")])
tx.plan = [gp.step for gp in plans]
tx.write(t)
tx.status = "staging"; tx.write(t)
for gp in plans:
    txm.stage_resource(t, gp.step.spec_id, tx.tx_id, gp.content, gp.mode)
tx.status = "committing"; tx.write(t)      # ★ 不变量 2：committing 先于首个替换
r1 = txm.commit_one(t, plans[0].step, tx.tx_id)
assert r1.status == "committed"
# —— 崩溃（1/N 已提交）——
os.close(lock)
r = txm.recover(t)
assert r.status == "rolled_back", f"期望 rolled_back，实际 {r.status}"
assert not (t / "docs" / "memory" / "RULES.md").exists(), "NEW 步骤未被回滚"
assert not (t / "docs" / "memory" / "pitfalls" / "_TEMPLATE.md").exists(), "OLD 不应被提交"
record = t / ".repo-memory-kit" / "tx" / tx.tx_id / "record.json"
assert record.is_file() and '"status": "rolled_back"' in record.read_text(), \
    "恢复后 rolled_back 记录保留（审计）"
print("部分提交回滚 OK")
PY
[ $? = 0 ] && ok "恢复分类：部分提交 → roll-back（NEW 回滚 / OLD 未动）" || bad "部分提交回滚"

# 6.3 全部已提交（committing 崩溃于 done 前）→ roll-forward 补收尾（manifest 写入）
X3="$T/crash-rollfwd"
mkdir -p "$X3"
python3 - "$X3" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm
from installer.install import plan_groups
from installer.registry import resolve_spec

t = Path(sys.argv[1])
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "install")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}/backups")
tx.status = "planning"; tx.write(t)
plans = plan_groups(t, [resolve_spec("memory-rules"), resolve_spec("pitfall-template")])
tx.plan = [gp.step for gp in plans]
tx.write(t)
tx.status = "staging"; tx.write(t)
for gp in plans:
    txm.stage_resource(t, gp.step.spec_id, tx.tx_id, gp.content, gp.mode)
tx.status = "committing"; tx.write(t)
for gp in plans:
    assert txm.commit_one(t, gp.step, tx.tx_id).status == "committed"
# —— 崩溃（全部提交、done 未写、Manifest 未写）——
os.close(lock)
r = txm.recover(t)
assert r.status == "roll_forward_completed", f"期望 roll_forward，实际 {r.status}"
m = t / ".repo-memory-kit" / "manifest.json"
assert m.is_file(), "roll-forward 未补写 Manifest"
import json
entries = {e["spec_id"] for e in json.loads(m.read_text())["entries"]}
assert "memory-rules" in entries and "pitfall-template" in entries
assert (t / "docs" / "memory" / "RULES.md").is_file()
print("roll-forward OK")
PY
[ $? = 0 ] && ok "恢复分类：全部提交 → roll-forward（Manifest 补写）" || bad "roll-forward"

# 6.4 CONFLICT 现场分类 → needs_human（evidence 保留）
X4="$T/crash-conflict"
mkdir -p "$X4"
python3 - "$X4" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm
from installer.install import plan_groups
from installer.registry import resolve_spec

t = Path(sys.argv[1])
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "install")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}/backups")
tx.status = "planning"; tx.write(t)
plans = plan_groups(t, [resolve_spec("memory-rules")])
tx.plan = [gp.step for gp in plans]
tx.write(t)
tx.status = "staging"; tx.write(t)
for gp in plans:
    txm.stage_resource(t, gp.step.spec_id, tx.tx_id, gp.content, gp.mode)
tx.status = "committing"; tx.write(t)
# 外部修改目标 → CAS 失败现场
(t / "docs" / "memory").mkdir(parents=True, exist_ok=True)
(t / "docs" / "memory" / "RULES.md").write_text("外部内容\n")
os.close(lock)
r = txm.recover(t)
assert r.status == "needs_human", f"期望 needs_human，实际 {r.status}"
assert (t / ".repo-memory-kit" / "tx" / tx.tx_id).exists(), "needs_human 应保留 evidence"
print("CONFLICT 分类 OK")
PY
[ $? = 0 ] && ok "恢复分类：CONFLICT 现场 → needs_human（evidence 保留）" || bad "CONFLICT 分类"
# needs_human 阻断一切生命周期操作（§11.4）
python3 -m installer "$X4" > "$T/blocked.log" 2>&1
[ $? != 0 ] && grep -q "needs_human" "$T/blocked.log" \
  && ok "needs_human 事务阻断后续安装" || bad "needs_human 未阻断"
# 人工恢复入口（--recover=rollback）：CONFLICT 项不可绕过 CAS——先由用户
# 处置冲突文件（恢复到 plan 前状态：该文件 plan 时不存在 → 删除）再回滚
rm -f "$X4/docs/memory/RULES.md"
python3 -m installer --recover=rollback "$X4" > "$T/manual.log" 2>&1
check "--recover=rollback 退出码 0" 0 $?
grep -q "rolled_back" "$T/manual.log" && ok "人工回滚完成（记录转 rolled_back）" || bad "人工回滚异常: $(cat "$T/manual.log")"

# 6.5 HMAC：篡改 record.json → needs_human；密钥缺失（换用户运行）→ needs_human
X5="$T/hmac"
mkdir -p "$X5"
python3 - "$X5" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm
t = Path(sys.argv[1])
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "install")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
tx.status = "planning"; tx.write(t)
os.close(lock)
# 篡改任意字节
p = t / ".repo-memory-kit" / "tx" / tx.tx_id / "record.json"
raw = p.read_bytes()
p.write_bytes(raw.replace(b'"kind": "install"', b'"kind": "uninstall"'))
r = txm.recover(t)
assert r.status == "needs_human", f"篡改后期望 needs_human，实际 {r.status}"
print("HMAC 篡改 OK")
PY
[ $? = 0 ] && ok "HMAC：篡改 record.json 任意字节 → needs_human" || bad "HMAC 篡改检测"

X6="$T/nokey"
mkdir -p "$X6"
python3 - "$X6" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm
t = Path(sys.argv[1])
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "install")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
tx.status = "planning"; tx.write(t)
os.close(lock)
os.unlink(txm.tx_key_path(t))       # 模拟换用户/跨机器运行（密钥缺失）
r = txm.recover(t)
assert r.status == "needs_human", f"密钥缺失期望 needs_human，实际 {r.status}"
print("密钥缺失 OK")
PY
[ $? = 0 ] && ok "HMAC：密钥缺失（换用户运行）→ needs_human（不创建替身密钥）" || bad "密钥缺失处理"

# 6.6 外部步骤三窗口（§13 步骤 5.5 崩溃窗口分析）
C="$T/codexroot"
python3 - "$C" <<'PY'
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from installer import transaction as txm

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
t = root / "target"
t.mkdir(exist_ok=True)
lock = txm.acquire_install_lock(t)
tx = txm.TransactionRecord.new(t, "uninstall")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx.tx_id}")
tx.status = "planning"; tx.write(t)

from installer.transaction import ExternalStep
def external(state):
    return ExternalStep(spec_id="codex-link-tdd", skill_name="tdd",
                        codex_root=str(root), prior_state="pointing_to_target",
                        state=state)

# 窗口 a-b：意图已记、链接仍在 → 补偿判定"链接还在 → 不动"
link = root / ".agents" / "skills" / "tdd"
link.parent.mkdir(parents=True, exist_ok=True)
link.symlink_to(t / ".agents" / "skills" / "tdd")
tx.external = [external("intent")]
tx.write(t)
os.close(lock)
r = txm.recover(t)
assert link.is_symlink(), "窗口 a-b：链接仍在时不应被补偿删除"
assert r.status in ("rolled_back_to_clean", "failed"), f"意外状态 {r.status}"

# 窗口 b-c：意图已记、链接已没（unlink 后、removed 记录前）→ 补偿重建
tx2 = txm.TransactionRecord.new(t, "uninstall")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx2.tx_id}")
tx2.status = "planning"; tx2.write(t)
tx2.external = [external("intent")]
tx2.write(t)                      # intent 落盘后模拟崩溃于 unlink 与 removed 之间
os.unlink(link)
lock2 = txm.acquire_install_lock(t)
r2 = txm.recover(t)
assert r2.status in ("rolled_back_to_clean", "failed")
assert link.is_symlink(), "窗口 b-c：链接应被补偿重建"
import pathlib
assert os.readlink(link) == str(t / ".agents" / "skills" / "tdd")
os.close(lock2)

# 磁盘记录 removed 但链接已没（用户自行删除）→ 重建（磁盘现状为准）
tx3 = txm.TransactionRecord.new(t, "uninstall")
txm.secure_mkdir(t, f".repo-memory-kit/tx/{tx3.tx_id}")
tx3.status = "planning"; tx3.write(t)
tx3.external = [external("removed")]
tx3.write(t)
os.unlink(link)
lock3 = txm.acquire_install_lock(t)
r3 = txm.recover(t)
assert link.is_symlink(), "removed 记录 + 链接消失 → 仍应重建"
os.close(lock3)
print("外部步骤三窗口 OK")
PY
[ $? = 0 ] && ok "外部步骤补偿：intent/removed 三窗口（链接在→不动；链接没→重建）" || bad "外部步骤补偿"

# ══════════ 七、doctor 附加状态（锁占用 → INCOMPLETE；无清单 → INCOMPLETE） ══════════
N="$T/nomanifest"
mkdir -p "$N"
python3 -m installer --doctor "$N" > "$T/doc-inc.log" 2>&1
[ $? != 0 ] && grep -q "INCOMPLETE" "$T/doc-inc.log" \
  && ok "无 Manifest → doctor INCOMPLETE" || bad "无 Manifest doctor 异常"
# 锁被生命周期进程持有 → INCOMPLETE
python3 - "$P" <<'PY' &
import os, sys, time
from pathlib import Path
sys.path.insert(0, ".")
from installer.transaction import acquire_install_lock
fd = acquire_install_lock(Path(sys.argv[1]))
time.sleep(4)
os.close(fd)
PY
LOCKPID=$!
sleep 0.6
python3 -m installer --doctor "$P" > "$T/doc-lock.log" 2>&1
[ $? != 0 ] && grep -q "INCOMPLETE" "$T/doc-lock.log" \
  && ok "锁被排他持有 → doctor INCOMPLETE" || bad "锁占用 doctor 异常"
# 并发第二个生命周期进程 → 立即失败
python3 -m installer "$P" > "$T/concurrent.log" 2>&1
[ $? != 0 ] && grep -q "install.lock" "$T/concurrent.log" \
  && ok "并发生命周期进程被互斥锁拒绝" || bad "互斥锁未生效"
wait $LOCKPID 2>/dev/null || true

# ══════════ 八、adopt-legacy（§18） ══════════
A="$T/adopt"
mkdir -p "$A"
python3 -m installer "$A" > /dev/null 2>&1
rm "$A/.repo-memory-kit/manifest.json"
printf 'hacked\n' > "$A/docs/memory/RULES.md"
python3 -m installer.legacy "$A" > "$T/adopt.log" 2>&1
check "adopt-legacy 退出码 0" 0 $?
grep -q "hacked" "$A/docs/memory/RULES.md" && ok "adopt-legacy 不修改内容" || bad "adopt-legacy 修改了内容"
grep -q "memory-rules: 非 kit" "$T/adopt.log" && ok "被改内容不入清单（无删除授权）" || bad "被改内容误入清单"
python3 - "$A" <<'PY' && ok "adopt 后清单含未改条目" || bad "adopt 清单内容异常"
import json, sys
from pathlib import Path
d = json.loads((Path(sys.argv[1]) / ".repo-memory-kit" / "manifest.json").read_text())
ids = {e["spec_id"] for e in d["entries"]}
assert "pitfall-template" in ids and "memory-rules" not in ids
assert all(e["action"] == "adopted_legacy" for e in d["entries"])
PY

# ══════════ 九、zvec 索引发布（§15；PYTHONPATH 补回真实 user site） ══════════
Z="$T/zvec-proj"
mkdir -p "$Z"
python3 -m installer "$Z" > /dev/null 2>&1
PYTHONPATH="$USER_SITE" python3 -m installer.zvec "$Z" > "$T/zvec1.log" 2>&1
check "zvec rebuild 退出码 0（空仓库首次：zvec/generations 由 secure_mkdir 创建）" 0 $?
[ -f "$Z/.repo-memory-kit/zvec/current.json" ] && ok "current.json 指针已发布" || bad "current.json 缺失"
n_gen=$(ls "$Z/.repo-memory-kit/zvec/generations" 2>/dev/null | wc -l)
[ "$n_gen" -ge 1 ] && ok "generation 已创建（首次重建）" || bad "generation 缺失"
GEN1=$(python3 -c "import json;print(json.load(open('$Z/.repo-memory-kit/zvec/current.json'))['generation'])")
[ -f "$Z/.repo-memory-kit/zvec/generations/$GEN1/.metadata.json" ] \
  && ok "正式元数据存在（.build-metadata.json 已删）" || bad "元数据异常"
[ -f "$Z/.repo-memory-kit/zvec/generations/$GEN1/.lock" ] && ok "reader 协议锁存在" || bad "协议锁缺失"
PYTHONPATH="$USER_SITE" python3 -m installer.zvec "$Z" > "$T/zvec2.log" 2>&1
check "二次 rebuild 退出码 0" 0 $?
GEN2=$(python3 -c "import json;print(json.load(open('$Z/.repo-memory-kit/zvec/current.json'))['generation'])")
[ "$GEN1" != "$GEN2" ] && ok "rebuild 发布新 generation（指针切换）" || bad "generation 未切换"
n_gen2=$(ls "$Z/.repo-memory-kit/zvec/generations" | wc -l)
[ "$n_gen2" -ge 2 ] && [ "$n_gen2" -le 2 ] \
  && ok "旧 generation 延迟回收边界（keep=2）" || bad "generation 数异常: $n_gen2"
PYTHONPATH="$USER_SITE" python3 -m installer.zvec --check "$Z" > "$T/zvecchk.log" 2>&1
check "reader 协议（open_current_generation）退出码 0" 0 $?
# 指针逃逸防护
python3 - "$Z" <<'PY' && ok "current.json 指向逃逸路径 → SecurityError" || bad "指针逃逸未拒绝"
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
from installer.registry import SecurityError
from installer.zvec import read_current
z = Path(sys.argv[1]) / ".repo-memory-kit" / "zvec"
(z / "current.json").write_text(json.dumps({"generation": "../../../etc"}))
try:
    read_current(Path(sys.argv[1]))
    raise SystemExit("未拒绝")
except SecurityError:
    pass
PY

# ══════════ 十、卸载残留与缩减 Manifest（§13） ══════════
U="$T/uninstall2"
mkdir -p "$U"
python3 -m installer "$U" > /dev/null 2>&1
python3 -m installer --uninstall "$U" > "$T/un2.log" 2>&1
check "完整卸载（唯一残留为 seed）退出码 0" 0 $?
[ -f "$U/docs/memory/README.md" ] && ok "seed file 永不卸载" || bad "seed 被卸载"
python3 - "$U" <<'PY' && ok "缩减版 Manifest 只含残留项" || bad "缩减 Manifest 异常"
import json, sys
from pathlib import Path
d = json.loads((Path(sys.argv[1]) / ".repo-memory-kit" / "manifest.json").read_text())
ids = [e["spec_id"] for e in d["entries"]]
assert ids == ["memory-readme-seed"], f"期望仅 seed 残留，实际 {ids}"
PY
# 再次完整安装后用 --codex-root 卸载：外部链接三段式真实执行
U2="$T/uninstall3"
CR="$T/codexroot2"
mkdir -p "$U2"
python3 -m installer --codex-root "$CR" "$U2" > "$T/un3-install.log" 2>&1
[ -L "$CR/.agents/skills/tdd" ] && ok "--codex-root 安装创建 workspace 链接" || bad "workspace 链接未创建"
python3 -m installer --uninstall --codex-root "$CR" "$U2" > "$T/un3.log" 2>&1
check "带 codex-root 卸载退出码 0" 0 $?
[ ! -L "$CR/.agents/skills/tdd" ] && ok "卸载移除 kit 链接（指向 target）" || bad "kit 链接未移除"
grep -q "seed_file 永不卸载" "$T/un3.log" && ok "卸载报告列出残留原因" || bad "卸载报告异常"

# ══════════ 十一、--dry-run 只读（附加回归） ══════════
DR="$T/dryrun"
mkdir -p "$DR"
python3 -m installer --dry-run "$DR" > "$T/dry.log" 2>&1
check "dry-run 退出码 0" 0 $?
[ ! -e "$DR/.repo-memory-kit" ] && ok "dry-run 不写入任何文件" || bad "dry-run 产生写入"

# ══════════ 十二、--svn 团队治理初始化（svnadmin 可用时执行） ══════════
if command -v svnadmin >/dev/null 2>&1 && command -v svn >/dev/null 2>&1; then
    SV="$T/svn-proj"
    svnadmin create "$T/svn-repo" >/dev/null 2>&1
    svn -q checkout "file://$T/svn-repo" "$SV" >/dev/null 2>&1
    # 存量 ignore 项不被覆盖
    svn propset -q svn:ignore ".idea" "$SV" >/dev/null 2>&1
    python3 -m installer "$SV" > /dev/null 2>&1
    python3 -m installer --svn "$SV" > "$T/svn1.log" 2>&1
    check "--svn 治理初始化退出码 0" 0 $?
    ROOT_IGNORE="$(svn propget svn:ignore "$SV" 2>/dev/null)"
    echo "$ROOT_IGNORE" | grep -q "^.idea$" && ok "根 ignore 既有项保留（合并不覆盖）" || bad "根 ignore 覆盖了既有项"
    for want in ".repo-memory-kit" ".mcp.json" ".codex"; do
        echo "$ROOT_IGNORE" | grep -qx "$want" && ok "根 ignore 含 $want" || bad "根 ignore 缺 $want"
    done
    CLAUDE_IGNORE="$(svn propget svn:ignore "$SV/.claude" 2>/dev/null)"
    echo "$CLAUDE_IGNORE" | grep -qx "settings.json" && ok ".claude ignore settings.json" || bad ".claude ignore 缺失"
    MEM_IGNORE="$(svn propget svn:ignore "$SV/docs/memory" 2>/dev/null)"
    echo "$MEM_IGNORE" | grep -qx "README.md" && echo "$MEM_IGNORE" | grep -qx ".anchors.json" \
        && ok "docs/memory ignore 生成物（README/.anchors）" || bad "docs/memory ignore 缺失"
    [ -f "$SV/docs/01-需求/README.md" ] && grep -q "需求指派索引" "$SV/docs/01-需求/README.md" \
        && ok "需求指派索引已种子（含治理规则）" || bad "需求索引缺失"
    [ "$(svn status "$SV" 2>/dev/null | grep -cE '^A.*docs/memory/(README\.md|\.anchors)')" = "0" ] \
        && ok "docs/memory 生成物未被 svn add（ignore 生效）" || bad "docs/memory 生成物被误加入"
    [ "$(svn status "$SV" 2>/dev/null | grep -c '^A.*settings.json')" = "0" ] \
        && ok "settings.json 未被 svn add（ignore 生效）" || bad "settings.json 被误加入版本控制"
    [ "$(svn status "$SV" 2>/dev/null | grep -c '^A.*repo-memory-kit')" = "0" ] \
        && ok ".repo-memory-kit 未被 svn add" || bad ".repo-memory-kit 被误加入"
    svn status "$SV" 2>/dev/null | grep -q "^A.*docs/memory/RULES.md" \
        && ok "内容确定文件已 svn add（RULES.md）" || bad "RULES.md 未登记"
    EOL="$(svn propget svn:eol-style "$SV/docs/memory/RULES.md" 2>/dev/null)"
    [ "$EOL" = "LF" ] && ok "RULES.md eol-style=LF" || bad "eol-style 未设置: '$EOL'"
    # 二次执行幂等：ignore 不重复追加
    python3 -m installer --svn "$SV" > /dev/null 2>&1
    N_RMK="$(svn propget svn:ignore "$SV" 2>/dev/null | grep -c '.repo-memory-kit')"
    check "二次 --svn 幂等（ignore 不重复）" 1 "$N_RMK"
else
    ok "（跳过：无 svnadmin）--svn 治理初始化"
fi

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
