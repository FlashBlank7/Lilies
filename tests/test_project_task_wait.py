import asyncio

import pytest

from agent_platform.workflow_runtime import _NODE_EXECUTORS
from tests.test_projects import configured, graph, node, edge, start, settled  # noqa: F401


def held_workflow(configured, monkeypatch, *, fail=False):
    client, app, project, _ = configured
    entered, release = asyncio.Event(), asyncio.Event()
    original = _NODE_EXECUTORS['end']

    async def held(runtime, run):
        entered.set()
        await release.wait()
        if fail:
            raise ValueError('test execution failed')
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'end', held)
    graph(client, project['id'], [node('start', 'start'), node('end', 'end', outputs={'ready': True})],
        [edge('start', 'end')])
    base = '/api/v1/projects/' + project['id']
    task = start(client, base, 'one-task')
    client.portal.call(asyncio.wait_for, entered.wait(), 2)
    return client, app, base, task, release


def inspect(client, base, task, **arguments):
    response = client.post(base + '/agent-tools', json={'name': 'workflow_run',
        'arguments': {'action': 'inspect', 'task_id': task['id'], **arguments}})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('fail', [False, True])
def test_inspect_wait_returns_existing_task_terminal_state(configured, monkeypatch, fail):
    client, app, base, task, release = held_workflow(configured, monkeypatch, fail=fail)
    manual = client.post(base + '/agent-tools', json={'name': 'block_catalog',
        'arguments': {'tool_name': 'workflow_run'}}).json()
    field = manual['input_schema']['properties']['wait_seconds']
    assert field['default'] == 0 and field['maximum'] == 60 and field['minimum'] == 0
    example = next(e for e in manual['examples'] if e.get('wait_seconds'))
    example['task_id'] = task['id']

    async def release_soon():
        asyncio.get_running_loop().call_later(.05, release.set)

    client.portal.call(release_soon)
    result = client.post(base + '/agent-tools', json={'name': 'workflow_run', 'arguments': example}).json()
    assert result['id'] == task['id']
    assert result['status'] == ('failed' if fail else 'succeeded')
    if fail:
        assert 'test execution failed' in result['error']
    else:
        assert result['outputs'] == {'ready': True}
    assert len(client.get(base + '/tasks').json()) == 1
    assert len(inspect(client, base, task, view='full')['runs']) == 1


def test_inspect_timeout_keeps_original_worker_running(configured, monkeypatch):
    client, app, base, task, release = held_workflow(configured, monkeypatch)
    worker = app.state.services.projects.active[task['id']]
    result = inspect(client, base, task, wait_seconds=1)
    assert result['id'] == task['id'] and result['status'] == 'running'
    assert app.state.services.projects.active[task['id']] is worker
    assert not worker.done() and not worker.cancelled()
    assert len(client.get(base + '/tasks').json()) == 1
    client.portal.call(release.set)
    done = settled(client, base, task)
    assert done['status'] == 'succeeded' and done['outputs'] == {'ready': True}


def test_inspect_default_returns_immediately_without_task_side_effects(configured, monkeypatch):
    client, app, base, task, release = held_workflow(configured, monkeypatch)
    result = inspect(client, base, task)
    assert result['id'] == task['id'] and result['status'] == 'running'
    assert not release.is_set() and not app.state.services.projects.active[task['id']].done()
    assert len(client.get(base + '/tasks').json()) == 1
    client.portal.call(release.set)
    assert settled(client, base, task)['status'] == 'succeeded'


@pytest.mark.parametrize('arguments', [
    *[{'action': 'inspect', 'task_id': 'existing', 'wait_seconds': value} for value in [-1, 61, True, 1.5, '2']],
    {'action': 'start', 'wait_seconds': 1},
    {'action': 'inspect', 'run_id': 'existing-run', 'wait_seconds': 1},
])
def test_inspect_wait_rejects_invalid_parameters_without_creating_tasks(configured, arguments):
    client, _, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/agent-tools', json={'name': 'workflow_run', 'arguments': arguments})
    assert response.status_code == 422 and 'wait_seconds' in response.text
    assert client.get(base + '/tasks').json() == []
