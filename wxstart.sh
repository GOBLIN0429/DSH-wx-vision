#!/usr/bin/env bash
# Start the WeChat bridge, after making sure nothing from a previous run
# survived. A stale session still running its old queue is exactly how a
# "stopped" bridge keeps typing into WeChat, so start always pre-cleans.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
BIN="$HERE/bin"
PYD="$HERE/py"
SESSION="${WX_SESSION:-session-f528633e-7047-4cbd-ad4e-fd3c773ecb1b}"
MODE="${WX_MODE:-multi}"
LOG="/tmp/wxbridge.log"
TIMING="/tmp/wxbridge_timing.log"
STATE="/tmp/wxbridge.state"

say() { printf '%s\n' "$*"; }

say "=== 启动前清理残留 ==="
if ! "$HERE/wxstop.sh" >/tmp/wxstop.out 2>&1; then
  say "  上一次的停止不彻底，详情："
  sed 's/^/    /' /tmp/wxstop.out
  say "  继续尝试启动，但请留意上面的告警。"
else
  say "  ✓ 环境干净"
fi

say ""
say "=== 检查前置条件 ==="
if ! pgrep -f "微信.app/Contents/MacOS" >/dev/null 2>&1; then
  say "  ⚠ 微信似乎没有运行，检测器可能找不到窗口"
fi
"$PY" "$PYD/wxctl.py" status "$SESSION" | sed 's/^/  /'

say ""
say "=== 固定窗口几何 ==="
GEOM_JSON="$("$BIN/wxgeom" 2>/dev/null)"
say "  $GEOM_JSON"
if ! printf '%s' "$GEOM_JSON" | grep -q '"ok":true'; then
  say "  ✗ 无法把微信窗口调整到目标几何，后面的坐标会不可靠"
  exit 1
fi

say ""
say "=== 启动桥 ==="
: > "$TIMING"
if [[ "$MODE" == "multi" ]]; then
  nohup "$PY" "$PYD/wxmulti.py" --session "$SESSION" >"$LOG" 2>&1 &
else
  nohup "$PY" "$PYD/wxbridge.py" --session "$SESSION" >"$LOG" 2>&1 &
fi
BPID=$!
say "  pid=$BPID  日志=$LOG"

# Readiness means the detector is up, not just that the process exists: the
# bridge scans a baseline frame first, which can take several seconds on a busy
# conversation. Checking too early reports a false failure.
# Readiness differs by mode: the single-group bridge logs "baseline" after its
# first scan, the multi-group watcher logs "watching" once its detector is up.
READY_SENTINEL="baseline"
[[ "$MODE" == "multi" ]] && READY_SENTINEL="watching"

READY=0
for _ in $(seq 1 120); do
  if ! kill -0 "$BPID" 2>/dev/null; then break; fi
  if grep -q "$READY_SENTINEL" "$LOG" 2>/dev/null && pgrep -f "wxwatch" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.25
done

if ! kill -0 "$BPID" 2>/dev/null; then
  say "  ✗ 桥进程退出了，最后 20 行日志："
  tail -20 "$LOG" | sed 's/^/    /'
  exit 1
fi

if [[ "$READY" -ne 1 ]]; then
  say "  ✗ 配置超时（检测器未就绪），最后 20 行日志："
  tail -20 "$LOG" | sed 's/^/    /'
  exit 1
fi

say "  ✓ 已启动"
say ""
say "=== 运行状态 ==="
say "  进程     : $(pgrep -f 'wxbridge.py|wxmulti.py' | tr '\n' ' ')  (${MODE})"
say "  检测器 : $(pgrep -f 'wxwatch' | tr '\n' ' ')"
say "  会话   : $SESSION"
say "  模式   : $MODE"
say "  群列表 : $(tr '\n' ',' < "$HERE/groups.txt" 2>/dev/null)"
say ""
say "停止：$HERE/wxstop.sh"
