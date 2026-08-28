#!/usr/bin/env bash
# 提交前的门链。存在的理由是一次次重复的同一个错：
#
#     pytest -q | tail -2 && git commit ...
#
# `&&` 判的是 `tail` 的退出码，不是 pytest 的。测试红着照样提交，
# 而且屏幕上就摆着失败输出——看见了也没挡住。写过 memory 提醒自己，
# 之后仍然犯了第三次。所以不再靠记性：跑这个脚本，红了它自己退非零。
#
#   bash scripts/gate.sh            平台：ruff F 类 + 全量 pytest
#   bash scripts/gate.sh bench      客户端：ruff F 类 + pytest
#
# 用法上唯一的纪律：`bash scripts/gate.sh && git commit ...`。
set -euo pipefail

target="${1:-platform}"
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"
RUFF="$ROOT/.venv/bin/ruff"

if [ "$target" = "bench" ]; then
  SRC="$HOME/code/bench"
  LINT_PATH="guanjia"
else
  SRC="$ROOT"
  LINT_PATH="platform/backend/src"
fi

cd "$SRC"

echo "── ruff（F 类：算了没用的变量、没引用的导入、假 f-string）──"
"$RUFF" check "$LINT_PATH" --select F

echo "── pytest ──"
# 不加 tail：截断输出正是那个错的温床。要看少一点就自己 > 文件再 grep。
"$PY" -m pytest tests/ -q -p no:cacheprovider

echo "✓ 门链全绿（$target）"
