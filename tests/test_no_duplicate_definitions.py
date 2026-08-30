"""同一个模块里不许把同一个名字定义两遍。

来由：mechanical_builder.py 里 `_accepts_temperature` **定义了四遍**，
四份一字不差。Python 只留最后一份，前三份是死代码——
读代码的人会以为它们各有各的用处，改错一份还查不出来。
（多半是那次文件被覆盖后恢复留下的痕迹。）

ruff 的 F 类不管这个：F811 只在**重定义前没被用过**时才报，
模块顶层这种"定义完就再定义一遍"的形状它不出声（实测这个文件当时是绿的）。
所以专门钉一条。

顺带说清没管的两件事：
· 类里的方法重定义也一起扫了（当时全仓没有）。
· 函数**内部**的嵌套重定义不扫——那有正当用法
  （mechanical_builder 里的 propose 就是有意重定义的，还带着
  `# type: ignore[no-redef]` 标注）。
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# tests/ 和 scripts/ 一起扫。**重复的测试比重复的生产代码更坏**：
# 同名的第二个 def 会把第一个顶掉，第一份一次都不跑，
# 而它看起来是有覆盖的。真机上就有三处
# （test_builder_live_progress 一处、test_openai_chat_provider 两处，
#  三处都是一字不差的复制）。
MODULES = sorted(
    [p for p in (ROOT / "platform/backend/src").rglob("*.py")]
    + [p for p in (ROOT / "tests").rglob("*.py")]
    + [p for p in (ROOT / "scripts").rglob("*.py")]
)


def test_there_are_modules_to_check():
    """先钉住有东西可扫——空列表会让下面那条一路全绿却什么都没查。"""
    assert len(MODULES) > 100, len(MODULES)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_name_is_defined_twice_at_module_level(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tops = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    repeated = {name: n for name, n in Counter(x.name for x in tops).items() if n > 1}
    assert not repeated, f"{path.name} 里重复定义：{repeated}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_method_is_defined_twice_in_a_class(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [n.name for n in node.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        repeated = {name: n for name, n in Counter(methods).items() if n > 1}
        assert not repeated, f"{path.name}::{node.name} 里重复定义：{repeated}"


# —— 同一个毛病的另一种形状：**相邻语句**被复制粘贴 ——
#
# 2026-08-30 挖出四处，其中一处是真的坏了：拼引用解析报错的那段里，
# "该修哪一端"整块连着写了两遍，于是每条这类报错都把同一句话说两遍
# （真机上那是第二大的失败族）。上面两条只查 def 的名字，查不到这个。
#
# 但**不能一刀切**：相邻重复语句有正当用法——`await socket.send(x)`
# 连发两遍是在测去重（本仓就有一条），`next(it)` 连叫两次是跳两项。
# 所以只钉两种"重复了必定是错"的形状：
#   · 相邻两个一模一样的 if，且体内以 raise/return 收尾 → 第二个永远到不了
#   · 相邻两句一模一样的赋值，且右边是字面量 → 第二句纯属多余
def _statement_blocks(tree: ast.AST):
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and len(block) > 1:
                yield block


def _dead_if_repeat(first: ast.stmt, second: ast.stmt) -> bool:
    if not (isinstance(first, ast.If) and isinstance(second, ast.If)):
        return False
    if ast.dump(first) != ast.dump(second):
        return False
    return isinstance(first.body[-1], (ast.Raise, ast.Return))


def _redundant_literal_assignment(first: ast.stmt, second: ast.stmt) -> bool:
    if not isinstance(first, (ast.Assign, ast.AnnAssign)):
        return False
    if ast.dump(first) != ast.dump(second):
        return False
    return isinstance(first.value, (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_adjacent_statement_is_copy_pasted(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for block in _statement_blocks(tree):
        for first, second in zip(block, block[1:]):
            if _dead_if_repeat(first, second) or _redundant_literal_assignment(first, second):
                found.append(f"第 {first.lineno} 行又抄了一遍到第 {second.lineno} 行")
    assert not found, f"{path.name}：{found}"


def test_the_check_can_actually_see_a_duplicate():
    """扫描器自己得抓得住——上面那条对每个文件都断言"没有"，
    扫描器写坏成"永远返回空"的话，全仓一路绿。"""
    dead_if = ast.parse("def f(x):\n"
                        "    if x:\n        raise ValueError('a')\n"
                        "    if x:\n        raise ValueError('a')\n")
    blocks = list(_statement_blocks(dead_if))
    assert any(_dead_if_repeat(a, b)
               for block in blocks for a, b in zip(block, block[1:]))

    twice = ast.parse("def f():\n    x = {}\n    x = {}\n")
    blocks = list(_statement_blocks(twice))
    assert any(_redundant_literal_assignment(a, b)
               for block in blocks for a, b in zip(block, block[1:]))


def test_the_check_does_not_flag_the_legitimate_repeats():
    """反向：正当的重复不能报——不然这条会逼着人把对的代码改坏。"""
    sending = ast.parse("async def f(s, e):\n    await s.send(e)\n    await s.send(e)\n")
    for block in _statement_blocks(sending):
        for a, b in zip(block, block[1:]):
            assert not _dead_if_repeat(a, b)
            assert not _redundant_literal_assignment(a, b)

    # 体内不收尾于 raise/return 的相同 if：可能是有意跑两遍
    looping = ast.parse("def f(x):\n    if x:\n        x -= 1\n    if x:\n        x -= 1\n")
    for block in _statement_blocks(looping):
        for a, b in zip(block, block[1:]):
            assert not _dead_if_repeat(a, b)


# —— 第三种形状：**成组**被复制 ——
#
# 2026-08-30 在 builder.py 抓到：反刍守卫那四句（算签名、取计数、写回、
# 到 3 就警告）连着写了三遍，三遍都在同一个 except 里顺序跑。
# 后果不是死代码，是**算错**：一次被拒计数加 3，于是第一次被拒就被告知
# "第 3 次"，第二次起同一段警告连贴三遍——而模型正是靠这个数判断该不该换做法。
#
# 上面那条"相邻重复语句"抓不到它：重复的是四句一组，组与组之间隔着那个 if，
# 任何两条**相邻**语句都不相同。所以这里按"连续若干句整组重复"再扫一遍。
# 门槛定在 3 句：两句一组的重复偶尔是正当的（连发两次同样的事件），
# 三句一组一字不差地紧挨着出现，基本只可能是复制粘贴。
GROUP_MIN = 3


def _repeated_group(block: list) -> tuple[int, int] | None:
    dumped = [ast.dump(stmt) for stmt in block]
    for size in range(GROUP_MIN, len(dumped) // 2 + 1):
        for start in range(len(dumped) - 2 * size + 1):
            first = dumped[start:start + size]
            if first == dumped[start + size:start + 2 * size]:
                return block[start].lineno, block[start + size].lineno
    return None


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_group_of_statements_is_copy_pasted(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for block in _statement_blocks(tree):
        repeat = _repeated_group(block)
        if repeat:
            found.append(f"第 {repeat[0]} 行起的一组，在第 {repeat[1]} 行又来了一遍")
    assert not found, f"{path.name}：{found}"


def test_the_group_check_can_see_a_real_one():
    """扫描器自己得抓得住——它对每个文件都断言"没有"，写坏成永远返回 None 就全绿。"""
    body = "\n".join(["def f(x):"] + ["    a = 1", "    b = 2", "    c = 3"] * 2)
    tree = ast.parse(body)
    assert any(_repeated_group(block) for block in _statement_blocks(tree))


def test_the_group_check_does_not_flag_two_line_repeats():
    """两句一组的重复不报——连发两次同样的事件是正当写法，本仓就有一处。"""
    body = "async def f(s, e):\n    await s.send(e)\n    await s.wait()\n" \
           "    await s.send(e)\n    await s.wait()\n"
    tree = ast.parse(body)
    assert not any(_repeated_group(block) for block in _statement_blocks(tree))
