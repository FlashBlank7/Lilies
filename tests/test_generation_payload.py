"""Generation keeps its contracts and returns one final document with exact usage."""
import asyncio
import json
from types import SimpleNamespace
import pytest

from agent_platform.blocks import build_block_registry, DEFAULT_WORKFLOW_BLOCKS
from agent_platform.project_workflow_edit import generation_catalog
from tests.test_official_agent import official, FakeAgent, signup, project, enable, chat  # noqa: F401
from tests.test_users import platform  # noqa: F401


def test_catalog_shares_definitions_without_losing_block_contracts():
    blocks = [block for block in build_block_registry().list() if block.type in DEFAULT_WORKFLOW_BLOCKS]
    definitions, catalog = generation_catalog(blocks)
    for original, compact in zip(blocks, catalog):
        schema = compact['config_schema']
        combined = {**definitions, **schema.get('$defs', {})}
        assert all(combined[name] == value for name, value in original.config_schema.get('$defs', {}).items())
        assert {k: v for k, v in schema.items() if k != '$defs'} == {
            k: v for k, v in original.config_schema.items() if k != '$defs'}
        assert compact['type'] == original.type
        assert compact['description'] == original.description
        assert compact['input_ports'] == [p.model_dump(mode='json') for p in original.input_ports]
        assert compact['output_ports'] == [p.model_dump(mode='json') for p in original.output_ports]
    # The bulky workflow definitions used by loop and iteration are transmitted
    # once; their nodes/configuration remain fully available to generation.
    loop = next(row for row in catalog if row['type'] == 'loop')
    iteration = next(row for row in catalog if row['type'] == 'iteration')
    assert '$defs' not in loop['config_schema'] and '$defs' not in iteration['config_schema']
    assert 'WorkflowSpec' in definitions and 'NodeSpec' in definitions


def test_extension_with_conflicting_definition_keeps_local_semantics():
    def block(kind, definition):
        return SimpleNamespace(type=kind, description=kind, input_ports=[], output_ports=[],
            config_schema={'$ref': '#/$defs/Value', '$defs': {'Value': definition}})
    first, second = block('one', {'type': 'string'}), block('two', {'type': 'number'})
    definitions, catalog = generation_catalog([first, second])
    assert definitions == {'Value': {'type': 'string'}}
    assert catalog[1]['config_schema'] == second.config_schema
    assert first.config_schema['$defs'] == {'Value': {'type': 'string'}}


@pytest.mark.parametrize('known_usage', [True, False])
def test_official_generation_ignores_commentary_and_repeated_final_items(official, monkeypatch, known_usage):
    import time
    client, app, service = official
    _, headers = signup(client, 'Generation receiver')
    pid = project(client, headers)
    enable(client, pid)
    base = chat(client, pid, headers)
    seen = []
    document = {'workflow': {'nodes': [
        {'id': 's', 'type': 'start', 'title': '输入', 'config': {}},
        {'id': 'e', 'type': 'end', 'title': '输出', 'config': {'outputs': {'ok': True}}}],
        'edges': [{'id': 'se', 'source': 's', 'target': 'e'}]}}

    async def turn(self, message, on_event, on_tool, **kwargs):
        seen.append(json.loads(message))
        assert kwargs['finish_on_tool'] == 'return_workflow'
        assert [tool['name'] for tool in self.tools] == ['return_workflow']
        with pytest.raises(ValueError, match='不执行操作'):
            await on_tool('project_file', {'action': 'write', 'path': 'unwanted.txt', 'content': 'no'})
        with pytest.raises(ValueError, match='nodes 和 edges'):
            await on_tool('return_workflow', {'workflow': {'nodes': []}})
        assert (await on_tool('return_workflow', document))['received'] is True
        with pytest.raises(ValueError, match='已接收'):
            await on_tool('return_workflow', document)
        await on_event('item/completed', {'item': {
            'id': 'progress', 'type': 'agentMessage', 'phase': 'commentary', 'text': '正在准备流程。'}})
        # Commentary and even invalid JSON in final prose cannot corrupt the
        # submitted graph. Repeated delivery cannot create a second draft.
        final = {'id': 'final', 'type': 'agentMessage', 'phase': 'final_answer', 'text': '{invalid: final prose}'}
        await on_event('item/completed', {'item': final})
        await on_event('item/completed', {'item': final})
        # These are cumulative snapshots, not additive charge notifications.
        for total in ((100, 120, 120) if known_usage else ()):
            await on_event('thread/tokenUsage/updated', {'tokenUsage': {'total': {
                'totalTokens': total, 'inputTokens': total-20, 'cachedInputTokens': 60,
                'outputTokens': 20, 'reasoningOutputTokens': 10}}})
        return {'status': 'interrupted', 'result_received': True}

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    started = client.post(base+'/workflow-generation', headers=headers,
                          json={'instruction': '生成返回 true 的流程', 'name': '直接生成'})
    assert started.status_code == 202, started.text
    jid = started.json()['job_id']
    for _ in range(300):
        result = client.get(f'/api/v1/projects/{pid}/generation-jobs/{jid}', headers=headers).json()
        if result['status'] not in ('queued', 'running', 'waiting'):
            break
        time.sleep(.01)
    assert result['status'] == 'completed', result
    usage = result['result']['usage']
    if known_usage:
        assert (usage['input_tokens'], usage['output_tokens'], usage['cache_read_input_tokens'],
                usage['reasoning_tokens']) == (100, 20, 60, 10)
    else:
        assert usage is None
    assert service.job(jid)['tokens'] == (120 if known_usage else None)
    assert seen[0]['instruction'] == '生成返回 true 的流程'
    assert list(seen[0])[:2] == ['$defs', 'catalog']
    assert client.get(f'/api/v1/projects/{pid}/tasks', headers=headers).json() == []


@pytest.mark.parametrize('failure', ['missing', 'interrupted', 'invalid_reference', 'false_receipt', 'timeout'])
def test_generation_receiver_cannot_save_missing_incomplete_or_invalid_workflow(official, monkeypatch, failure):
    import time
    client, app, service = official
    _, headers = signup(client, 'Generation failure')
    pid = project(client, headers)
    enable(client, pid)
    base = chat(client, pid, headers)
    draft_path = f'/api/v1/applications/{pid}/draft'
    before = client.get(draft_path, headers=headers).json()

    async def turn(self, message, on_event, on_tool, **kwargs):
        assert kwargs['finish_on_tool'] == 'return_workflow'
        if failure not in {'missing', 'false_receipt'}:
            graph = json.loads(json.dumps(before['snapshot']['workflow']))
            if failure == 'invalid_reference':
                graph['edges'].append({'id': 'bad', 'source': 'missing-node', 'target': 'also-missing'})
            await on_tool('return_workflow', {'workflow': graph})
        if failure == 'timeout':
            raise TimeoutError('生成超时')
        # A complete-looking prose reply is never mistaken for a submission.
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': json.dumps(before['snapshot']['workflow'])}})
        return ({'status': 'interrupted', 'result_received': True}
                if failure in {'invalid_reference', 'false_receipt'} else
                {'status': 'interrupted' if failure == 'interrupted' else 'completed'})

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    started = client.post(base+'/workflow-generation', headers=headers, json={
        'instruction': '修改当前流程', 'workflow_id': pid, 'expected_revision': before['revision']})
    assert started.status_code == 202, started.text
    for _ in range(300):
        result = client.get(f'/api/v1/projects/{pid}/generation-jobs/'+started.json()['job_id'], headers=headers).json()
        if result['status'] not in ('queued', 'running', 'waiting'):
            break
        time.sleep(.01)
    assert result['status'] == 'error', result
    if failure == 'missing':
        assert '未提交工作流定义' in result['error']
    assert client.get(draft_path, headers=headers).json() == before
    assert client.get(f'/api/v1/projects/{pid}/tasks', headers=headers).json() == []


def test_user_stop_after_receiving_graph_does_not_save_it(official, monkeypatch):
    import threading
    client, app, service = official
    _, headers = signup(client, 'Stop generation')
    pid = project(client, headers); enable(client, pid)
    base = chat(client, pid, headers)
    draft_path = f'/api/v1/applications/{pid}/draft'
    before = client.get(draft_path, headers=headers).json()
    received = threading.Event()

    async def turn(self, message, on_event, on_tool, **kwargs):
        await on_tool('return_workflow', {'workflow': before['snapshot']['workflow']})
        received.set()
        # Native stop has not completed yet; the employee's stop must win.
        await asyncio.Event().wait()
        return {'status': 'interrupted', 'result_received': True}

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    response = client.post(base+'/workflow-generation', headers=headers, json={
        'instruction': '修改当前流程', 'workflow_id': pid, 'expected_revision': before['revision']})
    assert response.status_code == 202, response.text
    assert received.wait(3)
    job = f'/api/v1/projects/{pid}/generation-jobs/' + response.json()['job_id']
    stopped = client.post(job+'/stop', headers=headers)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()['status'] == 'interrupted'
    assert client.get(draft_path, headers=headers).json() == before
    assert client.get(f'/api/v1/projects/{pid}/tasks', headers=headers).json() == []
