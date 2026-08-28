"""出卷要看得见工作流真实的接口，否则监理只能猜字段名。

实测（2026-08-28 走监理动线）：工作流输出 line_count/char_count，
监理凭业主口语编出 lines/chars，于是**一个好工作流被判不合格**——
这比不验收更糟，因为它会把人引去「修」一个没坏的东西。
"""

from __future__ import annotations

from agent_platform.acceptance_pm import _io_contract
from agent_platform.workflow_models import ApplicationSnapshot


def _snapshot(nodes):
    return ApplicationSnapshot.model_validate(
        {"name": "x", "workflow": {"nodes": nodes, "edges": []}})


def test_contract_lists_declared_fields():
    snapshot = _snapshot([
        {"id": "s", "type": "start", "title": "开始", "config": {"inputs": [
            {"name": "text", "type": "string"},
            {"name": "month", "type": "array"}]}},
        {"id": "e", "type": "end", "title": "结束", "config": {"outputs": {
            "line_count": {"$ref": "x"}, "char_count": {"$ref": "y"}}}},
    ])
    text = _io_contract(snapshot)
    assert "text（string）" in text
    assert "month（array）" in text
    assert "line_count" in text and "char_count" in text
    assert "不要自己另起" in text        # 说清这是硬约束，不是参考


def test_contract_is_empty_when_nothing_declared():
    """没有声明就别塞一段空话进提示词。"""
    assert _io_contract(_snapshot([{"id": "s", "type": "start", "title": "开始",
                                       "config": {}}])) == ""


def test_contract_survives_broken_snapshot():
    """拿不到就返回空，别拖垮出卷本身。"""
    class Broken:
        @property
        def workflow(self):
            raise RuntimeError("坏了")

    assert _io_contract(Broken()) == ""


def test_spec_prompt_includes_the_contract():
    """契约必须真的拼进提示词——加了函数却没用上是最容易犯的错。"""
    from pathlib import Path

    source = Path("platform/backend/src/agent_platform/acceptance_pm.py").read_text(
        encoding="utf-8")
    start = source.index("async def generate_spec")
    body = source[start:start + 2000]
    assert "_io_contract" in body
    assert "{contract}" in body
