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

if [[ "${1:-}" == "--check-env" ]]; then
  [[ -n "${DEEPSEEK_API_KEY:-}" ]] && echo "DEEPSEEK_API_KEY ok" || echo "DEEPSEEK_API_KEY missing"
  [[ -n "${API_TOKEN:-}" ]] && echo "API_TOKEN ok" || echo "API_TOKEN missing"
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

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo ".venv is missing. Run: uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -d platform/frontend/node_modules ]]; then
  echo "platform/frontend/node_modules is missing. Run: cd platform/frontend && npm install" >&2
  exit 1
fi

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
.venv/bin/uvicorn agent_platform.api:app --host "$API_HOST" --port "$API_PORT" &

echo "Starting Studio on http://$WEB_HOST:$WEB_PORT"
(
  cd platform/frontend
  AGENT_PLATFORM_URL="http://$API_HOST:$API_PORT" API_TOKEN="$API_TOKEN" npm run dev -- --hostname "$WEB_HOST" --port "$WEB_PORT"
) &

wait
