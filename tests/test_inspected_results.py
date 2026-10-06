"""Saved results are available before an agent finishes composing its reply."""
import asyncio
import threading
import time

import pytest

from tests.test_project_conversation import configured, configure_agent, TestSession  # noqa: F401
from tests.test_projects import graph, node, edge, ref, start, settled
from tests.test_official_agent import official, platform, FakeAgent, signup, project, enable, chat  # noqa: F401


def conversation_until(client, path, predicate):
    for _ in range(300):
        state = client.get(path).json()
        if predicate(state):
            return state
        time.sleep(.01)
    raise AssertionError(state)


def result_cards(state):
    return [event for event in state['events'] if event['kind'] == 'result']


@pytest.mark.parametrize('provider', ['api', 'official'])
@pytest.mark.parametrize('stop_reply', [False, True])
def test_inspected_results_visible_before_reply_and_persist_without_new_runs(request, monkeypatch, provider, stop_reply):
    release = threading.Event()
    reads = []
    task_ids = []

    async def turn(self, message, on_event, on_tool, **kwargs):
        for task_id in task_ids:
            reads.append(await on_tool('workflow_run', {'action': 'inspect', 'task_id': task_id}))
        # Reading a branch or the full result does not add another card.
        for extra in ({'output_path': ['file']}, {'view': 'full'}):
            await on_tool('workflow_run', {'action': 'inspect', 'task_id': task_ids[0], **extra})
        while not release.is_set():
            await asyncio.sleep(.01)
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '两份已有结果解释完成。'}})
        return {'status': 'completed'}

    if provider == 'api':
        class ReadSaved(TestSession):
            pass
        ReadSaved.turn = turn
        client, _, p, _, base = configure_agent(request.getfixturevalue('configured'), monkeypatch, ReadSaved)
        pid = p['id']
        path = base + '/conversation'
        stop_path = base + '/agent-session/stop'
    else:
        client, _, _ = request.getfixturevalue('official')
        _, headers = signup(client, 'ReadSavedWorker')
        pid = project(client, headers)
        enable(client, pid)
        client.headers.update(headers)
        base = '/api/v1/projects/' + pid
        path = chat(client, pid, headers)
        stop_path = path + '/stop'
        monkeypatch.setattr(FakeAgent, 'turn', turn)

    graph(client, pid, [node('s', 'start', inputs=[{'name': 'file', 'type': 'string'}]),
                       node('e', 'end', outputs={'file': ref('s', 'file')})], [edge('s', 'e')])
    tasks = [settled(client, base, start(client, base, name, inputs={'file': name}))
             for name in ('first.csv', 'second.csv')]
    assert all(task['status'] == 'succeeded' for task in tasks)
    task_ids.extend(task['id'] for task in tasks)
    try:
        assert client.post(path + '/messages', json={'message': '解释这两份结果，不重新运行'}).status_code == 202
        live = conversation_until(client, path, lambda state: len(result_cards(state)) == 2)
        assert live['status'] == 'running'
        cards = result_cards(live)
        assert {card['task_id'] for card in cards} == set(task_ids)
        assert all(card['request_id'] == live['request_id'] and card['result_kind'] == 'inspected' for card in cards)
        assert not any(event['kind'] == 'assistant' for event in live['events'])
        assert [read['outputs']['file'] for read in reads] == ['first.csv', 'second.csv']
        if stop_reply:
            assert client.post(stop_path).status_code == 200
        else:
            release.set()
        final = conversation_until(client, path, lambda state: state['status'] in {'idle', 'interrupted', 'error'})
        assert final['status'] == ('interrupted' if stop_reply else 'idle'), final.get('error')
        assert result_cards(final) == cards
        for task in tasks:
            assert client.get(base + '/tasks/' + task['id']).json() == task
        assert len(client.get(base + '/tasks').json()) == 2
        if not stop_reply:
            # A later question can reference the same saved tasks, once each.
            assert client.post(path + '/messages', json={'message': '再解释一下这两份结果'}).status_code == 202
            again = conversation_until(client, path, lambda state: state['status'] in {'idle', 'error'})
            assert again['status'] == 'idle', again.get('error')
            assert len(result_cards(again)) == 4
            assert {card['request_id'] for card in result_cards(again)} == {live['request_id'], again['request_id']}
            assert len(client.get(base + '/tasks').json()) == 2
    finally:
        release.set()


def test_stopping_explanation_does_not_stop_independently_started_task(configured, monkeypatch):
    ids = []

    class ReadActive(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            await on_tool('workflow_run', {'action': 'inspect', 'task_id': ids[0]})
            await asyncio.sleep(30)
            return {'status': 'completed'}

    client, app, p, _, base = configure_agent(configured, monkeypatch, ReadActive)
    graph(client, p['id'], [node('s', 'start'), node('e', 'end', outputs={'answer': 42})], [edge('s', 'e')])
    run = app.state.services.projects._run
    release = threading.Event()

    async def delayed(task):
        while not release.is_set():
            await asyncio.sleep(.01)
        return await run(task)

    monkeypatch.setattr(app.state.services.projects, '_run', delayed)
    task = start(client, base, 'independent')
    ids.append(task['id'])
    try:
        client.post(base + '/conversation/messages', json={'message': '查看现有运行'})
        live = conversation_until(client, base + '/conversation', lambda state: len(result_cards(state)) == 1)
        assert live['status'] == 'running'
        assert client.post(base + '/agent-session/stop').status_code == 200
        stopped = conversation_until(client, base + '/conversation', lambda state: state['status'] == 'interrupted')
        assert result_cards(stopped) == result_cards(live)
        assert client.get(base + '/tasks/' + task['id']).json()['status'] in {'queued', 'running'}
    finally:
        release.set()
    complete = settled(client, base, task)
    assert complete['status'] == 'succeeded' and complete['outputs'] == {'answer': 42}
    assert len(client.get(base + '/tasks').json()) == 1


def test_failed_or_internal_reads_do_not_publish_customer_result_cards(configured, monkeypatch):
    requests = []
    errors = []

    class ReadInvalid(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            for arguments in requests:
                try:
                    await on_tool('workflow_run', {'action': 'inspect', **arguments})
                except (ValueError, KeyError) as cause:
                    errors.append(str(cause))
            return {'status': 'completed'}

    client, _, p, _, base = configure_agent(configured, monkeypatch, ReadInvalid)
    graph(client, p['id'], [node('s', 'start'), node('e', 'end', outputs={'value': 42})], [edge('s', 'e')])
    internal = settled(client, base, start(client, base, 'internal', purpose='build_test'))
    customer = settled(client, base, start(client, base, 'customer'))
    other = client.post('/api/v1/projects', json={'name': 'Outside'}).json()['id']
    outside_base = '/api/v1/projects/' + other
    outside = settled(client, outside_base, start(client, outside_base, 'outside'))
    requests.extend([{'task_id': internal['id']}, {'task_id': customer['id'], 'output_path': ['missing']},
                     {'task_id': outside['id']}])
    client.post(base + '/conversation/messages', json={'message': '查看结果'})
    final = conversation_until(client, base + '/conversation', lambda state: state['status'] in {'idle', 'error'})
    assert final['status'] == 'idle', final.get('error')
    assert len(errors) == 2
    assert result_cards(final) == []
    assert len(client.get(base + '/tasks').json()) == 2
