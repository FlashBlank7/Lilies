"""管家交给模型的载荷里不许出现内部状态码。

今天这个洞被撞见了四次，每次只修了眼前那一个出口：
  build_status → recent_builds → run_workflow → resume_build
提示词里禁止机器词汇是拦不住的：模型手里有什么词就说什么词。
所以把「所有出口」这件事本身变成一道门——
逐条扫 _exec 的 return，而不是等下一次在真机上撞见。

翻译后的键叫「情况」，值来自 _RUN_WORDS / _build_situation / _HEALTH_WORDS。
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path("platform/backend/src/agent_platform/assistant_agent.py")

# 这些键一旦出现在给模型的返回里，值就是内部状态码或内部计数。
# published_version 不在列：那是个版本号，业主看「已发布第 3 版」是有意义的——
# 门槛太宽会制造假警报，假警报多了这道门就没人当真了。
BANNED_KEYS = {"status", "revision", "state"}


def _exec_returns() -> list[tuple[int, str]]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_exec"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.append((node.lineno, key.value))
    return found


def test_no_internal_status_keys_reach_the_model() -> None:
    leaks = [(line, key) for line, key in _exec_returns() if key in BANNED_KEYS]
    assert not leaks, (
        "这些出口把内部状态码直接递给了模型，模型会原样念给业主：\n"
        + "\n".join(f"  {SOURCE.name}:{line} 的 {key!r}" for line, key in leaks)
        + "\n翻成人话放进「情况」，另配一句「接下来」。")


def test_the_guard_actually_scans_something() -> None:
    """别让扫描器因为找不到 _exec 而空跑通过。"""
    returns = _exec_returns()
    assert len(returns) > 20, f"只扫到 {len(returns)} 个返回键，扫描器多半失效了"
    assert any(key == "情况" for _, key in returns), "没扫到翻译后的「情况」键"
