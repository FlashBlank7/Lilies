#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8001}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-3000}"

cd "$ROOT"

load_env_file() {
  local file="$1"
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
      if [[ "$value" == \"*\" && "$value" == *\" ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
        value="${value:1:${#value}-2}"
      fi
      export "$key=$value"
    fi
  done < "$file"
}

if [[ -f .env ]]; then
  load_env_file .env
fi

API_CONNECT_HOST="$API_HOST"
if [[ "$API_CONNECT_HOST" == "0.0.0.0" || "$API_CONNECT_HOST" == "::" ]]; then
  API_CONNECT_HOST="127.0.0.1"
fi
API_CONNECT_AUTHORITY="$API_CONNECT_HOST"
if [[ "$API_CONNECT_AUTHORITY" == *:* && "$API_CONNECT_AUTHORITY" != \[*\] ]]; then
  API_CONNECT_AUTHORITY="[$API_CONNECT_AUTHORITY]"
fi
API_CONNECT_URL="http://$API_CONNECT_AUTHORITY:$API_PORT"
export LILIES_PLATFORM_BASE_URL="${LILIES_PLATFORM_BASE_URL:-$API_CONNECT_URL}"

if [[ "${1:-}" == "--check-env" ]]; then
  [[ -n "${DEEPSEEK_API_KEY:-}" ]] && echo "DEEPSEEK_API_KEY ok" || echo "DEEPSEEK_API_KEY missing"
  [[ -n "${API_TOKEN:-}" ]] && echo "API_TOKEN ok" || echo "API_TOKEN missing"
  if [[ "${LILIES_LOCAL_AGENT_ENABLED:-false}" == "true" ]]; then
    echo "Local Lilies callback $LILIES_PLATFORM_BASE_URL"
  fi
  if [[ "${LILIES_COLLABORATION_ENABLED:-false}" == "true" ]]; then
    collaboration_developer_token="${LILIES_COLLABORATION_DEVELOPER_TOKEN:-}"
    collaboration_verifier_token="${LILIES_COLLABORATION_VERIFIER_TOKEN:-}"
    [[ ${#collaboration_developer_token} -ge 32 ]] \
      && echo "LILIES_COLLABORATION_DEVELOPER_TOKEN ok" \
      || echo "LILIES_COLLABORATION_DEVELOPER_TOKEN missing/short"
    [[ ${#collaboration_verifier_token} -ge 32 ]] \
      && echo "LILIES_COLLABORATION_VERIFIER_TOKEN ok" \
      || echo "LILIES_COLLABORATION_VERIFIER_TOKEN missing/short"
  fi
  exit 0
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is missing. Edit $ROOT/.env before starting." >&2
  echo "Run ./scripts/dev_platform.sh --check-env to verify dotenv loading." >&2
  exit 1
fi

if [[ -z "${API_TOKEN:-}" ]]; then
  echo "API_TOKEN is missing. Edit $ROOT/.env before starting." >&2
  echo "Run ./scripts/dev_platform.sh --check-env to verify dotenv loading." >&2
  exit 1
fi

if [[ "${LILIES_COLLABORATION_ENABLED:-false}" == "true" ]]; then
  collaboration_developer_token="${LILIES_COLLABORATION_DEVELOPER_TOKEN:-}"
  collaboration_verifier_token="${LILIES_COLLABORATION_VERIFIER_TOKEN:-}"
  if [[ ${#collaboration_developer_token} -lt 32 ]]; then
    echo "LILIES_COLLABORATION_DEVELOPER_TOKEN must be at least 32 characters." >&2
    exit 1
  fi
  if [[ ${#collaboration_verifier_token} -lt 32 ]]; then
    echo "LILIES_COLLABORATION_VERIFIER_TOKEN must be at least 32 characters." >&2
    exit 1
  fi
  if [[ "$collaboration_developer_token" == "$API_TOKEN" \
     || "$collaboration_verifier_token" == "$API_TOKEN" \
     || "$collaboration_developer_token" == "$collaboration_verifier_token" ]]; then
    echo "Collaboration user, developer, and verifier credentials must be distinct." >&2
    exit 1
  fi
fi

ensure_node_tools() {
  if command -v npm &>/dev/null; then
    return 0
  fi

  if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
    # shellcheck source=/dev/null
    source "$HOME/.nvm/nvm.sh"
  fi

  if ! command -v npm &>/dev/null; then
    echo "npm is missing. Install Node.js 20+ or load your Node version manager before starting." >&2
    echo "If you use nvm, run: source ~/.nvm/nvm.sh && nvm use" >&2
    exit 1
  fi
}

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo ".venv is missing. Run: uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'" >&2
  exit 1
fi

ensure_node_tools

if [[ ! -d platform/frontend/node_modules ]]; then
  echo "platform/frontend/node_modules is missing. Run: cd platform/frontend && npm install" >&2
  exit 1
fi

# ── Docker checks ─────────────────────────────────────────────

check_docker_cli() {
  if ! command -v docker &>/dev/null; then
    echo "Docker is not installed. Install Docker Engine or Docker Desktop first:" >&2
    echo "  Linux:   https://docs.docker.com/engine/install/" >&2
    echo "  macOS:   https://docs.docker.com/desktop/setup/install/mac-install/" >&2
    echo "  Windows: https://docs.docker.com/desktop/setup/install/windows-install/" >&2
    return 1
  fi
}

check_docker_daemon() {
  if ! docker info &>/dev/null; then
    echo "Docker daemon is not running. Start it first:" >&2
    echo "  Linux:   sudo systemctl start docker" >&2
    echo "  macOS:   open -a Docker" >&2
    echo "  Windows: Start Docker Desktop" >&2
    return 1
  fi
}

check_docker_permission() {
  if ! docker ps &>/dev/null; then
    echo "Cannot access Docker. Add your user to the docker group:" >&2
    echo "  sudo usermod -aG docker \$USER" >&2
    echo "Then log out and back in, or run: newgrp docker" >&2
    return 1
  fi
}

check_sandbox_image() {
  local image="${SANDBOX_IMAGE:-agent-platform-sandbox:latest}"
  if ! docker image inspect "$image" &>/dev/null; then
    echo "Sandbox image '$image' not found. Building it now..."
    local uid="${SANDBOX_UID:-10001}"
    local gid="${SANDBOX_GID:-10001}"
    if [[ -f "$ROOT/Dockerfile.sandbox" ]]; then
      docker build \
        --build-arg SANDBOX_UID="$uid" \
        --build-arg SANDBOX_GID="$gid" \
        -t "$image" \
        -f "$ROOT/Dockerfile.sandbox" \
        "$ROOT" || {
          echo "Failed to build sandbox image. Check Dockerfile.sandbox and try again." >&2
          return 1
        }
      echo "Sandbox image '$image' built successfully."
    else
      echo "Dockerfile.sandbox not found at $ROOT/Dockerfile.sandbox" >&2
      return 1
    fi
  fi
}

echo "Checking Docker..."
check_docker_cli || exit 1
check_docker_daemon || exit 1
check_docker_permission || exit 1
check_sandbox_image || exit 1
echo "Docker ready."

# ── Port checks ───────────────────────────────────────────────
check_port() {
  local port="$1"
  local label="$2"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$label port $port is already in use by PID(s): $pids" >&2
    echo "Stop the stale process first, for example: kill $pids" >&2
    exit 1
  fi
}

check_port "$API_PORT" "API"
check_port "$WEB_PORT" "Web"

cleanup() {
  local pids
  pids="$(jobs -pr)"
  if [[ -n "$pids" ]]; then
    kill $pids 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting API on http://$API_HOST:$API_PORT"
if [[ "${LILIES_LOCAL_AGENT_ENABLED:-false}" == "true" ]]; then
  echo "Local Lilies callback: $LILIES_PLATFORM_BASE_URL"
fi
.venv/bin/uvicorn agent_platform.api:app --host "$API_HOST" --port "$API_PORT" &

echo "Starting Studio on http://$WEB_HOST:$WEB_PORT"
echo "Studio proxy target: $API_CONNECT_URL"
(
  cd platform/frontend
  AGENT_PLATFORM_URL="$API_CONNECT_URL" API_TOKEN="$API_TOKEN" npm run dev -- --hostname "$WEB_HOST" --port "$WEB_PORT"
) &

wait
