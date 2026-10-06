"""Generation can use existing static feedback without executing or gating drafts."""
from copy import deepcopy
import time

import pytest

from tests.test_official_agent import official, FakeAgent, signup, project, enable  # noqa: F401
from tests.test_users import platform  # noqa: F401
from tests.test_projects import node, edge, ref


def await_generation(client, base, headers, body):
    response = client.post(base + '/workflow-generation', headers=headers, json=body)
    assert response.status_code == 202, response.text
    job = response.json()['job_id']
    for _ in range(300):
        result = client.get(base + '/generation-jobs/' + job, headers=headers).json()
        if result['status'] not in {'queued', 'running', 'waiting'}:
            return result
        time.sleep(.01)
    pytest.fail('generation did not finish')


@pytest.mark.parametrize('correct', [False, True])
def test_generation_receives_unreachable_node_feedback_without_running_or_gating_draft(official, monkeypatch, correct):
    client, app, _ = official
    _, headers = signup(client, '结构反馈员工')
    pid = project(client, headers); enable(client, pid)
    base = '/api/v1/projects/' + pid
    before = client.get(base + '/members', headers=headers).json()
    broken = {'nodes': [node('s', 'start'), node('unused', 'variable_assigner', assignments={'value': 3}),
                        node('e', 'end', outputs={'value': 3})], 'edges': [edge('s', 'e')]}
    fixed = {**broken, 'edges': [edge('s', 'unused'), edge('unused', 'e')]}
    receipts = []

    async def turn(self, message, on_event, on_tool, **kwargs):
        receipt = await on_tool('return_workflow', {'workflow': broken})
        receipts.append(receipt)
        assert receipt['received'] and receipt['structure_check']['valid'] is False
        assert any('unused' in error for error in receipt['structure_check']['errors'])
        assert receipt['structure_check']['runtime_checked'] is False
        assert not kwargs['tool_result_ready']('return_workflow', receipt)
        if correct:
            receipt = await on_tool('return_workflow', {'workflow': fixed})
            receipts.append(receipt)
            assert receipt['structure_check']['valid'] is True
            assert kwargs['tool_result_ready']('return_workflow', receipt)
        return {'status': 'completed'}

    async def unexpected_execution(*args, **kwargs):
        raise AssertionError('Static generation feedback must not execute a workflow')

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    monkeypatch.setattr(app.state.services.workflow_runtime, 'create_run', unexpected_execution)
    job = await_generation(client, base, headers, {'instruction': '准备一个可继续编辑的流程'})
    assert job['status'] == 'completed', job
    assert job['result']['structure_check']['valid'] is correct
    assert len(receipts) == (2 if correct else 1)
    assert len(client.get(base + '/members', headers=headers).json()) == len(before) + 1
    assert client.get(base + '/tasks', headers=headers).json() == []


def test_generation_feedback_checks_selected_fragment_in_its_existing_scope(official, monkeypatch):
    client, _, _ = official
    _, headers = signup(client, '选区反馈员工')
    pid = project(client, headers); enable(client, pid)
    base = '/api/v1/projects/' + pid
    original = {'nodes': [node('s', 'start'), node('e', 'end', outputs={'value': ref('s', 'value')})],
                'edges': [edge('s', 'e')]}
    draft = client.get(f'/api/v1/applications/{pid}/draft', headers=headers).json()
    saved = client.put(base + f'/workflows/{pid}/draft', headers=headers,
                       json={'expected_revision': draft['revision'], 'workflow': original})
    assert saved.status_code == 200, saved.text
    fragment = {'nodes': [node('e', 'end', outputs={'value': ref('s', 'value'), 'extra': 42})], 'edges': []}
    unchanged = deepcopy(fragment)

    async def turn(self, message, on_event, on_tool, **kwargs):
        receipt = await on_tool('return_workflow', {'workflow': fragment})
        assert receipt['structure_check']['valid'] is True, receipt
        assert kwargs['tool_result_ready']('return_workflow', receipt)
        assert fragment == unchanged
        return {'status': 'completed'}

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    job = await_generation(client, base, headers, {'instruction': '只增加结果字段', 'workflow_id': pid,
        'expected_revision': saved.json()['revision'], 'node_ids': ['e']})
    assert job['status'] == 'completed', job
    graph = job['result']['draft']['snapshot']['workflow']
    assert graph['nodes'][0]['id'] == 's'
    assert graph['nodes'][1]['config']['outputs']['extra'] == 42
    assert [(item['source'], item['target']) for item in graph['edges']] == [('s', 'e')]
    assert client.get(base + '/tasks', headers=headers).json() == []


def test_official_receiver_applies_small_updates_once_and_rejects_ambiguous_delivery(official, monkeypatch):
    client, _, _ = official
    _, headers = signup(client, '简短修改员工')
    pid = project(client, headers); enable(client, pid)
    base = '/api/v1/projects/' + pid
    original = {'nodes': [node('s', 'start'), node('e', 'end', outputs={'existing': 1})],
                'edges': [edge('s', 'e')]}
    draft = client.get(f'/api/v1/applications/{pid}/draft', headers=headers).json()
    saved = client.put(base + f'/workflows/{pid}/draft', headers=headers,
        json={'expected_revision': draft['revision'], 'workflow': original}).json()
    operations = [{'op': 'update_node', 'data': {'node_id': 'e', 'changes': {'config': {'outputs': {'new': 2}}}}}]

    async def turn(self, message, on_event, on_tool, **kwargs):
        with pytest.raises(ValueError, match='一种交付方式'):
            await on_tool('return_workflow', {'workflow': original, 'operations': operations})
        receipt = await on_tool('return_workflow', {'operations': operations})
        assert receipt['structure_check']['valid'] is True
        assert kwargs['tool_result_ready']('return_workflow', receipt)
        return {'status': 'interrupted', 'result_received': True}

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    job = await_generation(client, base, headers, {'instruction': '增加结果字段，保留已有字段',
        'workflow_id': pid, 'expected_revision': saved['revision'], 'node_ids': ['e']})
    assert job['status'] == 'completed', job
    assert job['result']['revision'] == saved['revision'] + 1
    assert job['result']['draft']['snapshot']['workflow']['nodes'][1]['config']['outputs'] == {'existing': 1, 'new': 2}
    assert client.get(base + '/tasks', headers=headers).json() == []
