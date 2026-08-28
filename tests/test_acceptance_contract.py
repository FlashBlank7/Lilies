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


def test_the_contract_actually_reaches_the_prompt(tmp_path, monkeypatch):
    """光测函数不够：generate_spec 真把它拼进提示词了吗？

    这条的由来是自查——上面几条只测 _io_contract 的产出，
    只要 generate_spec 里读版本的键名手滑一下，contract 就恒为空，
    而全套测试照样全绿，没有任何一条执行过 generate_spec。
    「断言源码里还有这个词」不等于「这个值真的到了下游」。
    """
    import asyncio
    from types import SimpleNamespace

    from agent_platform import acceptance_pm

    snapshot = _snapshot([
        {"id": "s", "type": "start", "title": "开始", "config": {"inputs": [
            {"name": "text", "type": "string"}]}},
        {"id": "e", "type": "end", "title": "结束",
         "config": {"outputs": {"line_count": {"$ref": "x"}}}},
    ])
    seen = {}

    async def fake_chat(services, application_id, system, prompt, phase, **kwargs):
        seen["prompt"] = prompt
        return ('{"summary": "查一查", "cases": [{"name": "c", '
                '"inputs": {"text": "a"}, "expect": {"equals": {"line_count": 1}}}]}')

    async def fake_get_version(application_id, *args, **kwargs):
        return {"snapshot": snapshot}

    monkeypatch.setattr(acceptance_pm, "_pm_chat", fake_chat)
    services = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        blocks=SimpleNamespace(list=lambda: []),
        workflow_store=SimpleNamespace(get_version=fake_get_version))

    asyncio.run(acceptance_pm.generate_spec(
        services, {"id": "a1", "requirement": "统计一段文字有几行几个字"},
        "比如给它 a\nb，应该说 2 行 3 个字"))

    prompt = seen.get("prompt", "")
    assert "text（string）" in prompt, "工作流声明的输入没进提示词"
    assert "line_count" in prompt, "工作流声明的输出没进提示词"
    assert "不要自己另起" in prompt, "那句硬约束没进提示词"
