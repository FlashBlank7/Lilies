"""Replicate a real small repair without regenerating an unchanged graph."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_platform.db import connect
from tests.test_projects import configured as configured  # noqa: F401
from tests.test_projects import graph, node, edge, ref
from tests.test_project_tool_help import prepare


def fixture(configured):
    client, manager, pid, base = prepare(configured)
    nodes = [node('start', 'start', inputs=[{'name': 'quantity', 'type': 'number', 'required': True}]),
             node('end', 'end', outputs={'quantity': ref('start', 'quantity')})]
    nodes[0]['position'] = {'x': 215, 'y': 84}
    nodes[0]['description'] = 'Keep the human layout and explanation.'
    graph(client, pid, nodes, [edge('start', 'end')])
    before = client.get('/api/v1/applications/' + pid + '/draft').json()
    return client, pid, base, before


def call(client, base, tool_name, **arguments):
    result = client.post(base + '/agent-tools', json={'name': tool_name, 'arguments': arguments})
    assert result.status_code == 200, result.text
    return result.json()


def copy_args(pid, before, **changes):
    return {'action': 'copy', 'workflow_id': pid, 'expected_revision': before['revision'],
            'name': 'Editable repair', 'request_key': 'same-repair', **changes}


def test_copy_local_edit_and_two_real_runs_preserve_original(configured):
    client, pid, base, before = fixture(configured)
    for key in ('failure-one', 'failure-two'):
        failed = call(client, base, 'workflow_run', action='start', inputs={}, request_key=key)
        assert failed['status'] == 'failed' and 'quantity' in failed['error']
    copied = call(client, base, 'project_workflows', **copy_args(pid, before))
    copy_path = '/api/v1/applications/' + copied['id'] + '/draft'
    snapshot = client.get(copy_path).json()['snapshot']
    assert snapshot['workflow'] == before['snapshot']['workflow']
    assert snapshot['tests'] == before['snapshot']['tests']
    assert copied['tested_hash'] is None
    # The copy response is enough to edit; no empty-draft/schema reads needed.
    changed = call(client, base, 'workflow_draft', workflow_id=copied['id'], batch={
        'expected_revision': copied['revision'], 'expected_content_hash': copied['content_hash'],
        'idempotency_key': 'default-quantity', 'operations': [{'op': 'update_node', 'data': {
            'node_id': 'start', 'changes': {'config': {'inputs': [
                {'name': 'quantity', 'type': 'number', 'required': False, 'default': 1}]}},
            'merge_config': True}}]})
    assert changed['revision'] == copied['revision'] + 1
    for inputs, quantity in (({}, 1), ({'quantity': 4}, 4)):
        result = call(client, base, 'workflow_run', action='start', workflow_id=copied['id'], inputs=inputs)
        assert result['status'] == 'succeeded' and result['outputs'] == {'quantity': quantity}
    assert client.get('/api/v1/applications/' + pid + '/draft').json() == before
    retried = call(client, base, 'project_workflows', **copy_args(pid, before))
    assert retried['id'] == copied['id'] and retried['content_hash'] == changed['content_hash']
    assert len(client.get(base).json()['members']) == 2


def test_copy_preserves_nested_code_tests_and_unbound_resources(configured):
    client, pid, base, before = fixture(configured)
    services = configured[1].state.services
    async def setup():
        from agent_platform.workflow_models import WorkflowTestCase, NodeSpec
        draft = await services.workflow_store.get_draft(pid)
        snapshot = draft['snapshot']
        snapshot.tests = [WorkflowTestCase(id='quantity', name='quantity', requirement='Return quantity', inputs={'quantity': 4})]
        snapshot.workflow.nodes.append(NodeSpec(id='unbound', type='model_predict', title='Unbound',
            config={'model_ref': 'future-model'}))
        snapshot.workflow.nodes.append(NodeSpec(id='nested', type='loop', title='Nested', config={'workflow': {
            'nodes': [node('code', 'code', code='print("unchanged")')], 'edges': []}}))
        await services.workflow_store.save_draft(pid, snapshot,
            expected_revision=draft['revision'], idempotency_key='fixture-resources')
    client.portal.call(setup)
    before = client.get('/api/v1/applications/' + pid + '/draft').json()
    copied = call(client, base, 'project_workflows', **copy_args(pid, before))
    after = client.get('/api/v1/applications/' + copied['id'] + '/draft').json()
    for key in ('workflow', 'tests', 'requirement'):
        assert after['snapshot'][key] == before['snapshot'][key]
    assert client.get(base + '/tasks').json() == []


def test_copy_rejects_cross_project_stale_revision_and_conflicting_retry(configured):
    client, pid, base, before = fixture(configured)
    other = client.post('/api/v1/projects', json={'name': 'Other project'}).json()['id']
    for changes in ({'workflow_id': other}, {'expected_revision': 0}):
        response = client.post(base + '/agent-tools', json={'name': 'project_workflows',
            'arguments': copy_args(pid, before, **changes)})
        assert response.status_code in (409, 422)
    assert len(client.get(base).json()['members']) == 1
    copied = call(client, base, 'project_workflows', **copy_args(pid, before))
    conflict = client.post(base + '/agent-tools', json={'name': 'project_workflows',
        'arguments': copy_args(pid, before, name='Different copy')})
    assert conflict.status_code in (409, 422)
    assert len(client.get(base).json()['members']) == 2
    assert client.get('/api/v1/applications/' + copied['id'] + '/draft').json()['snapshot']['name'] == 'Editable repair'


def test_concurrent_copy_creates_one_member(configured):
    client, pid, base, before = fixture(configured)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: call(client, base, 'project_workflows', **copy_args(pid, before)), range(2)))
    assert results[0]['id'] == results[1]['id']
    assert len(client.get(base).json()['members']) == 2


def test_copy_failure_rolls_back_and_retry_succeeds(configured):
    client, pid, base, before = fixture(configured)
    database = configured[1].state.services.storage.db_path
    with connect(database) as db:
        count = db.execute('SELECT count(*) FROM applications').fetchone()[0]
        db.execute("CREATE TRIGGER fail_copy BEFORE INSERT ON project_members BEGIN SELECT RAISE(ABORT,'copy failure'); END")
    try:
        with pytest.raises(Exception, match='copy failure'):
            call(client, base, 'project_workflows', **copy_args(pid, before))
        with connect(database) as db:
            assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == count
            assert not db.execute("SELECT 1 FROM draft_idempotency WHERE idempotency_key='project_workflow_copy'").fetchone()
    finally:
        with connect(database) as db:
            db.execute('DROP TRIGGER fail_copy')
    call(client, base, 'project_workflows', **copy_args(pid, before))
    assert len(client.get(base).json()['members']) == 2


def test_copy_checks_current_project_capabilities(configured):
    client, pid, base, before = fixture(configured)
    assert client.put(base + '/capabilities', json={'agent_modules_enabled': True}).status_code == 200
    graph(client, pid, [node('agent', 'claude_agent', agent_id='unbound-agent', task='Example')], [])
    before = client.get('/api/v1/applications/' + pid + '/draft').json()
    assert client.put(base + '/capabilities', json={'agent_modules_enabled': False}).status_code == 200
    response = client.post(base + '/agent-tools', json={'name': 'project_workflows', 'arguments': copy_args(pid, before)})
    assert response.status_code == 422 and '禁止使用智能体' in response.text
    assert len(client.get(base).json()['members']) == 1


def test_retry_after_source_removed_returns_existing_copy(configured):
    client, pid, base, before = fixture(configured)
    source = client.post(base + '/members', json={'name': 'Temporary source'}).json()['id']
    original = client.get('/api/v1/applications/' + source + '/draft').json()
    arguments = copy_args(source, original)
    copied = call(client, base, 'project_workflows', **arguments)
    assert client.delete(base + '/members/' + source).status_code == 200
    assert call(client, base, 'project_workflows', **arguments)['id'] == copied['id']


def test_conversation_copy_and_reply_finish_without_result_task_and_count_repeated_reads(configured, monkeypatch):
    import json
    from tests.test_project_conversation import TestSession, configure_agent
    from tests.test_local_agents import settled
    client, pid, base, before = fixture(configured)

    class RepairSession(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message)
            assert 'business_task' not in context
            for _ in range(2):
                await on_tool('project_workflows', {'action': 'inspect', 'workflow_id': pid})
            for _ in range(2):
                await on_tool('project_workflows', copy_args(pid, before))
            await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '已复制，尚未执行。'}})
            return {'status': 'completed'}

    configure_agent(configured, monkeypatch, RepairSession)
    assert client.post(base + '/conversation/messages', json={'message': '复制这个流程'}).status_code == 202
    state = settled(client, base)
    assert state['status'] == 'idle' and not state['error']
    assert client.get(base + '/tasks').json() == []
    metrics = client.get(base + '/conversation/metrics').json()['requests'][0]
    assert metrics['tool_calls'] == 4 and metrics['tool_failures'] == 0
    assert metrics['identical_read_responses'] == 1  # inspect counts; a copy retry does not.
