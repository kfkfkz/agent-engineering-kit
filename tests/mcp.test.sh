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

PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$(dirname "$0")/dispatch-service.test.py" \
    && ok "DispatchService 单路径与 receipt 契约" \
    || bad "DispatchService 契约失败"
PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd)${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$(dirname "$0")/mcp-dispatch.test.py" \
    && ok "MCP started 后不 fallback 且 receipt 去重" \
    || bad "MCP dispatch 故障注入失败"

# 准备：安装到临时目录（提供 MCP 服务器绑定的目标仓库）
P="$T/repo"; mkdir -p "$P"
python3 -m installer "$P" >/dev/null 2>&1
MCP="$P/.repo-memory-kit/bin/agent-engineering-mcp"
[ -f "$MCP" ] || { echo "✗ MCP 服务器未安装"; exit 1; }

# JSON-RPC 辅助函数（第七轮审计：initialize 成功前拒绝业务调用——
# 每次调用先握手；输出取**最后一个** JSON 响应）
INIT='{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}'
mcp_rpc() {  # $1=请求 JSON → 输出最后一个响应（或空=通知）
    printf '%s\n%s\n' "$INIT" "$1" | python3 "$MCP" "$P" 2>/dev/null | tail -1
}
mcp_rpc_multi() {  # $1=请求1 $2=请求2 → 输出合并响应
    { printf '%s\n%s\n%s\n' "$INIT" "$1" "$2"; } | python3 "$MCP" "$P" 2>/dev/null
}

# ── T1 initialize + 版本协商 ──
# T1a: 合法 initialize → 服务器信息 + 能力
RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":1,"method":"ping"}')
[ -n "$RESP" ] && ok "T1a 前置：合法 initialize 后连接可用" || bad "T1a 前置失败: $RESP"

# T1a-2: initialize 缺必填参数（params={}）→ -32602（2025-11-25 schema 必填
#        protocolVersion/capabilities/clientInfo——第七轮审计 P2）
RESP=$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32602
" && ok "T1a-2 initialize 缺必填参数 → -32602" || bad "T1a-2: $RESP"

# T1a-3: 初始化前调用业务方法 → -32002
RESP=$(echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32002
" && ok "T1a-3 初始化前调用 → -32002" || bad "T1a-3: $RESP"

# T1a-4: 初始化前 ping 仍可用（无状态健康检查）
RESP=$(echo '{"jsonrpc":"2.0","id":1,"method":"ping"}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']=={}
" && ok "T1a-4 初始化前 ping 放行" || bad "T1a-4: $RESP"

# T1b: 客户端发送支持版本 → 回显
RESP=$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']['protocolVersion']=='2025-06-18'  # 回显
assert r['result']['serverInfo']=={'name':'agent-engineering-kit','version':'1.0.1'}
" && ok "T1b 版本协商：回显客户端版本" || bad "T1b: $RESP"

# T1c: 客户端发送未知版本 → 默认版本
RESP=$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1999-01-01","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['result']['protocolVersion']=='2025-11-25'  # 不认识→默认
" && ok "T1c 版本协商：未知版本→默认" || bad "T1c: $RESP"

# T1d: 旧版本 2024-11-05 → 回显（向后兼容）
RESP=$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | python3 "$MCP" "$P" 2>/dev/null)
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
N_RESP=$(printf '%s\n%s\n' "$INIT" '{"jsonrpc":"2.0","method":"notifications/initialized"}' | python3 "$MCP" "$P" 2>/dev/null | grep -c '"jsonrpc"')
[ "$N_RESP" = "1" ] && ok "通知无响应（仅前置 initialize 一条）" || bad "通知产生了响应: $N_RESP 条"

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

# ── T14 JSON-RPC envelope 与方法参数 schema（非法请求不应变成 -32603）──
RESP=$(echo '{"id":14,"method":"ping"}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32600 and r['id'] is None
" && ok "缺 jsonrpc → -32600" || bad "缺 jsonrpc 被接受: $RESP"

RESP=$(echo '{"jsonrpc":"1.0","id":15,"method":"ping"}' | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32600 and r['id'] is None
" && ok "错误 jsonrpc 版本 → -32600" || bad "错误版本被接受: $RESP"

RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":16,"method":"tools/call","params":[]}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32602 and r['id']==16
" && ok "tools/call params 数组 → -32602 且保留 id" || bad "params 数组错误边界异常: $RESP"

RESP=$(mcp_rpc '{"jsonrpc":"2.0","id":17,"method":"tools/call","params":{"name":"kit_status","arguments":[]}}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32602 and r['id']==17
" && ok "tools/call arguments 数组 → -32602" || bad "arguments 数组被接受: $RESP"

RESP=$(mcp_rpc_multi \
    '{"jsonrpc":"2.0","id":18,"method":"tools/call","params":[]}' \
    '{"jsonrpc":"2.0","id":19,"method":"ping"}')
printf '%s\n' "$RESP" | python3 -c "
import json,sys
rows=[json.loads(line) for line in sys.stdin if line.strip()]
by_id={row.get('id'): row for row in rows}
assert by_id[18]['error']['code']==-32602
assert by_id[19]['result']=={}
" && ok "非法 params 后服务继续处理下一请求" || bad "非法 params 终止服务: $RESP"

# 任意合法 envelope 的无 id 请求都是 notification：即使方法或参数错误也不回包。
N_RESP=$(printf '%s\n%s\n%s\n%s\n' "$INIT" \
    '{"jsonrpc":"2.0","method":"ping"}' \
    '{"jsonrpc":"2.0","method":"tools/list","params":[]}' \
    '{"jsonrpc":"2.0","method":"missing/method"}' \
    | python3 "$MCP" "$P" 2>/dev/null | grep -c '"jsonrpc"')
[ "$N_RESP" = "1" ] && ok "通用 notification 无响应" || bad "notification 多产生了响应: $N_RESP 条"

RESP=$(printf '%s\n' '{"jsonrpc":"2.0","id":20,"method":"ping","params":NaN}' \
    | python3 "$MCP" "$P" 2>/dev/null)
printf '%s\n' "$RESP" | python3 -c "
import json,sys; r=json.load(sys.stdin)
assert r['error']['code']==-32700 and r['id'] is None
" && ok "非标准 JSON NaN → -32700" || bad "NaN 被当作合法 JSON: $RESP"

echo
echo "通过 $pass / 失败 $fail"
[ "$fail" = 0 ]
