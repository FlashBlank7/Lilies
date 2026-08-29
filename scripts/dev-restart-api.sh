#!/usr/bin/env bash
# 开发期重启平台 API（bagpipe）。改了 agent_platform 代码后必须跑一次——
# 进程不会自动装载新代码。
#
# 两处都栽过，都写在这里：
#
# 1) 找进程别用 `fuser data/agent_platform.db`。
#    2026-08-29 把 SQLite 连接改成"用完即关"之后，服务不再常驻持有那个文件，
#    fuser 返回空 → 旧进程没被杀 → 新进程绑不上端口，自己退了。
#    改用监听端口定位：那是"谁在服务"的唯一权威。
#    （也别用 pkill 按名字匹配——它会匹配到跑这个脚本的 shell 自己，本会话中过四次。）
#
# 2) 光看 /health 返回 200 不能算重启成功。
#    上面那次事故里，应答的是**旧进程**：脚本每次都打印 "api ready"，
#    而线上跑的代码停在 12 个提交之前，期间所有"真机验证"验的都是旧代码。
#    所以要核对 /health 报的 git commit 就是当前 HEAD——
#    检查声称的东西不能多于它验证的东西。
set -e
cd "$(dirname "$0")/.."

PORT=8000
listener() { ss -lptn "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1; }

OLD=$(listener || true)
if [ -n "$OLD" ]; then
  kill "$OLD" 2>/dev/null || true
  for _ in $(seq 1 15); do
    [ -z "$(listener || true)" ] && break
    sleep 1
  done
  # 还赖着不走就来硬的：端口占着的话新进程根本起不来
  STILL=$(listener || true)
  [ -n "$STILL" ] && kill -9 "$STILL" 2>/dev/null || true
  sleep 1
fi

if [ -n "$(listener || true)" ]; then
  echo "端口 $PORT 还被 pid $(listener) 占着，杀不掉——先手工处理" >&2
  exit 1
fi

HOST=127.0.0.1 setsid nohup ~/.local/bin/uv run agent-platform > ~/platform-api.log 2>&1 < /dev/null &

WANT=$(git rev-parse --short HEAD)
for _ in $(seq 1 30); do
  # /health 的细节要令牌了（免鉴权那份只回 status:ok——
  # 原先它把 git 提交、路由图、工具清单都免费发出去，而 Docker 默认绑 0.0.0.0）
  TOKEN=$(grep -m1 '^API_TOKEN=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
  GOT=$(curl -s --max-time 3 -H "Authorization: Bearer $TOKEN" \
        http://127.0.0.1:$PORT/health 2>/dev/null \
        | grep -oP '"commit":"\K[^"]+' || true)
  if [ -n "$GOT" ]; then
    if [ "$GOT" = "$WANT" ]; then
      echo "api ready（$GOT，pid $(listener)）"
      exit 0
    fi
    echo "api 起来了但跑的是 $GOT，不是当前 HEAD $WANT——多半是旧进程还在应答" >&2
    tail -5 ~/platform-api.log >&2
    exit 1
  fi
  sleep 2
done
echo "api FAILED to start"; tail -8 ~/platform-api.log; exit 1
