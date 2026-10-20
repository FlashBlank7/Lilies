import json

from tests.test_projects import configured as configured  # noqa: F401
from tests.test_projects import edge, node, ref, settled, start
from tests.test_local_agents import settled as agent_settled


class ScriptedSession:
    async def start(self, tools, instructions, thread_id=None):
        names = {t['name'] for t in tools}
        assert {'project_workflows', 'project_records', 'project_task_result'} <= names
        return thread_id or 'project-test-thread'

    def __init__(self, *args, **kwargs):
        pass

    async def turn(self, message, on_event, on_tool, **kwargs):
        context = json.loads(message)
        if context['phase'] == 'discuss':
            await on_tool('project_file', {'action': 'list'})
            await on_tool('requirements_submit', {'understanding': '两条工作流共享申请记录。',
                'document': '# 平台测试需求\n申请校验后读取申请，主流程依次调用两条成员流程。'})
        elif context['phase'] == 'build':
            members = await on_tool('project_workflows', {'action': 'list'})
            main = members['id']
            first = await on_tool('project_workflows', {'action': 'create', 'name': '校验'})
            second = await on_tool('project_workflows', {'action': 'create', 'name': '读取申请'})
            fields = [{'name': 'request_id', 'type': 'string', 'required': True}]
            graphs = {
                first['id']: ([node('start', 'start', inputs=fields),
                    node('save', 'project_record', action='put', collection='requests', key=ref('$inputs', 'request_id'), value={'valid': True}),
                    node('end', 'end', outputs={'request_id': ref('$inputs', 'request_id')})],
                    [edge('start', 'save'), edge('save', 'end')]),
                second['id']: ([node('start', 'start', inputs=fields),
                    node('read', 'project_record', action='get', collection='requests', key=ref('$inputs', 'request_id')),
                    node('end', 'end', outputs={'valid': ref('read', 'value', 'valid')})],
                    [edge('start', 'read'), edge('read', 'end')]),
                main: ([node('start', 'start', inputs=fields),
                    node('check', 'tool', tool_name='workflow:'+first['id'], input={'request_id': ref('$inputs', 'request_id')}),
                    node('read', 'tool', tool_name='workflow:'+second['id'], input={'request_id': ref('check', 'output', 'request_id')}),
                    node('end', 'end', outputs={'valid': ref('read', 'output', 'valid')})],
                    [edge('start', 'check'), edge('check', 'read'), edge('read', 'end')]),
            }
            for workflow_id, (nodes, edges) in graphs.items():
                draft = await on_tool('workflow_draft', {'workflow_id': workflow_id})
                await on_tool('workflow_draft', {'workflow_id': workflow_id, 'operation': {
                    'op': 'replace_workflow', 'data': {'workflow': {'nodes': nodes, 'edges': edges}},
                    'expected_revision': draft['revision'], 'idempotency_key': 'create'}})
            result = await on_tool('workflow_run', {'action': 'start', 'inputs': {'request_id': 'build'}})
            assert result['status'] == 'succeeded' and result['outputs']['valid']
        else:
            task = context['business_task']
            result = await on_tool('workflow_run', {'action': 'start', 'inputs': task['inputs']})
            assert result['status'] == 'succeeded'
            await on_tool('project_task_result', {'status': 'succeeded', 'message': '处理成功', 'outputs': result['outputs']})
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '已完成本轮。'}})
        return {'status': 'completed'}

    async def close(self):
        pass


def test_project_session_discuss_build_and_operate_with_real_runtime(configured, monkeypatch):
    client, app, project, _ = configured
    base = '/api/v1/projects/'+project['id']
    monkeypatch.setattr('agent_platform.local_agents.ModelSession', ScriptedSession)
    assert client.put(base+'/agent-session', json={'provider': 'classic'}).status_code == 422
    assert client.put(base+'/agent-session', json={'provider': 'api', 'model': 'test', 'base_url': 'https://example.test/v1', 'api_key': 'test'}).status_code == 200
    assert client.post(base+'/agent-session/messages', json={'message': '分析'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert state['requirements']['source'] == 'lilies'
    assert client.post(base+'/requirements/confirm', json={'revision': state['requirements']['revision']}).status_code == 200
    assert client.post(base+'/agent-session/messages', json={'intent': 'build', 'message': '开始搭建'}).status_code == 202
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert len(client.get(base).json()['members']) == 3
    task = settled(client, base, start(client, base, 'business', mode='agent', inputs={'request_id': 'business'}))
    assert task['status'] == 'succeeded' and len(task['runs']) == 3, task
    assert client.get(base+'/records/requests/business').json()['value']['valid']
    agent_settled(client, base)
    other = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    for name, arguments in [('workflow_draft', {'workflow_id': other}),
                             ('project_file', {'action': 'write', 'path': 'solution/edit.py', 'content': 'x'}),
                             ('requirements_submit', {'understanding': 'edit', 'document': 'changed'})]:
        assert client.post(base+'/agent-tools', json={'name': name, 'arguments': arguments}).status_code == 422
    read = client.post(base+'/agent-tools', json={'name': 'requirements_submit', 'arguments': {'action': 'read'}})
    assert read.status_code == 200 and read.json()['document'].startswith('# 平台测试')
    manual = client.post(base+'/agent-tools', json={'name': 'block_catalog', 'arguments': {'tool_name': 'Bash'}})
    assert manual.status_code == 200 and 'command' in manual.json()['input_schema']['properties']


def test_continued_task_displays_latest_summary_when_agent_carries_previous_outputs(configured, monkeypatch):
    client, app, project, _ = configured
    base = '/api/v1/projects/' + project['id']

    class ContinuingSession(ScriptedSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            task = json.loads(message)['business_task']
            previous = task['outputs']
            resumed = bool(task['supplements'])
            outputs = ({**previous, 'plan_version': 2, 'previous_result': previous}
                       if resumed else {'plan_version': 1, 'details': {'resource': 'test-resource'}})
            await on_tool('project_task_result', {
                'status': 'succeeded' if resumed else 'waiting_input',
                'message': '方案 v2 已更新' if resumed else '方案 v1 等待补充',
                'outputs': outputs,
            })
            return {'status': 'completed'}

    async def inspect(_):
        return {'path': '/test/codex', 'version': 'codex-cli 0.153.4'}

    monkeypatch.setattr('agent_platform.local_agents.inspect_executable', inspect)
    monkeypatch.setattr('agent_platform.local_agents.ModelSession', ContinuingSession)
    assert client.put(base + '/agent-session', json={'provider': 'api', 'model': 'test', 'base_url': 'https://example.test/v1', 'api_key': 'test'}).status_code == 200
    pending = settled(client, base, start(client, base, 'continued-summary', mode='agent'))
    assert pending['status'] == 'waiting_input'
    assert pending['outputs']['message'] == '方案 v1 等待补充'
    assert agent_settled(client, base)['status'] == 'idle'

    task_path = base + '/tasks/' + pending['id']
    assert client.post(task_path + '/supplements', json={'message': '补充已到', 'inputs': {}}).status_code == 200
    assert client.post(task_path + '/resume', json={}).status_code == 202
    done = settled(client, base, pending)
    assert done['id'] == pending['id'] and done['status'] == 'succeeded'
    assert done['outputs']['message'] == '方案 v2 已更新'
    assert done['outputs']['plan_version'] == 2
    assert done['outputs']['details'] == {'resource': 'test-resource'}
    assert done['outputs']['previous_result'] == pending['outputs']
