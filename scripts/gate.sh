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
  LINT_PATH="guanjia scripts tests"
else
  SRC="$ROOT"
  # scripts 也要扫：里面是冒烟、准确性核对、跨端点对账这些**用来验别人**的东西，
  # 它们自己坏了最没人发现。2026-08-29 加对账脚本时才注意到这一格一直空着。
  # tests 暂时不加：pytest 夹具的导入再同名接参会被 F811 大面积误报（97 处），
  # 真要加得先把那批 noqa 补齐，那是另一件事。
  LINT_PATH="platform/backend/src scripts"
fi

cd "$SRC"

echo "── ruff（F 类：算了没用的变量、没引用的导入、假 f-string）──"
"$RUFF" check "$LINT_PATH" --select F

echo "── pytest ──"
# 不加 tail：截断输出正是那个错的温床。要看少一点就自己 > 文件再 grep。
"$PY" -m pytest tests/ -q -p no:cacheprovider

echo "✓ 门链全绿（$target）"
