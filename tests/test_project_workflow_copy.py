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


def test_copy_with_node_updates_saves_once_and_reports_actual_fields(configured):
    client, pid, base, before = fixture(configured)
    updates = {'start': {'config': {'inputs': [
        {'name': 'quantity', 'type': 'number', 'required': False, 'default': 1}]}}}
    arguments = copy_args(pid, before, node_updates=updates)
    copied = call(client, base, 'project_workflows', **arguments)
    assert copied['revision'] == copied['applied_revision'] == 0
    assert copied['content_hash'] == copied['applied_content_hash']
    check = copied['structure_check']
    assert check['valid'] is True and check['runtime_checked'] is False
    assert check['revision'] == copied['applied_revision'] and check['content_hash'] == copied['applied_content_hash']
    separate = call(client, base, 'workflow_run', action='validate', workflow_id=copied['id'])
    assert {k: check[k] for k in ('valid', 'errors', 'warnings', 'revision', 'content_hash')} == {
        k: separate[k] for k in ('valid', 'errors', 'warnings', 'revision', 'content_hash')}
    assert client.get(base + '/tasks').json() == []
    assert copied['changes']['node_count'] == 1
    change = copied['changes']['nodes'][0]
    assert change['node_id'] == 'start' and change['fields'] == updates['start']
    for inputs, value in (({}, 1), ({'quantity': 4}, 4)):
        assert call(client, base, 'workflow_run', action='start', workflow_id=copied['id'], inputs=inputs)['outputs'] == {'quantity': value}
    # A retry after a human edit returns its original receipt, without applying it again.
    edit = {'expected_revision': 0, 'expected_content_hash': copied['content_hash'],
            'idempotency_key': 'human-title', 'operations': [{'op': 'update_node',
            'data': {'node_id': 'end', 'changes': {'title': 'Human result title'}}}]}
    changed = call(client, base, 'workflow_draft', workflow_id=copied['id'], batch=edit)
    assert changed['changes']['nodes'][0]['fields'] == {'title': 'Human result title'}
    repeated_edit = call(client, base, 'workflow_draft', workflow_id=copied['id'], batch=edit)
    assert repeated_edit['applied_revision'] == repeated_edit['revision'] == 1
    assert repeated_edit['changes_available'] is False and 'changes' not in repeated_edit
    retry = call(client, base, 'project_workflows', **arguments)
    assert retry['id'] == copied['id'] and retry['applied_revision'] == 0 and retry['revision'] == 1
    assert retry['changes'] == copied['changes']
    assert retry['structure_check'] == check and retry['structure_check']['revision'] != retry['revision']
    assert client.get('/api/v1/applications/' + pid + '/draft').json() == before
    assert client.get('/api/v1/applications/' + copied['id'] + '/draft').json()['snapshot']['workflow']['nodes'][0]['position'] == {'x': 215, 'y': 84}


def test_copy_update_failure_is_atomic_and_changed_retry_conflicts(configured):
    client, pid, base, before = fixture(configured)
    bad = copy_args(pid, before, node_updates={'start': {'title': 'Temporary'}, 'missing': {'title': 'Error'}})
    response = client.post(base + '/agent-tools', json={'name': 'project_workflows', 'arguments': bad})
    assert response.status_code in (404, 422)
    assert len(client.get(base).json()['members']) == 1
    good = copy_args(pid, before, node_updates={'start': {'title': 'Saved'}})
    call(client, base, 'project_workflows', **good)
    changed = {**good, 'node_updates': {'start': {'title': 'Different'}}}
    response = client.post(base + '/agent-tools', json={'name': 'project_workflows', 'arguments': changed})
    assert response.status_code in (409, 422)
    assert len(client.get(base).json()['members']) == 2


def test_copy_updates_cannot_add_foreign_references(configured):
    client, pid, base, before = fixture(configured)
    other = client.post('/api/v1/projects', json={'name': 'Foreign'}).json()['id']
    args = copy_args(pid, before, node_updates={'end': {'config': {'outputs': {'foreign': 'workflow:' + other}}}})
    response = client.post(base + '/agent-tools', json={'name': 'project_workflows', 'arguments': args})
    assert response.status_code == 422 and '当前项目' in response.text
    assert len(client.get(base).json()['members']) == 1


def test_node_read_and_diagnostic_keep_original_error_separate_from_current_draft(configured):
    client, pid, base, before = fixture(configured)
    failed = call(client, base, 'workflow_run', action='start', inputs={})
    read = failed['diagnostic_with']
    diagnosis = call(client, base, read['tool'], **read['arguments'])
    assert diagnosis['failure_stage'] == 'input_validation'
    assert diagnosis['error'] == failed['error'] and diagnosis['inputs'] == {}
    assert [n['id'] for n in diagnosis['nodes']] == ['start']
    assert diagnosis['connections'][0]['edges'][0]['target'] == 'end'
    assert diagnosis['connections'][0]['neighbors'] == [{'id': 'end', 'type': 'end', 'title': 'end'}]
    assert not diagnosis['current_draft']['changed_since_run']
    assert diagnosis['edit_base'] == {'workflow_id': pid, 'expected_revision': before['revision'],
                                     'expected_content_hash': before['content_hash']}
    changed = call(client, base, 'workflow_draft', operation={'op': 'update_node',
        'expected_revision': before['revision'], 'idempotency_key': 'now-optional',
        'data': {'node_id': 'start', 'changes': {'config': {'inputs': [
            {'name': 'quantity', 'type': 'number', 'default': 1, 'required': False}]}}}})
    old = call(client, base, read['tool'], **read['arguments'])
    assert old['revision'] == before['revision'] and old['nodes'][0]['config']['inputs'][0]['required']
    assert old['current_draft']['revision'] == changed['revision'] and old['current_draft']['changed_since_run']
    assert 'edit_base' not in old
    complete = call(client, base, 'workflow_run', action='inspect', task_id=failed['id'], view='full')
    from agent_platform.workflow_models import ApplicationSnapshot
    assert ApplicationSnapshot.model_validate(complete['workflow_snapshot']['snapshot']) == ApplicationSnapshot.model_validate(before['snapshot'])
    assert old['snapshot_source'] == 'original_run'
    other = client.post('/api/v1/projects', json={'name': 'Foreign'}).json()['id']
    response = client.post('/api/v1/projects/' + other + '/agent-tools', json={'name': read['tool'], 'arguments': read['arguments']})
    assert response.status_code == 404


def test_diagnostic_reads_failed_node_and_only_neighbor_output(configured):
    client, pid, base, before = fixture(configured)
    graph(client, pid, [node('start', 'start'), node('large', 'end', outputs={'unused': 'private-bulk' * 5000}),
        node('bad', 'variable_assigner', assignments={'quantity': {'$formula': '1 / 0'}}),
        node('end', 'end', outputs={'quantity': ref('bad', 'quantity')})],
        [edge('start', 'large'), edge('start', 'bad'), edge('bad', 'end')])
    failed = call(client, base, 'workflow_run', action='start')
    diagnosis = call(client, base, 'workflow_run', action='inspect', task_id=failed['id'], view='diagnostic')
    assert diagnosis['status'] == 'failed' and diagnosis['failure_stage'] == 'node_execution', (diagnosis['error'], diagnosis['run_id'], diagnosis['node_errors'])
    assert diagnosis['node_errors'][-1]['node_id'] == 'bad'
    assert [n['id'] for n in diagnosis['nodes']] == ['bad']
    assert 'private-bulk' not in str(diagnosis)
    assert set(diagnosis['upstream_outputs']) <= {'start'}


def test_focused_nested_read_and_copy_edit_preserve_surrounding_graph(configured):
    client, pid, base, before = fixture(configured)
    services = configured[1].state.services
    async def setup():
        from agent_platform.workflow_models import NodeSpec
        draft = await services.workflow_store.get_draft(pid)
        snapshot = draft['snapshot']
        snapshot.workflow.nodes.append(NodeSpec(id='loop', type='loop', title='Loop', config={
            'max_iterations': 1, 'break_value': True, 'break_condition': {'value': True, 'expected': True},
            'output_node_id': 'neighbor', 'workflow': {
            'nodes': [node('inside', 'start'), node('neighbor', 'end')],
            'edges': [edge('inside', 'neighbor')]}}))
        await services.workflow_store.save_draft(pid, snapshot, expected_revision=draft['revision'], idempotency_key='nested')
    client.portal.call(setup)
    read = call(client, base, 'workflow_draft', view='nodes', node_ids=['inside'])
    assert [n['id'] for n in read['nodes']] == ['inside']
    assert read['connections'][0]['scope'] == ['loop']
    assert read['connections'][0]['neighbors'][0]['title'] == 'neighbor'
    before = client.get('/api/v1/applications/' + pid + '/draft').json()
    copied = call(client, base, 'project_workflows', **copy_args(pid, before, node_updates={'inside': {'title': 'Updated'}}))
    changed = call(client, base, 'workflow_draft', workflow_id=copied['id'], view='nodes', node_ids=['inside'])
    assert changed['nodes'][0]['title'] == 'Updated'
    from agent_platform.workflow_models import EdgeSpec
    assert changed['connections'][0]['scope'] == read['connections'][0]['scope']
    assert changed['connections'][0]['neighbors'] == read['connections'][0]['neighbors']
    assert [EdgeSpec.model_validate(e) for e in changed['connections'][0]['edges']] == [EdgeSpec.model_validate(e) for e in read['connections'][0]['edges']]
    assert copied['changes']['edges_changed'] is False
    assert client.get('/api/v1/applications/' + pid + '/draft').json() == before
    edited = call(client, base, 'workflow_draft', workflow_id=copied['id'], operation={
        'expected_revision': 0, 'idempotency_key': 'inner-edges', 'op': 'update_node',
        'data': {'node_id': 'loop', 'changes': {'config': {'workflow': {'edges': []}}}, 'merge_config': True}})
    assert edited['changes']['edges_changed'] is True


def test_nested_failure_uses_failed_iteration_inputs_and_outputs(configured):
    client, pid, base, before = fixture(configured)
    nested = {'nodes': [node('inner-start', 'start'),
        node('shared', 'variable_assigner', assignments={'quantity': ref('$inputs', 'item')}),
        node('bad', 'variable_assigner', assignments={'quantity': {'$formula': '1 / 0'}}),
        node('inner-end', 'end', outputs={'quantity': ref('bad', 'quantity')})],
        'edges': [edge('inner-start', 'shared'), edge('shared', 'bad'), edge('bad', 'inner-end')]}
    graph(client, pid, [node('start', 'start'),
        node('shared', 'variable_assigner', assignments={'quantity': 999}),
        node('each', 'loop', workflow=nested, variables={'item': 4}, output_node_id='inner-end',
             max_iterations=1, break_value=True, break_condition={'value': True, 'expected': True}),
        node('end', 'end', outputs={})],
        [edge('start', 'shared'), edge('shared', 'each'), edge('each', 'end')])
    failed = call(client, base, 'workflow_run', action='start')
    diagnosis = call(client, base, 'workflow_run', action='inspect', task_id=failed['id'], view='diagnostic')
    assert diagnosis['status'] == 'failed' and diagnosis['failure_stage'] == 'node_execution', diagnosis['error']
    assert [n['id'] for n in diagnosis['nodes']] == ['bad']
    assert diagnosis['inputs_scope'] == 'nested_graph' and diagnosis['execution_scope'] == 'each[0].'
    assert diagnosis['inputs']['item'] == 4
    assert diagnosis['upstream_outputs']['shared']['output']['quantity'] == 4


@pytest.mark.parametrize('unfinished', [False, True])
def test_copy_structure_check_allows_unbound_resources_and_incomplete_drafts(configured, unfinished):
    client, pid, base, _ = fixture(configured)
    graph(client, pid, [node('start', 'start'),
        node('predict', 'model_predict', model_ref='', dataset_id=''), node('end', 'end')],
        [] if unfinished else [edge('start', 'predict'), edge('predict', 'end')])
    before = client.get('/api/v1/applications/' + pid + '/draft').json()
    copied = call(client, base, 'project_workflows', **copy_args(pid, before))
    assert copied['structure_check']['valid'] is (not unfinished)
    assert copied['structure_check']['runtime_checked'] is False
    if unfinished:
        assert any('unreachable' in error for error in copied['structure_check']['errors'])
    assert client.get('/api/v1/applications/' + copied['id'] + '/draft').status_code == 200
    assert client.get(base + '/tasks').json() == []
    assert client.get('/api/v1/applications/' + pid + '/draft').json() == before


def test_copy_check_failure_does_not_undo_saved_copy_or_run_business(configured, monkeypatch):
    client, pid, base, before = fixture(configured)
    def broken_check(snapshot):
        raise RuntimeError('checker temporarily unavailable')
    monkeypatch.setattr(configured[1].state.services.applications, 'validate_structure', broken_check)
    copied = call(client, base, 'project_workflows', **copy_args(pid, before))
    assert copied['structure_check']['valid'] is None
    assert copied['structure_check']['errors']
    assert client.get('/api/v1/applications/' + copied['id'] + '/draft').status_code == 200
    assert call(client, base, 'project_workflows', **copy_args(pid, before))['id'] == copied['id']
    assert client.get(base + '/tasks').json() == []
