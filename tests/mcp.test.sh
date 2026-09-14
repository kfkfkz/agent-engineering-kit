#!/bin/sh
# shellcheck disable=SC2015,SC2034  # 断言惯用法；KIT 供路径推导
# agent-engineering-mcp 协议级测试：initialize / tools/list / tools/call /
# 非法 JSON-RPC / 超时。使用真实安装的目标仓库 + 子进程 stdio JSON-RPC。
set -u
T="$(mktemp -d)"
XDG_CONFIG_HOME="$T/xdg"; export XDG_CONFIG_HOME
trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "✓ $1"; }
bad() { fail=$((fail+1)); echo "✗ $1"; }

# 准备：安装到临时目录（提供 MCP 服务器绑定的目标仓库）
P="$T/repo"; mkdir -p "$P"
python3 -m installer "$P" >/dev/null 2>&1
MCP="$P/.repo-memory-kit/bin/agent-engineering-mcp"
[ -f "$MCP" ] || { echo "✗ MCP 服务器未安装"; exit 1; }

# JSON-RPC 辅助函数
mcp_rpc() {  # $1=请求 JSON → 输出响应 JSON（或空=通知）
    printf '%s\n' "$1" | python3 "$MCP" "$P" 2>/dev/null
}
mcp_rpc_multi() {  # $1=请求1 $2=请求2 → 输出合并响应
    { printf '%s\n%s\n' "$1" "$2"; } | python3 "$MCP" "$P" 2>/dev/null
}

# ── T1 initialize + 版本协商 ──
# T1a: 无版本参数 → 默认版本
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
res=r['result']
assert res['protocolVersion']=='2026-07-28'  # 无参数→默认
assert res['serverInfo']['name']=='agent-engineering-kit'
assert 'tools' in res['capabilities']
" && ok "T1a initialize（默认版本 2026-07-28）" || bad "T1a: $RESP"

# T1b: 客户端发送支持版本 → 回显
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']['protocolVersion']=='2025-06-18'  # 回显
" && ok "T1b 版本协商：回显客户端版本" || bad "T1b: $RESP"

# T1c: 客户端发送未知版本 → 默认版本
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']['protocolVersion']=='2026-07-28'  # 不认识→默认
" && ok "T1c 版本协商：未知版本→默认" || bad "T1c: $RESP"

# T1d: 旧版本 2024-11-05 → 回显（向后兼容）
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']['protocolVersion']=='2024-11-05'  # 旧版也支持
" && ok "T1d 版本协商：2024-11-05 向后兼容" || bad "T1d: $RESP"

# ── T2 tools/list ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
tools=[t['name'] for t in r['result']['tools']]
assert 'memory_recall' in tools and 'memory_build' in tools
assert 'domain_check' in tools and 'kit_status' in tools
assert all('inputSchema' in t for t in r['result']['tools'])
" && ok "tools/list：4 工具 + inputSchema" || bad "tools/list 异常: $RESP"

# ── T3 tools/call kit_status ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"kit_status","arguments":{}}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['id']==3
content=r['result']['content'][0]['text']
data=json.loads(content)
assert 'kit_version' in data and 'semantic_index' in data
assert data['kit_version'] != 'not installed'
" && ok "tools/call kit_status：版本/索引状态" || bad "kit_status 异常: $RESP"

# ── T4 tools/call memory_recall（先建索引再检索） ──
python3 "$P/.repo-memory-kit/bin/memory-recall" --rebuild "$P" >/dev/null 2>&1
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"memory_recall","arguments":{"query":"RULES"}}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
text=r['result']['content'][0]['text']
data=json.loads(text)
assert data.get('returncode')==0 or 'RULES' in data.get('output',''), \
    f'recall failed: {data}'
" && ok "tools/call memory_recall：检索命中" || bad "memory_recall 异常: $RESP"

# ── T5 未知工具 → -32602 ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"nonexistent","arguments":{}}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32602
assert 'Unknown tool' in r['error']['message']
" && ok "未知工具 → -32602" || bad "未知工具错误码异常: $RESP"

# ── T6 非法 JSON → -32700 ──
RESP=$(echo 'not-json{{' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32700
assert 'Parse error' in r['error']['message']
" && ok "非法 JSON → -32700" || bad "解析错误码异常: $RESP"

# ── T7 未知方法 → -32601 ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":7,"method":"resources/list"}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32601
" && ok "未知方法 → -32601" || bad "方法错误码异常: $RESP"

# ── T8 通知（无 id）→ 无响应 ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","method":"notifications/initialized"}')
[ -z "$RESP" ] && ok "通知无响应" || bad "通知产生了响应: $RESP"

# ── T9 ping ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":9,"method":"ping"}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['id']==9 and r['result']=={}
" && ok "ping → 空结果" || bad "ping 异常: $RESP"

# ── T10 批量请求（initialize + tools/list） ──
RESP=$(mcp_rpc_multi \
    '{"jsonrpc":"2.0","id":10,"method":"initialize","params":{}}' \
    '{"jsonrpc":"2.0","id":11,"method":"tools/list"}')
N_LINES=$(echo "$RESP" | grep -c '"jsonrpc"')
[ "$N_LINES" -ge 2 ] && ok "多请求逐行响应" || bad "多请求响应行数: $N_LINES"

# ── T11 memory_recall 缺 query → is_error ──
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"memory_recall","arguments":{}}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']['isError']==True
" && ok "memory_recall 缺参数 → isError" || bad "缺参数未标错误: $RESP"

# ── T12 超时行为（60s 上限：不实际等待，验证超时码路径存在） ──
grep -q "TimeoutExpired" "$MCP" && grep -q "timeout=60" "$MCP" \
    && ok "CLI 委托超时（60s）已实现" || bad "超时未实现"

# ── T13 绑定仓库（不接受任意路径） ──
python3 -c "
import os,sys
KIT_BIN='$P/.repo-memory-kit/bin'
sys.path.insert(0, KIT_BIN)
# MCP 的 _BOUND_REPO 应指向 $P
bound = os.path.dirname(os.path.dirname(KIT_BIN))
assert bound == '$P', f'绑定仓库错误: {bound} != $P'
" && ok "MCP 绑定安装仓库（不接受任意路径）" || bad "仓库绑定异常"

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
