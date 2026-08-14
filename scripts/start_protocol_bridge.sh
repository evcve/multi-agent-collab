#!/bin/bash
# Protocol Bridge (超越版协作桥, port 8766) 启动脚本（WSL 侧）
# Hermes 新协议：multi-agent-collab/protocol/mcp_server.py
# 用法：bash scripts/start_protocol_bridge.sh
# 日志：/tmp/protocol-bridge-8766.log
set -u
PROJ_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY=/root/.openclaw/workspace/tools/openclaw-bridge/.venv/bin/python
LOG=/tmp/protocol-bridge-8766.log

if pgrep -f "protocol.mcp_server --port 8766" > /dev/null 2>&1; then
  echo "protocol bridge (8766) already running: $(pgrep -f 'protocol.mcp_server --port 8766')"
  exit 0
fi

cd "$PROJ_DIR" || exit 1
setsid env PYTHONPATH="$PROJ_DIR" PROTOCOL_KEYFILE=/root/.zhipu_key \
  "$VENV_PY" -m protocol.mcp_server --port 8766 --host 0.0.0.0 \
  > "$LOG" 2>&1 < /dev/null &
sleep 3
if pgrep -f "protocol.mcp_server --port 8766" > /dev/null 2>&1; then
  echo "protocol bridge started: PID $(pgrep -f 'protocol.mcp_server --port 8766')"
  echo "MCP endpoint: http://127.0.0.1:8766/mcp"
else
  echo "protocol bridge FAILED to start, log:"
  tail -20 "$LOG"
  exit 1
fi
