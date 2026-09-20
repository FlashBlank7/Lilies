import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_platform.connected_model import completion_events
from agent_platform.model_session import ModelSession


async def event(*args):
    pass


@pytest.mark.asyncio
async def test_request_budget_survives_background_turns_and_preserves_results(tmp_path):
    seen, writes = [], []

    async def stream(**kwargs):
        seen.append(kwargs['max_output_tokens'])
        for item in completion_events([{'type': 'tool_use', 'id': str(len(seen)),
                'name': 'write', 'input': {}}], stop_reason='tool_use'):
            yield item

    async def tool(*args):
        writes.append(len(writes) + 1)
        return {'written': writes[-1]}

    session = ModelSession(SimpleNamespace(stream=stream), tmp_path,
                           max_model_calls=2, max_output_tokens=192)
    await session.start([{'name': 'write', 'description': 'write', 'inputSchema': {'type': 'object'}}], 'test')
    with pytest.raises(RuntimeError, match='2 次对话模型调用上限'):
        await session.turn('work', event, tool)
    with pytest.raises(RuntimeError, match='2 次对话模型调用上限'):
        await session.turn('background result', event, tool)
    assert seen == [192, 192] and writes == [1, 2]
    saved = json.loads((tmp_path / 'conversation.json').read_text())
    results = [b for m in saved['messages'] for b in m['content'] if b['type'] == 'tool_result']
    assert [json.loads(b['content']) for b in results] == [{'written': 1}, {'written': 2}]
    session.reset_budget()
    with pytest.raises(RuntimeError, match='2 次对话模型调用上限'):
        await session.turn('user continues', event, tool)
    assert seen == [192] * 4 and writes == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_abrupt_tool_exit_preserves_completed_unknown_and_unstarted_results(tmp_path):
    class ProcessExit(BaseException):
        """Skip the normal cancellation/error cleanup, like process termination."""

    requests = ['completed', 'unknown', 'unstarted']
    writes = []

    async def stream(**kwargs):
        results = [b for m in kwargs['messages'] for b in m.content if b.type == 'tool_result']
        if results:
            assert [b.tool_use_id for b in results] == requests
            assert json.loads(results[0].content) == {'written': 'completed'}
            assert not results[0].is_error
            assert results[1].is_error and '结果未确认' in results[1].content
            assert results[2].is_error and '未执行' in results[2].content
            blocks, reason = [{'type': 'text', 'text': '先核对中断的写入'}], 'end_turn'
        else:
            blocks = [{'type': 'tool_use', 'id': name, 'name': 'write', 'input': {'name': name}}
                      for name in requests]
            reason = 'tool_use'
        for item in completion_events(blocks, stop_reason=reason):
            yield item

    async def tool(name, arguments):
        writes.append(arguments['name'])
        if arguments['name'] == 'unknown':
            raise ProcessExit()
        return {'written': arguments['name']}

    specs = [{'name': 'write', 'description': 'write', 'inputSchema': {'type': 'object'}}]
    provider = SimpleNamespace(stream=stream)
    session = ModelSession(provider, tmp_path)
    thread = await session.start(specs, 'test')
    with pytest.raises(ProcessExit):
        await session.turn('write', event, tool)

    resumed = ModelSession(provider, tmp_path)
    await resumed.start(specs, 'test', thread)
    assert (await resumed.turn('continue', event, tool))['status'] == 'completed'
    assert writes == ['completed', 'unknown']


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_before_consuming', [True, False])
async def test_supplement_survives_provider_failure_and_restart(tmp_path, fail_before_consuming):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def stream(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            if not fail_before_consuming:
                for item in completion_events([{'type': 'text', 'text': 'first response'}]):
                    yield item
                return
        raise RuntimeError('provider disconnected')

    async def tool(*args):
        raise AssertionError('no tools requested')

    session = ModelSession(SimpleNamespace(stream=stream), tmp_path)
    thread = await session.start([], 'test')
    task = asyncio.create_task(session.turn('original request', event, tool))
    await entered.wait()
    await session.steer('customer supplement')
    assert json.loads((tmp_path / 'conversation.json').read_text())['pending'] == ['customer supplement']
    release.set()
    with pytest.raises(RuntimeError, match='provider disconnected'):
        await task

    async def resumed_stream(**kwargs):
        texts = [b.text for m in kwargs['messages'] if m.role == 'user' for b in m.content]
        assert texts == ['original request', 'customer supplement', 'continue']
        for item in completion_events([{'type': 'text', 'text': 'supplement retained'}]):
            yield item

    resumed = ModelSession(SimpleNamespace(stream=resumed_stream), tmp_path)
    await resumed.start([], 'test', thread)
    assert (await resumed.turn('continue', event, tool))['status'] == 'completed'
    assert json.loads((tmp_path / 'conversation.json').read_text())['pending'] == []
