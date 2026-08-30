#!/usr/bin/env bash
# 提交前的门链。存在的理由是一次次重复的同一个错：
#
#     pytest -q | tail -2 && git commit ...
#
# `&&` 判的是 `tail` 的退出码，不是 pytest 的。测试红着照样提交，
# 而且屏幕上就摆着失败输出——看见了也没挡住。写过 memory 提醒自己，
# 之后仍然犯了第三次。所以不再靠记性：跑这个脚本，红了它自己退非零。
#
#   bash scripts/gate.sh            平台：ruff F 类 + 前端 tsc/vitest + 全量 pytest
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
  LINT_PATHS=(guanjia scripts tests)
  LINT_FLOOR=20
else
  SRC="$ROOT"
  # scripts 也要扫：里面是冒烟、准确性核对、跨端点对账这些**用来验别人**的东西，
  # 它们自己坏了最没人发现。2026-08-29 加对账脚本时才注意到这一格一直空着。
  # tests 暂时不加：pytest 夹具的导入再同名接参会被 F811 大面积误报（97 处），
  # 真要加得先把那批 noqa 补齐，那是另一件事。
  LINT_PATHS=(platform/backend/src scripts)
  LINT_FLOOR=60
fi

cd "$SRC"

echo "── ruff（F 类：算了没用的变量、没引用的导入、假 f-string）──"
# 先数一遍到底要扫几个文件，再真扫。
#
# 为什么多这一步：路径写错时 ruff **只是警告，退出码照样是 0**。
# 2026-08-29 把路径从一个扩到两个时写成了 "$LINT_PATH" 一个带空格的整串，
# 于是每次门链都印着
#     warning: Failed to lint platform/backend/src scripts: No such file or directory
#     All checks passed!
# ——一个文件都没扫，却报"全部通过"，而且绿了好几次提交都没人看那行 warning。
# 门链是最后一道闸，它自己空转是最不该发生的一种坏法。
# 所以这里不信 ruff 的退出码，先要它把打算扫的文件列出来自己数：
# 数目为零、或少得离谱（目录被移走、被 exclude 规则吞掉），当场退非零。
lint_files="$("$RUFF" check "${LINT_PATHS[@]}" --select F --show-files | wc -l)"
if [ "$lint_files" -lt "$LINT_FLOOR" ]; then
  echo "✕ ruff 只打算扫 $lint_files 个文件（至少该有 $LINT_FLOOR 个）" >&2
  echo "  多半是 ${LINT_PATHS[*]} 里有路径不存在或被 exclude 吞了。" >&2
  echo "  注意 ruff 遇到不存在的路径只 warning、退出码仍是 0，别信那句 All checks passed。" >&2
  exit 1
fi
"$RUFF" check "${LINT_PATHS[@]}" --select F
echo "   （扫了 $lint_files 个文件）"

# 前端：36 个 TS 文件此前**一条都没进过门链**。
# vitest 的 9 条测试和 `tsc --noEmit` 的类型检查都在 package.json 里躺着，
# 谁也没跑过——2026-08-30 第一次跑：全绿，加起来 3 秒。
# 没进门链多半是因为 node 不在 PATH 上（这台机器装在 ~/.local/node）。
#
# 找不到 node 时**不静默放行**：这个门链自己栽过一次
# "扫了 0 个文件还报全绿"，教训是"一个都没找到"在多数工具里默认算成功。
# 所以跳过要写进最后那行结论里——只印一句 warning 是没用的，
# 上次绿着提交了好几回也没人看那行。
front_note=""
FRONT="$ROOT/platform/frontend"
if [ "$target" != "bench" ] && [ -d "$FRONT/node_modules" ]; then
  echo "── 前端（tsc + vitest）──"
  NODE_BIN="$(command -v node || true)"
  [ -n "$NODE_BIN" ] || NODE_BIN="$HOME/.local/node/bin/node"
  if [ -x "$NODE_BIN" ]; then
    export PATH="$(dirname "$NODE_BIN"):$PATH"
    ( cd "$FRONT" && ./node_modules/.bin/tsc --noEmit )
    ( cd "$FRONT" && ./node_modules/.bin/vitest run --reporter=dot )
  else
    front_note="；前端跳过：找不到 node"
    echo "！前端没跑：找不到 node（装上就会自动跑）" >&2
  fi
elif [ "$target" != "bench" ]; then
  front_note="；前端跳过：没有 node_modules（先 npm install）"
  echo "！前端没跑：$FRONT/node_modules 不在" >&2
fi

echo "── pytest ──"
# 不用字节码缓存。2026-08-29 被它骗过一次：改代码、跑测试、还原，
# 三步都在同一秒内完成时，__pycache__ 里那份 .pyc 会被当成有效的，
# 于是**跑的是磁盘上已经不存在的代码**——当时表现为
# "边界检查明明在 mkdir 前面，目录却还是被建出来了"，
# 查了半天才发现是缓存。
# 门链是挡住坏提交的最后一道，它绝不能跑一份"曾经的代码"。
# 实测代价：整套 1269 条，3m52 vs 3m54——在噪声里。
export PYTHONDONTWRITEBYTECODE=1
# 不加 tail：截断输出正是那个错的温床。要看少一点就自己 > 文件再 grep。
#
# 条数记下来，最后那行一起报。**这是为了掐掉一个反复出现的诱因**：
# 想在提交信息里写准"N 条绿"，就会忍不住写成
#     bash scripts/gate.sh | tail -2 && git commit
# 而 `&&` 判的是 tail 的退出码——正是本文件开头那个错。
# 2026-08-30 我就这么闯了一次红灯（漏进去一条 ruff F401）。
# 条数直接印在结论里，不接管道也看得见，就没有理由再接了。
pytest_log="$(mktemp)"
trap 'rm -f "$pytest_log"' EXIT
"$PY" -m pytest tests/ -q -p no:cacheprovider 2>&1 | tee "$pytest_log"
# pipefail 已经开着：pytest 红了这里就退非零，tee 顶不掉它
count="$(grep -Eo '[0-9]+ passed' "$pytest_log" | tail -1 || true)"

echo "✓ 门链全绿（$target$front_note）${count:+ · $count}"
