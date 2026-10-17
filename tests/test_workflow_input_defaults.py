"""Declared optional inputs work without extra customer-supplied parameters."""
import pytest

from agent_platform.workflow_runtime import WorkflowRuntime
from tests.test_projects import configured, edge, graph, node, ref  # noqa: F401
from tests.test_project_validation import run_tool


@pytest.mark.parametrize('nested', [False, True])
def test_declared_defaults_and_nulls_match_start_output(configured, nested):
    client, _, project, _ = configured
    pid = project['id']
    target = (client.post(f'/api/v1/projects/{pid}/members', json={'name': '整理'}).json()['id']
              if nested else pid)
    graph(client, target, [node('s', 'start', inputs=[
        {'name': 'source', 'type': 'string', 'required': True},
        {'name': 'directory', 'type': 'string', 'required': False, 'default': 'results'},
        {'name': 'note', 'type': 'string', 'required': False}]),
        node('e', 'end', outputs={'source': ref('$inputs', 'source'),
            'directory': ref('$inputs', 'directory'), 'note': ref('$inputs', 'note'),
            'declared': ref('s', 'output')})], [edge('s', 'e')])
    for overrides, expected in [({}, 'results'), ({'directory': 'new'}, 'new'), ({'directory': None}, None)]:
        supplied = {'source': 'sample.csv', **overrides}
        if nested:
            graph(client, pid, [node('s', 'start'),
                node('call', 'tool', tool_name='workflow:'+target, input=supplied),
                node('e', 'end', outputs={'result': ref('call', 'output')})],
                [edge('s', 'call'), edge('call', 'e')])
        task = run_tool(client, pid, 'start', inputs={} if nested else supplied)
        assert task['status'] == 'succeeded', task
        output = task['outputs']['result'] if nested else task['outputs']
        assert output == {'source': 'sample.csv', 'directory': expected, 'note': None,
                          'declared': {'source': 'sample.csv', 'directory': expected, 'note': None}}
        assert task['inputs'] == ({} if nested else supplied)


def test_missing_required_and_undeclared_inputs_still_fail_without_repeat(configured, monkeypatch):
    client, _, project, _ = configured
    pid = project['id']
    calls = []
    original = WorkflowRuntime._execute_node
    async def counted(self, snapshot, node, *args, **kwargs):
        calls.append(node.id)
        return await original(self, snapshot, node, *args, **kwargs)
    monkeypatch.setattr(WorkflowRuntime, '_execute_node', counted)
    graph(client, pid, [node('s', 'start', inputs=[{'name': 'source', 'type': 'string', 'required': True}]),
        {**node('e', 'end', outputs={'directory': ref('$inputs', 'directory')}),
         'retry': {'enabled': True, 'max_attempts': 3, 'delay_seconds': 0}}], [edge('s', 'e')])
    missing = run_tool(client, pid, 'start')
    assert missing['status'] == 'failed' and 'source' in missing['error']
    assert calls == []
    failed = run_tool(client, pid, 'start', inputs={'source': 'sample.csv'})
    assert failed['status'] == 'failed' and "path=['directory']" in failed['error']
    assert calls.count('e') == 1
    assert 'source' in failed['error'] and '节点代码执行前' in failed['error']
    graph(client, pid, [node('s', 'start', inputs=[{'name': 'source', 'type': 'string', 'required': True},
        {'name': 'directory', 'type': 'string', 'required': False}]),
        node('e', 'end', outputs={'directory': ref('$inputs', 'directory')})], [edge('s', 'e')])
    repaired = run_tool(client, pid, 'start', inputs={'source': 'sample.csv'})
    assert repaired['status'] == 'succeeded' and repaired['outputs'] == {'directory': None}


def test_input_defaults_remain_available_after_pause(configured):
    client, _, project, _ = configured
    pid = project['id']
    graph(client, pid, [node('s', 'start', inputs=[{'name': 'directory', 'type': 'string',
            'required': False, 'default': 'results'}]),
        node('ask', 'human_input', title='补充说明', fields=[{'name': 'answer', 'label': '说明', 'type': 'string'}]),
        node('e', 'end', outputs={'directory': ref('$inputs', 'directory'), 'answer': ref('ask', 'answer')})],
        [edge('s', 'ask'), edge('ask', 'e')])
    task = run_tool(client, pid, 'start')
    assert task['status'] == 'waiting_input', task
    run = task['runs'][0]
    result = run_tool(client, pid, 'respond', task_id=task['id'], run_id=run['id'],
                      node_id='ask', inputs={'answer': '保留'})
    assert result['status'] == 'succeeded' and result['outputs'] == {'directory': 'results', 'answer': '保留'}
