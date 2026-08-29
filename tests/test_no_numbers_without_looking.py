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


# ── 被作废的那一轮，一个字都不能流到业主屏幕上 ──


async def _run_streaming(replies: list):
    """跟 _run 一样，但接上 emit，把发出去的事件都记下来。"""
    concierge = _concierge()
    rounds = {"n": 0}
    events: list[dict] = []

    async def fake(*args, **kwargs):
        rounds["n"] += 1
        reply = replies[min(rounds["n"] - 1, len(replies) - 1)]
        # 模拟真流：把整段正文当成一次 delta 推给 forward
        forward = kwargs.get("emit")
        if forward is not None:
            for block in reply.blocks:
                if block.type == "text":
                    await forward("content.text.delta", {"text": block.text})
        return reply

    async def collect(event: dict) -> None:
        events.append(event)

    with patch("agent_platform.assistant_agent.collect_model_stream", fake):
        actions, text = await concierge.reply(
            [{"role": "user", "text": "今天跑了几次"}], {}, emit=collect)
    return events, text, rounds["n"]


@pytest.mark.asyncio
async def test_the_discarded_round_never_reaches_the_screen():
    """打出去的字收不回来。

    第一版把「补发攒着的字」写在了「判要不要打回」**前面**，
    于是被作废的那一轮照样流了出去——业主先看到一个错数字、
    再看到订正。这比让他多等半秒糟得多。
    """
    events, text, rounds = await _run_streaming(
        [_text("今天跑了 12 次。"), _text("查过了，今天跑了 1 次。")])
    assert rounds == 2
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "12 次" not in streamed, f"作废的那一轮流出去了：{streamed}"
    assert "1 次" in streamed
    assert "12 次" not in text


@pytest.mark.asyncio
async def test_the_kept_round_does_reach_the_screen():
    """别为了保险把该发的也扣住——不打回时字要照常流出去。"""
    events, text, _ = await _run_streaming([_text("好的，这就帮你看看。")])
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "这就帮你看看" in streamed


@pytest.mark.asyncio
async def test_the_final_event_matches_what_was_streamed():
    """流出去的和 final 说的必须是同一件事。

    客户端只在"一个字都没流过"时才用 final；两者不一致时，
    用户看到的取决于他用的是哪个客户端——那是最难查的一类不一致。
    """
    events, text, _ = await _run_streaming(
        [_text("今天跑了 12 次。"), _text("查过了，今天跑了 1 次。")])
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    final = next(e["text"] for e in events if e["type"] == "final")
    assert streamed.replace(" ", "") == final.replace(" ", "")
    assert final == text


@pytest.mark.asyncio
async def test_text_said_alongside_a_tool_call_is_not_swallowed():
    """「边说一句边去查」那种回答的前半截不能被吞掉。

    加缓冲时漏的正是这一格：有工具调用的那一轮不走 `if not calls` 分支，
    攒着的正文就永远没人补发。拿"正文 + 工具调用同轮"试了一次才发现。
    （这一句在缓冲之前是会流出去的，所以是我自己造的回归。）
    """
    from agent_platform.models import ContentBlock

    concierge = _concierge()
    concierge._exec = AsyncMock(return_value={"一共跑了几次": 1})
    rounds = {"n": 0}
    events: list[dict] = []

    async def fake(*args, **kwargs):
        rounds["n"] += 1
        forward = kwargs.get("emit")
        if rounds["n"] == 1:
            if forward:
                await forward("content.text.delta", {"text": "先说一句有用的话。"})
            return SimpleNamespace(blocks=[
                ContentBlock(type="text", text="先说一句有用的话。"),
                ContentBlock(type="tool_use", id="c1", name="run_counts", input={})])
        if forward:
            await forward("content.text.delta", {"text": "今天跑了 1 次。"})
        return SimpleNamespace(blocks=[ContentBlock(type="text", text="今天跑了 1 次。")])

    async def collect(event: dict) -> None:
        events.append(event)

    with patch("agent_platform.assistant_agent.collect_model_stream", fake):
        await concierge.reply([{"role": "user", "text": "今天跑了几次"}], {},
                              emit=collect)
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "先说一句有用的话" in streamed, streamed
    assert "今天跑了 1 次" in streamed

