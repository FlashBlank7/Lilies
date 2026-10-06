import asyncio

import pytest

from agent_platform.workflow_runtime import _NODE_EXECUTORS
from agent_platform.project_agent_tools import WorkspaceProjectTools
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


def release_worker(client, worker, release):
    async def finish():
        release.set()
        # Wait for the worker we just released, without competing with its
        # final persistence through hundreds of HTTP/database reads.
        done, _ = await asyncio.wait({worker}, timeout=3)
        if worker not in done:
            stacks = [f'{task.get_name()}: ' + ' -> '.join(
                f'{frame.f_code.co_name}:{frame.f_lineno}' for frame in task.get_stack())
                for task in asyncio.all_tasks() if not task.done()]
            pytest.fail('Released worker did not finish; active tasks: ' + '; '.join(stacks))
        await worker

    client.portal.call(finish)


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
    release_worker(client, worker, release)
    done = client.get(base + '/tasks/' + task['id']).json()
    assert done['status'] == 'succeeded' and done['outputs'] == {'ready': True}
    assert len(done['runs']) == 1


def test_inspect_default_returns_immediately_without_task_side_effects(configured, monkeypatch):
    client, app, base, task, release = held_workflow(configured, monkeypatch)
    result = inspect(client, base, task)
    assert result['id'] == task['id'] and result['status'] == 'running'
    assert not release.is_set() and not app.state.services.projects.active[task['id']].done()
    assert len(client.get(base + '/tasks').json()) == 1
    release_worker(client, app.state.services.projects.active[task['id']], release)
    done = client.get(base + '/tasks/' + task['id']).json()
    assert done['status'] == 'succeeded' and done['outputs'] == {'ready': True}


def test_diagnostic_wait_observes_actual_failure_without_restart(configured, monkeypatch):
    client, app, base, task, release = held_workflow(configured, monkeypatch, fail=True)
    async def release_soon():
        asyncio.get_running_loop().call_later(.05, release.set)
    client.portal.call(release_soon)
    result = inspect(client, base, task, view='diagnostic', wait_seconds=1)
    assert result['id'] == task['id'] and result['status'] == 'failed'
    assert result['node_errors'][-1]['error'] == 'test execution failed'
    assert [n['id'] for n in result['nodes']] == ['end']
    assert len(client.get(base + '/tasks').json()) == 1


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


@pytest.mark.parametrize('outcome', ['succeeded', 'failed', 'background', 'interrupted'])
def test_respond_waits_on_same_task_and_respects_background_and_stop(configured, monkeypatch, outcome):
    client, app, project, _ = configured
    pid = project['id']
    base = '/api/v1/projects/' + pid
    entered, release = asyncio.Event(), asyncio.Event()
    original = _NODE_EXECUTORS['end']
    executions = []

    async def held(runtime, run):
        executions.append('end')
        entered.set()
        await release.wait()
        if outcome == 'failed':
            raise ValueError('answer processing failed')
        return await original(runtime, run)

    monkeypatch.setitem(_NODE_EXECUTORS, 'end', held)
    graph(client, pid, [node('start', 'start'),
        node('human', 'human_input', fields=[{'name': 'answer', 'label': '回答', 'type': 'string'}]),
        node('end', 'end', outputs={'ready': True})],
        [edge('start', 'human'), edge('human', 'end')])
    task = settled(client, base, start(client, base, 'answer-once'))
    assert task['status'] == 'waiting_input'
    run_id = task['runs'][0]['id']
    tools = WorkspaceProjectTools(app.state.services, pid, app.state.services.local_agents)
    arguments = {'action': 'respond', 'task_id': task['id'], 'run_id': run_id,
                 'node_id': 'human', 'inputs': {'answer': '不清楚'}}
    if outcome == 'background':
        arguments['wait'] = False
    if outcome == 'succeeded':
        arguments['view'] = 'full'
    response = client.portal.start_task_soon(tools.call, 'workflow_run', arguments)
    client.portal.call(asyncio.wait_for, entered.wait(), 2)
    if outcome == 'background':
        running = response.result(timeout=2)
        assert running['id'] == task['id'] and running['status'] == 'running'
        assert not app.state.services.projects.active[task['id']].done()
        client.portal.call(release.set)
    else:
        assert not response.done()
        if outcome == 'interrupted':
            client.post(base + '/tasks/' + task['id'] + '/stop').raise_for_status()
            result = response.result(timeout=2)
            assert result['status'] == 'interrupted' and not release.is_set()
        else:
            client.portal.call(release.set)
            result = response.result(timeout=2)
            assert result['id'] == task['id'] and result['status'] == outcome
            assert result['runs'][0]['waiting_input'] is None
            if outcome == 'failed':
                assert 'answer processing failed' in result['error']
            else:
                assert result['outputs'] == {'ready': True}
                assert result['runs'][0]['outputs'] == {'ready': True}
    done = settled(client, base, task)
    assert done['status'] == ('succeeded' if outcome == 'background' else outcome)
    assert executions == ['end']
    assert len(done['runs']) == 1 and done['runs'][0]['id'] == run_id
    assert len(client.get(base + '/tasks').json()) == 1
