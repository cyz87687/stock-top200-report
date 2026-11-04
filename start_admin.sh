#!/bin/bash
# round125｜权重配置后台一键启动
#
# ⚠️ 进程存活机制的实话（本机实测，勿再踩）：
#   · macOS launchd **装载不了**（`launchctl bootstrap gui/501` 报
#     `Bootstrap failed: 5: Input/output error`，本机沙箱拦截系统级服务注册）。
#     → 所以没有常驻守护，别指望开机自启。
#   · `nohup ... &` 脱离终端也**会被回收**（实测启动 2 秒后端口就空了）。
#   · 真正能活的是「由宿主会话托管的进程」。
#   ⇒ 结论：每次要用后台，就执行本脚本重新拉起；浏览器标签页刷新即可恢复，
#     不需要重启机器。这是本机能力边界，不是配置没写对。
#
# 用法：
#   ./start_admin.sh          # 起服务（已在跑则只报状态）
#   ./start_admin.sh status   # 看状态
#   ./start_admin.sh stop     # 停掉
#   ./start_admin.sh open     # 起服务并打开浏览器
#
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PY="$HOME/.workbuddy/binaries/python/envs/default/bin/python"
PORT="${ADMIN_PORT:-8788}"
HOST="127.0.0.1"
PIDFILE="$DIR/.admin_server.pid"
LOG="$DIR/.admin_server.log"
URL="http://$HOST:$PORT/"

alive() {
  [ -f "$PIDFILE" ] || return 1
  local p
  p="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

# 端口上是否有本后台在应答（比 PID 更可信：进程重启后 PID 会变）
probe() {
  curl -s -o /dev/null -m 2 -w "%{http_code}" "$URL" 2>/dev/null | grep -q 200
}

case "${1:-start}" in
  status)
    if probe; then
      echo "✅ 后台在跑  $URL"
      [ -f "$PIDFILE" ] && echo "   pid = $(cat "$PIDFILE")"
      echo "   日志 = $LOG"
    else
      echo "❌ 后台未运行"
      exit 1
    fi
    ;;

  stop)
    if alive; then
      kill "$(cat "$PIDFILE")" 2>/dev/null && echo "已停止 (pid $(cat "$PIDFILE"))"
      rm -f "$PIDFILE"
    elif probe; then
      echo "⚠️ 端口 $PORT 上有服务但 PID 文件不匹配，按端口清理"
      lsof -ti "tcp:$PORT" 2>/dev/null | xargs -r kill
    else
      echo "未在运行"
    fi
    ;;

  open)
    "$0" start
    sleep 1
    if probe; then
      case "$(uname -s)" in
        Darwin) open "$URL" ;;
        Linux)  xdg-open "$URL" >/dev/null 2>&1 || echo "请手动打开 $URL" ;;
        *)      echo "请手动打开 $URL" ;;
      esac
    fi
    ;;

  start|*)
    if probe; then
      echo "✅ 后台已在运行  $URL"
      echo "   改完权重点页面上的「生效并重生成 index」即可"
      exit 0
    fi
    # 清掉残留 pid
    rm -f "$PIDFILE"
    echo "启动后台 ..."
    # setsid/nohup：让进程脱离当前终端，避免会话结束被回收
    nohup "$PY" admin_server.py --port "$PORT" --host "$HOST" \
      >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    # 等最多 8 秒确认端口应答
    for _ in $(seq 1 16); do
      if probe; then
        echo "✅ 已启动  $URL"
        echo "   pid  = $(cat "$PIDFILE")"
        echo "   日志 = $LOG"
        echo
        echo "   展示页 index.html 与维护页分离："
        echo "   不点「生效」→ index.html 保持原样"
        exit 0
      fi
      sleep 0.5
    done
    echo "❌ 启动失败，最近日志："
    tail -20 "$LOG" 2>/dev/null
    exit 1
    ;;
esac