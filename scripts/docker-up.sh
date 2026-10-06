#!/usr/bin/env bash
# 启动独立的 Lilies Compose 实例；恢复时将 LILIES_STATE_DIR 指向恢复目录。
# 可设置 LILIES_API_PORT、LILIES_WEB_PORT 和 LILIES_DOCKER_SOCKET。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

fail() { echo "错误：$*" >&2; exit 1; }
case "${1:-}" in
  ""|--build|--down|--logs|--status) ;;
  *) fail "用法：$0 [--build|--down|--logs|--status]" ;;
esac
[[ $# -le 1 ]] || fail "只接受一个操作参数。"

# 相对路径始终相对于源码目录，与调用脚本时所在的目录无关。
mkdir -p "${LILIES_STATE_DIR:-$ROOT}"
export LILIES_STATE_DIR="$(cd "${LILIES_STATE_DIR:-$ROOT}" && pwd -P)"
export LILIES_DATA_DIR="$LILIES_STATE_DIR/data"
export LILIES_WORKSPACE_DIR="$LILIES_STATE_DIR/workspaces"
mkdir -p "$LILIES_DATA_DIR" "$LILIES_WORKSPACE_DIR"
for directory in "$LILIES_DATA_DIR" "$LILIES_WORKSPACE_DIR"; do
  [[ -r "$directory" && -w "$directory" && -x "$directory" ]] ||
    fail "当前用户无法读写 ${directory}；请核对恢复目录的所有者和权限。"
done

export LILIES_UID="$(id -u)" LILIES_GID="$(id -g)"
export LILIES_API_PORT="${LILIES_API_PORT:-8000}"
export LILIES_WEB_PORT="${LILIES_WEB_PORT:-3000}"
for port in "$LILIES_API_PORT" "$LILIES_WEB_PORT"; do
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) ||
    fail "端口必须是 1 至 65535 的整数：$port"
done

command -v docker >/dev/null || fail "未安装 Docker。"
docker info >/dev/null 2>&1 || fail "无法连接 Docker daemon；请检查 Docker 是否启动及当前用户权限。"
export LILIES_DOCKER_SOCKET="${LILIES_DOCKER_SOCKET:-/var/run/docker.sock}"
[[ "$LILIES_DOCKER_SOCKET" = /* ]] || fail "LILIES_DOCKER_SOCKET 必须是绝对路径。"
[[ -S "$LILIES_DOCKER_SOCKET" ]] || fail "找不到 Docker socket：$LILIES_DOCKER_SOCKET"
if [[ -z "${LILIES_DOCKER_GID:-}" ]]; then
  if [[ "$(uname -s)" = Darwin ]]; then
    # Docker Desktop 将挂载后的 socket 映射为容器内的 root 组。
    LILIES_DOCKER_GID=0
  else
    LILIES_DOCKER_GID="$(stat -L -c '%g' "$LILIES_DOCKER_SOCKET" 2>/dev/null || stat -L -f '%g' "$LILIES_DOCKER_SOCKET")"
  fi
fi
[[ "$LILIES_DOCKER_GID" =~ ^[0-9]+$ ]] || fail "无法读取 Docker socket 所属组。"
export LILIES_DOCKER_GID

# 实例和基础镜像均按数据目录区分，构建不会覆盖其他实例使用的镜像标签。
PROJECT="lilies-$(printf '%s' "$LILIES_STATE_DIR" | cksum | awk '{print $1}')"
export LILIES_BASE_SANDBOX_IMAGE="$PROJECT:sandbox"
export LILIES_BASE_MODELING_IMAGE="$PROJECT:modeling"
CONFIG="$LILIES_STATE_DIR/.env"
[[ ! -e "$CONFIG" || -r "$CONFIG" ]] || fail "无法读取恢复配置：$CONFIG"
[[ -f "$CONFIG" ]] || CONFIG=/dev/null
COMPOSE=(docker compose --project-name "$PROJECT" --env-file "$CONFIG" -f "$ROOT/compose.yaml")

show_urls() {
  echo "数据目录：$LILIES_STATE_DIR"
  echo "API：http://127.0.0.1:$LILIES_API_PORT"
  echo "Studio：http://127.0.0.1:$LILIES_WEB_PORT"
}
case "${1:-}" in
  --down)
    "${COMPOSE[@]}" down
    echo "已停止实例；数据保留在 ${LILIES_STATE_DIR}。"
    ;;
  --logs) "${COMPOSE[@]}" logs -f --tail=100 ;;
  --status)
    "${COMPOSE[@]}" ps
    show_urls
    ;;
  --build)
    "${COMPOSE[@]}" build --no-cache
    "${COMPOSE[@]}" up -d --wait --wait-timeout 60
    show_urls
    ;;
  "")
    "${COMPOSE[@]}" up -d --build --wait --wait-timeout 60
    show_urls
    ;;
esac
