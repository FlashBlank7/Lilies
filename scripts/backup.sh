#!/usr/bin/env bash
# 用法：停止平台后，scripts/backup.sh [数据目录] [备份目录] [工作区目录]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_args=()
if [[ -f .env ]]; then config_args=(--config .env); fi
"$ROOT/.venv/bin/python" -m agent_platform.backup create \
  --data "${1:-data}" --output "${2:-backups}" --workspaces "${3:-workspaces}" \
  "${config_args[@]}"
