"""对话被截到最近 12 轮——**截了要说出来**。

今天反复出现的那个形状，这次截的是对话本身。

真机实测：第 1 轮里业主约定了一个内部代号，聊满 17 轮后再问，它答

    「乙丙丁」在工作流列表里并没有对应的工作流。我不太明白你指的是什么。

它没有编（这点是对的），但业主明明说过——他会以为管家在装傻。
正确的话是「更早的对话我这边看不到了，麻烦再说一次」，
而要说这句，它得先知道自己看的是一截。

补上之后同一问答变成：
    「抱歉，我这边看不到更早的 5 轮对话了，对「乙丙丁」这个约定没有印象。
      方便的话请再跟我说一下……」
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_platform.assistant_agent import WorkflowConcierge


async def _messages_sent(turns: int):
    """跑一轮，回"实际送进模型的那串消息"。"""
    seen: dict = {}

    def remember(**kwargs):
        # 消息是送给 provider.stream 的，不是 collect_model_stream
        seen["messages"] = kwargs["messages"]
        return None

    async def fake(*args, **kwargs):
        return SimpleNamespace(blocks=[SimpleNamespace(type="text", text="好的。")])

    concierge = WorkflowConcierge(
        SimpleNamespace(provider=SimpleNamespace(stream=remember),
                        storage=SimpleNamespace(append_event=AsyncMock())),
        SimpleNamespace(deepseek_runtime_model="m"))
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"第{i}句"}
               for i in range(turns)]
    with patch("agent_platform.assistant_agent.collect_model_stream", fake):
        await concierge.reply(history, {})
    return seen["messages"]


def _texts(messages) -> str:
    return " ".join(block.text for m in messages for block in m.content
                    if getattr(block, "text", None))


@pytest.mark.asyncio
async def test_a_short_conversation_gets_no_note():
    """没截就别提——多余的话会让真有事的时候没人看。"""
    sent = await _messages_sent(6)
    assert "看不到" not in _texts(sent)
    assert len(sent) == 6


@pytest.mark.asyncio
async def test_a_long_conversation_says_how_much_is_missing():
    sent = await _messages_sent(17)
    text = _texts(sent)
    assert "看不到" in text
    assert "5 轮" in text, text          # 17 - 12
    assert "12 轮" in text, text


@pytest.mark.asyncio
async def test_the_note_tells_it_what_to_say_instead():
    """光说"看不到"不够——要挡住那句「不明白你指的是什么」。

    业主明明说过的事，被回一句"不明白"，他只会觉得管家在装傻。
    """
    text = _texts(await _messages_sent(17))
    assert "再说一次" in text
    assert "不明白" in text          # 这三个字出现在"别这么说"里


@pytest.mark.asyncio
async def test_the_recent_turns_are_all_still_there():
    """别为了加提示把内容挤掉：12 轮正文一句不能少。"""
    sent = await _messages_sent(17)
    text = _texts(sent)
    for i in range(5, 17):
        assert f"第{i}句" in text, i


@pytest.mark.asyncio
async def test_the_note_is_first():
    """提示要在最前面。夹在中间的话，模型会当成对话的一部分。"""
    sent = await _messages_sent(17)
    assert "看不到" in sent[0].content[0].text


@pytest.mark.asyncio
async def test_the_note_never_reaches_the_user():
    from agent_platform.assistant_agent import _without_context_marks

    first = (await _messages_sent(17))[0].content[0].text
    assert _without_context_marks(first).strip() == ""
