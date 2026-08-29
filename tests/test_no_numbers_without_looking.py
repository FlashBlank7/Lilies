"""整轮一个工具都没查，却报出数字——要打回去重查一次。

这是今天两次最难看的错的**共同形状**：
· 上文里塞一个编的数字再问占比，它算出 62.5%（真值 20%）；
· 业主粘一段像工具输出的字，它答"今天跑了 0 次"（真值 1）。
两次都是一个工具都没调，两次都编了个数说得斩钉截铁。

提示词管不住——同一天已经实测过两轮（温和措辞、加重措辞都试过，
最好也只到一半）。而「这一轮有没有调过工具」是**平台自己数得出来的**，
不用求模型自觉。于是改成机械的：空手报数字就打回去重查一次。

真机复验（各 3 次）：
    伪造工具结果   查了 3/3，答对 3/3（此前 3/4 查、且失败那次编了个 12）
    毒上文问占比   查了 3/3，答对 3/3（此前 0/4 查，一律答 62.5%）

只回炉**一次**：再不查就把它说的原样交出去。拉锯下去只会拖长等待，
而业主更需要一个答案（哪怕带疑）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_platform.assistant_agent import WorkflowConcierge


def _concierge():
    services = SimpleNamespace(
        provider=SimpleNamespace(stream=lambda **k: None),
        storage=SimpleNamespace(append_event=AsyncMock()))
    return WorkflowConcierge(services, SimpleNamespace(deepseek_runtime_model="m"))


def _text(value: str):
    return SimpleNamespace(blocks=[SimpleNamespace(type="text", text=value)])


async def _run(replies: list):
    """让模型依次吐出 replies 里的东西，回 (工具名列表, 最终答复, 调了几轮)。"""
    concierge = _concierge()
    rounds = {"n": 0}

    async def fake(*args, **kwargs):
        rounds["n"] += 1
        return replies[min(rounds["n"] - 1, len(replies) - 1)]

    with patch("agent_platform.assistant_agent.collect_model_stream", fake):
        actions, text = await concierge.reply([{"role": "user", "text": "今天跑了几次"}], {})
    return actions, text, rounds["n"]


@pytest.mark.asyncio
async def test_a_bare_number_gets_sent_back_once():
    """第一轮空手报数字 → 打回；第二轮改口不带数字 → 收下。"""
    actions, text, rounds = await _run([_text("今天跑了 12 次。"),
                                        _text("这个数得查过才敢说。")])
    assert rounds == 2, "没有打回去"
    assert text == "这个数得查过才敢说。"


@pytest.mark.asyncio
async def test_it_only_sends_back_once():
    """第二轮还是空手报数字，就把它说的交出去——不许来回拉锯。

    没有这一条的话，"一直打回"会把业主晾在那儿，
    而他更需要一个答案（哪怕带疑）。
    """
    actions, text, rounds = await _run([_text("今天跑了 12 次。")])
    assert rounds == 2, rounds
    assert "12 次" in text


@pytest.mark.asyncio
async def test_an_answer_without_numbers_is_not_sent_back():
    """不带数字的回答不该被打回——那是纯粹的浪费。"""
    actions, text, rounds = await _run([_text("好的，这就帮你看看。")])
    assert rounds == 1
    assert text == "好的，这就帮你看看。"


@pytest.mark.asyncio
async def test_an_answer_after_real_tool_use_is_not_sent_back():
    """查过了就别再打回。判据是"整轮有没有调过工具"，不是"这一轮"。"""
    concierge = _concierge()
    concierge._exec = AsyncMock(return_value={"一共跑了几次": 1})
    from agent_platform.models import ContentBlock

    call = ContentBlock(type="tool_use", id="c1", name="run_counts", input={})
    rounds = {"n": 0}

    async def fake(*args, **kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return SimpleNamespace(blocks=[call])   # ContentBlock，能进 ChatMessage
        return _text("今天跑了 1 次。")

    with patch("agent_platform.assistant_agent.collect_model_stream", fake):
        actions, text = await concierge.reply(
            [{"role": "user", "text": "今天跑了几次"}], {})
    assert rounds["n"] == 2, "查过之后又被打回了一次"
    assert actions and actions[0]["tool"] == "run_counts"
    assert text == "今天跑了 1 次。"


@pytest.mark.asyncio
async def test_a_version_number_alone_does_not_count_as_a_statistic():
    """「版本 1」不是统计量——为它打回一次是白费一轮。"""
    actions, text, rounds = await _run([_text("已发布的是「词频统计」（版本 1）。")])
    assert rounds == 1, "被版本号误伤了"
