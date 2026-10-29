#!/usr/bin/env bash
# Stop the WeChat bridge COMPLETELY.
#
# Killing the scripts is only half the job. Any prompt already injected into a
# DSH session keeps that session's agent running, and that agent will keep
# activating WeChat and typing. So a clean stop is three layers:
#
#   1. cancel the injected session and wait for its queue to drain
#   2. terminate the bridge and its detector child
#   3. verify nothing survived — process table AND session state
#
# Exits non-zero if anything is still alive.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
PYD="$HERE/py"
STATE="/tmp/wxbridge.state"
FAIL=0

say() { printf '%s\n' "$*"; }

say "=== 1. 取消被注入的会话 ==="
SID=""
if [[ -f "$STATE" ]]; then
  SID="$("$PY" -c "import json;print(json.load(open('$STATE')).get('session_id',''))" 2>/dev/null)"
fi
if [[ -n "${1:-}" ]]; then SID="$1"; fi
if [[ -n "$SID" ]]; then
  say "target session: $SID"
  "$PY" "$PYD/wxctl.py" cancel "$SID" || FAIL=1
else
  say "没有记录的目标会话（跳过）"
fi

say ""
say "=== 2. 终止桥与检测器 ==="
for pat in "wxbridge.py" "wxmulti.py" "wxwatch" "wxbot.py"; do
  pids="$(pgrep -f "$pat" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    say "  停止 $pat: $pids"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
  else
    say "  $pat: 未运行"
  fi
done

# Give them a moment to exit on their own before forcing.
for _ in $(seq 1 10); do
  if ! pgrep -f "wxbridge.py|wxmulti.py|wxwatch|wxbot.py" >/dev/null 2>&1; then break; fi
  sleep 0.3
done

for pat in "wxbridge.py" "wxmulti.py" "wxwatch" "wxbot.py"; do
  pids="$(pgrep -f "$pat" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    say "  强制结束 $pat: $pids"
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
done

say ""
say "=== 3. 校验：进程 ==="
LEFT="$(pgrep -f "wxbridge.py|wxmulti.py|wxwatch|wxbot.py|wxsend.py|wxtool" 2>/dev/null || true)"
if [[ -n "$LEFT" ]]; then
  say "  ✗ 仍有残留进程："
  pgrep -fl "wxbridge.py|wxmulti.py|wxwatch|wxbot.py|wxsend.py|wxtool" 2>/dev/null | sed 's/^/    /'
  FAIL=1
else
  say "  ✓ 无残留进程"
fi

say ""
say "=== 4. 校验：会话是否还在跑 ==="
RUNNING="$("$PY" "$PYD/wxctl.py" any-running 2>/dev/null || true)"
OTHERS=""
if [[ -n "$RUNNING" ]]; then
  # The bridge's own session must not be running after a stop.
  if [[ -n "$SID" ]] && printf '%s\n' "$RUNNING" | grep -q "$SID"; then
    say "  ✗ 目标会话仍在运行：$SID"
    FAIL=1
  else
    say "  ✓ 目标会话已停止"
  fi
  OTHERS="$(printf '%s\n' "$RUNNING" | { [[ -n "$SID" ]] && grep -v "$SID" || cat; } || true)"
  if [[ -n "$OTHERS" ]]; then
    say "  ℹ 另有会话在运行（不归本桥管，通常是你在用的窗口）："
    printf '%s\n' "$OTHERS" | sed 's/^/    /'
  fi
else
  say "  ✓ 没有运行中的会话"
fi

say ""
if [[ "$FAIL" -eq 0 ]]; then
  say "✅ 已完全停止：进程清空，会话已取消，队列为空。"
  rm -f "$STATE"
  exit 0
else
  say "❌ 停止不彻底，见上面的 ✗ 项。"
  exit 1
fi
