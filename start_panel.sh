#!/usr/bin/env bash
# OutlookRegister 面板启动器
# 作用：先确保 Xvfb 虚拟显示真实可连接，再拉起 web_panel_server.py。
# 背景：面板内置 X11/Wayland 预检，若服务进程连不上图形显示，/api/start 会直接返回
#       409 DISPLAY_UNAVAILABLE 并拒绝注册任务（避免空跑住宅代理）。
set -u
cd "$(dirname "$0")"

DISPLAY_NUM="${PANEL_DISPLAY_NUM:-99}"
DISPLAY_VAL=":${DISPLAY_NUM}"
SOCKET_PATH="/tmp/.X11-unix/X${DISPLAY_NUM}"
LOCK_PATH="/tmp/.X${DISPLAY_NUM}-lock"
PORT="${PANEL_PORT:-8890}"
LOG_DIR="$(pwd)"

x_display_ok() {
  python3 - "$SOCKET_PATH" <<'PY'
import socket, sys
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        s.connect(sys.argv[1])
except OSError:
    sys.exit(1)
PY
}

if x_display_ok; then
  echo "[*] X 显示 ${DISPLAY_VAL} 已可用"
else
  echo "[*] X 显示 ${DISPLAY_VAL} 不可用，清理陈旧套接字并启动 Xvfb ..."
  rm -f "$LOCK_PATH" "$SOCKET_PATH"
  setsid nohup Xvfb "${DISPLAY_VAL}" -screen 0 1920x1080x24 -ac +extension GLX +render -noreset \
    > "/tmp/xvfb${DISPLAY_NUM}.log" 2>&1 < /dev/null &
  for _ in $(seq 1 20); do
    x_display_ok && break
    sleep 0.5
  done
  if ! x_display_ok; then
    echo "[!] Xvfb 启动失败，请检查 /tmp/xvfb${DISPLAY_NUM}.log" >&2
    exit 1
  fi
  echo "[*] Xvfb ${DISPLAY_VAL} 已启动"
fi

if curl -s --max-time 3 "http://127.0.0.1:${PORT}/api/status" > /dev/null 2>&1; then
  echo "[*] 面板已在 127.0.0.1:${PORT} 运行，无需重复启动"
  exit 0
fi

echo "[*] 启动面板（DISPLAY=${DISPLAY_VAL}, PANEL_PORT=${PORT}）..."
setsid env DISPLAY="${DISPLAY_VAL}" PANEL_PORT="${PORT}" nohup python3 -u web_panel_server.py \
  > "${LOG_DIR}/panel_stdout.log" 2>&1 < /dev/null &
sleep 2

if curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/status" > /dev/null 2>&1; then
  echo "[*] 面板已就绪：http://<本机IP>:${PORT}"
else
  echo "[!] 面板启动异常，请查看 ${LOG_DIR}/panel_stdout.log" >&2
  exit 1
fi
