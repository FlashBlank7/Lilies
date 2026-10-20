"""Saved Python edits are repairable without executing code or binding resources."""
import pytest

from tests.test_projects import configured, edge, graph, node  # noqa: F401
from tests.test_project_efficiency import batch, update
from tests.test_project_tool_help import prepare
from tests.test_project_workflow_copy import call, copy_args


BAD_CODE = "def main(inputs):\n    return {'total': 1)\n"
# Neither imports nor top-level statements may execute during a syntax check.
GOOD_CODE = "import dependency_not_installed\nraise RuntimeError('do not execute')\ndef main(inputs):\n    return inputs\n"


def workflow(code):
    return {'nodes': [node('start', 'start'), node('process', 'code', code=code),
        node('predict', 'model_predict', model_ref='', dataset_id=''), node('end', 'end')],
        'edges': [edge('start', 'process'), edge('process', 'predict'), edge('predict', 'end')]}


def assert_syntax_error(check, *, scope=()):
    assert check['valid'] is False and check['runtime_checked'] is False
    assert check['python_version']
    diagnostic, = check['diagnostics']
    assert diagnostic['node_id'] == 'process' and diagnostic['scope'] == list(scope)
    assert diagnostic['field'] == 'config.code' and diagnostic['error_type'] == 'SyntaxError'
    assert diagnostic['line'] == 2 and diagnostic['column'] > 0
    assert 'does not match' in diagnostic['message']
    assert diagnostic['source_line'].strip() == "return {'total': 1)"
    assert not diagnostic['source_line_truncated']
    assert any('process' in error and 'line 2' in error for error in check['errors'])


@pytest.mark.parametrize('mode', ['batch', 'operation', 'manual'])
def test_python_syntax_error_is_saved_and_repaired_in_place(configured, mode):
    client, _, pid, base = prepare(configured)
    before = call(client, base, 'workflow_draft')
    operation = {'op': 'replace_workflow', 'data': {'workflow': workflow(BAD_CODE)}}
    if mode == 'manual':
        response = client.put(base + f'/workflows/{pid}/draft', json={
            'expected_revision': before['revision'], 'workflow': workflow(BAD_CODE)})
        assert response.status_code == 200, response.text
        saved = response.json()
    elif mode == 'operation':
        saved = call(client, base, 'workflow_draft', operation={**operation,
            'expected_revision': before['revision'], 'idempotency_key': 'bad-code'})
    else:
        saved = call(client, base, 'workflow_draft', batch=batch(before, operation, key='bad-code'))
    assert_syntax_error(saved['structure_check'])
    current = client.get(f'/api/v1/applications/{pid}/draft').json()
    assert current['snapshot']['workflow']['nodes'][1]['config']['code'] == BAD_CODE
    assert saved['structure_check']['revision'] == current['revision'] == before['revision'] + 1
    assert saved['structure_check']['content_hash'] == current['content_hash']
    # The saved receipt already has the version and location needed for this edit.
    fixed = call(client, base, 'workflow_draft', batch=batch(saved['structure_check'],
        update('process', config={'code': GOOD_CODE}), key='repair-code'))
    assert fixed['structure_check']['valid'] is True
    assert fixed['structure_check']['diagnostics'] == []
    assert fixed['structure_check']['errors'] == []
    assert fixed['revision'] == current['revision'] + 1
    assert client.get(base + '/tasks').json() == []


def test_copy_reports_code_error_and_keeps_historical_check_on_retry(configured):
    client, _, pid, base = prepare(configured)
    flow = workflow(GOOD_CODE)
    graph(client, pid, flow['nodes'], flow['edges'])
    before = client.get(f'/api/v1/applications/{pid}/draft').json()
    args = copy_args(pid, before, node_updates={'process': {'config': {'code': BAD_CODE}}})
    copied = call(client, base, 'project_workflows', **args)
    assert_syntax_error(copied['structure_check'])
    assert copied['structure_check']['revision'] == copied['applied_revision'] == 0
    assert copied['structure_check']['content_hash'] == copied['applied_content_hash']
    checked = call(client, base, 'workflow_run', action='validate', workflow_id=copied['id'])
    assert checked['diagnostics'] == copied['structure_check']['diagnostics']
    fixed = call(client, base, 'workflow_draft', workflow_id=copied['id'], batch=batch(copied,
        update('process', config={'code': GOOD_CODE}), key='repair-code'))
    assert fixed['structure_check']['valid'] is True
    retried = call(client, base, 'project_workflows', **args)
    assert retried['structure_check'] == copied['structure_check']
    assert retried['revision'] == fixed['revision'] != retried['structure_check']['revision']
    assert client.get(f'/api/v1/applications/{pid}/draft').json() == before
    assert client.get(base + '/tasks').json() == []


@pytest.mark.parametrize('container', ['iteration', 'loop'])
def test_saved_nested_code_error_reports_scope_and_line(configured, container):
    client, _, pid, base = prepare(configured)
    inner = workflow(BAD_CODE)
    config = {'workflow': inner, 'output_node_id': 'end'}
    if container == 'iteration':
        config['items'] = [1]
    else:
        config.update(max_iterations=1, break_value=True, break_condition={'value': True, 'expected': True})
    flow = {'nodes': [node('start', 'start'), node('repeat', container, **config), node('end', 'end')],
            'edges': [edge('start', 'repeat'), edge('repeat', 'end')]}
    before = call(client, base, 'workflow_draft')
    saved = call(client, base, 'workflow_draft', batch=batch(before,
        {'op': 'replace_workflow', 'data': {'workflow': flow}}))
    assert_syntax_error(saved['structure_check'], scope=('repeat',))
    fixed = call(client, base, 'workflow_draft', batch=batch(saved,
        update('process', config={'code': GOOD_CODE}), key='repair-nested'))
    assert fixed['structure_check']['valid'] is True and fixed['structure_check']['diagnostics'] == []
    assert client.get(base + '/tasks').json() == []


def test_draft_retry_diagnostics_identify_current_version(configured):
    client, _, pid, base = prepare(configured)
    before = call(client, base, 'workflow_draft')
    edits = batch(before, {'op': 'replace_workflow', 'data': {'workflow': workflow(BAD_CODE)}})
    saved = call(client, base, 'workflow_draft', batch=edits)
    fixed = call(client, base, 'workflow_draft', batch=batch(saved,
        update('process', config={'code': GOOD_CODE}), key='repair-code'))
    replay = call(client, base, 'workflow_draft', batch=edits)
    assert replay['applied_revision'] == saved['revision']
    assert replay['revision'] == replay['structure_check']['revision'] == fixed['revision']
    assert replay['structure_check']['content_hash'] == fixed['content_hash']
    assert replay['structure_check']['valid'] is True and replay['changes_available'] is False
