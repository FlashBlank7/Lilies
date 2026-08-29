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
