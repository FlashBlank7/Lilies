"""内部代号不能出现在用户看得见的地方。

实测（2026-08-28 走招牌动线）：客户端清干净了，平台侧没清——
管家答了一句「莉莉丝正在后台处理中」，用户完全不知道那是谁。
源头没清等于白做，所以在平台侧设一道门。

注释与文档字符串里保留没关系，那是给维护者看的。
"""

from __future__ import annotations

import ast
from pathlib import Path

CODENAMES = ("莉莉丝",)
BASE = Path("platform/backend/src/agent_platform")


def _docstrings(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = ast.get_docstring(node, clean=False)
            if text:
                found.add(text)
    return found


def test_no_codename_in_user_facing_strings() -> None:
    leaks = []
    for path in sorted(BASE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in CODENAMES):
            continue
        tree = ast.parse(source)
        docstrings = _docstrings(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.value in docstrings:
                continue          # 文档字符串给维护者看，不外流
            if any(name in node.value for name in CODENAMES):
                leaks.append(f"{path.name}:{node.lineno} {node.value[:60]}")
    assert not leaks, (
        "这些字符串会流到用户面前：\n  " + "\n  ".join(leaks)
        + "\n用描述性说法（构建智能体 / 搭建方 / 构建方），别造代号。")


def test_the_check_can_actually_see_strings() -> None:
    """守门测试自己要有鉴别力：确认它能在字符串里找到东西。"""
    tree = ast.parse('x = "莉莉丝在这里"\n')
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "莉莉丝" in n.value and n.value not in _docstrings(tree)]
    assert len(hits) == 1
