#!/usr/bin/env bash
# =============================================================================
# Lilies — Docker Compose 一键部署
#
# 前置条件: 只安装 Docker（不需要 Python/Node.js）
# 首次运行会自动构建镜像（约 3-5 分钟）
#
# 用法:
#   ./scripts/docker-up.sh             启动全部服务
#   ./scripts/docker-up.sh --build     强制重新构建镜像
#   ./scripts/docker-up.sh --down      停止并清理
#   ./scripts/docker-up.sh --logs      查看实时日志
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── 前置检查 ──────────────────────────────────────────────────

check_prereqs() {
  local missing=()

  if ! command -v docker &>/dev/null; then
    echo "❌ Docker is not installed."
    echo "   Install: https://docs.docker.com/engine/install/"
    missing+=("docker")
  fi

  if command -v docker &>/dev/null && ! docker info &>/dev/null; then
    echo "❌ Docker daemon is not running."
    echo "   Linux:   sudo systemctl start docker"
    echo "   macOS:   open -a Docker"
    missing+=("docker-daemon")
  fi

  if command -v docker &>/dev/null && ! docker ps &>/dev/null; then
    echo "❌ Cannot access Docker. Add your user to the docker group:"
    echo "   sudo usermod -aG docker \$USER"
    echo "   Then log out and back in."
    missing+=("docker-permission")
  fi

  if [[ ! -f .env ]]; then
    echo "❌ .env file is missing."
    echo "   cp .env.example .env"
    echo "   Then edit .env and set DEEPSEEK_API_KEY"
    missing+=("dotenv")
  else
    # Quick check if API key is set
    if ! grep -q "DEEPSEEK_API_KEY=sk-" .env 2>/dev/null; then
      echo "⚠️  DEEPSEEK_API_KEY does not look configured in .env"
      echo "   Edit .env and set a valid API key from https://platform.deepseek.com"
    fi
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo ""
    echo "Fix the issues above and re-run: ./scripts/docker-up.sh"
    exit 1
  fi
}

# ── 命令处理 ──────────────────────────────────────────────────

case "${1:-}" in
  --down)
    echo "Stopping and removing containers..."
    docker compose down
    echo "Done. Data in ./data and ./workspaces is preserved."
    exit 0
    ;;
  --logs)
    docker compose logs -f --tail=100
    exit 0
    ;;
  --build)
    echo "Rebuilding all images..."
    docker compose build --no-cache
    echo "Starting..."
    docker compose up -d
    ;;
  --status)
    docker compose ps
    echo ""
    echo "API:    http://localhost:8000"
    echo "Studio: http://localhost:3000"
    echo "Swagger: http://localhost:8000/docs"
    echo "Debug:   http://localhost:8000/debug"
    exit 0
    ;;
  "")
    check_prereqs
    echo "Building and starting Lilies..."
    echo "(First run may take 3-5 minutes to build images)"
    echo ""
    docker compose up -d --build

    echo ""
    echo "Waiting for API to be ready..."
    for i in $(seq 1 30); do
      if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Lilies is running!"
        echo ""
        echo "  API:      http://localhost:8000"
        echo "  Studio:   http://localhost:3000"
        echo "  Swagger:  http://localhost:8000/docs"
        echo "  Debug:    http://localhost:8000/debug"
        echo ""
        echo "  Status:   ./scripts/docker-up.sh --status"
        echo "  Logs:     ./scripts/docker-up.sh --logs"
        echo "  Stop:     ./scripts/docker-up.sh --down"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        exit 0
      fi
      sleep 2
      echo -n "."
    done
    echo ""
    echo "⚠️  API did not become ready in 60s. Check logs:"
    echo "   ./scripts/docker-up.sh --logs"
    exit 1
    ;;
  *)
    echo "Usage: $0 [--build|--down|--logs|--status]"
    echo ""
    echo "  (no args)   Build and start all services"
    echo "  --build     Force rebuild images"
    echo "  --down      Stop and remove containers"
    echo "  --logs      View real-time logs"
    echo "  --status    Show container status and URLs"
    exit 1
    ;;
esac
