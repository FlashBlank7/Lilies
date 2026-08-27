#!/usr/bin/env bash
# 开发期重启平台 API（bagpipe）。改了 agent_platform 代码后必须跑一次——
# 进程不会自动装载新代码（本会话已三次踩坑）。用 DB 持有者定位进程，避免 pkill 自匹配。
set -e
cd "$(dirname "$0")/.."
OLD=$(fuser data/agent_platform.db 2>/dev/null | xargs || true)
for p in $OLD; do kill "$p" 2>/dev/null || true; done
sleep 2
HOST=127.0.0.1 setsid nohup ~/.local/bin/uv run agent-platform > ~/platform-api.log 2>&1 < /dev/null &
for i in $(seq 1 30); do
  curl -s --max-time 3 http://127.0.0.1:8000/health | grep -q '"ok"' && { echo "api ready"; exit 0; }
  sleep 2
done
echo "api FAILED to start"; tail -5 ~/platform-api.log; exit 1
