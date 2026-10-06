"""Subscription integration tests use a protocol substitute, never provider HTTP."""
import asyncio
import json
import time
from uuid import uuid4

import pytest

from agent_platform.official_agent import ServiceConfig
from agent_platform.codex_app_server import CodexError
from tests.test_local_agents import configured as legacy_configured  # noqa: F401
from tests.test_users import platform, signup, project  # noqa: F401

ADMIN = {'Authorization': 'Bearer admin-boot'}


class FakeAgent:
    turns = []
    instructions = []
    hold = False
    def __init__(self, executable, runtime_dir, **kwargs):
        self.runtime_dir = runtime_dir
        self.options = kwargs
        self.thread_id = None
        self.turn_id = None
        self.process = None
    async def start(self, tools, instructions, thread_id=None):
        self.thread_id = thread_id or str(uuid4())
        self.tools = tools
        self.instructions.append(instructions)
        return self.thread_id
    async def turn(self, message, on_event, on_tool, **kwargs):
        self.turn_id = str(uuid4())
        self.turns.append((self.thread_id, message, self.options))
        while self.hold:
            await asyncio.sleep(.01)
        if self.tools and self.tools[0]['name'] != 'return_workflow':
            result = await on_tool('project_file', {'action': 'list'})
            text = json.dumps(result, ensure_ascii=False)
        else:
            text = json.dumps({'workflow': {'nodes': [
                {'id':'start','type':'start','title':'输入','config':{},'position':{'x':0,'y':0}},
                {'id':'end','type':'end','title':'输出','config':{'outputs':{'ok':True}},'position':{'x':200,'y':0}}
            ], 'edges':[{'id':'edge','source':'start','target':'end'}]}})
            assert [tool['name'] for tool in self.tools] == ['return_workflow']
            assert (await on_tool('return_workflow', json.loads(text)))['received'] is True
            text = '已提交。'
        await on_event('thread/tokenUsage/updated', {'tokenUsage': {'total': {'totalTokens': 100, 'inputTokens':80,'outputTokens':20}}})
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': text}})
        self.turn_id = None
        return {'status': 'completed'}
    async def steer(self, text):
        raise AssertionError('Duplicate request must not be steered')
    async def interrupt(self):
        self.turn_id = None
    async def close(self):
        pass


@pytest.fixture
def official(platform, monkeypatch):
    client, app = platform
    service = app.state.services.official_agent
    FakeAgent.turns = []; FakeAgent.instructions = []; FakeAgent.hold = False
    service.client_factory = FakeAgent
    service.save_config(ServiceConfig(enabled=True))
    async def inspect():
        service.rate_limits = {'rateLimits': {'primary': {'usedPercent': 20}, 'secondary': {'usedPercent':10}}}
        return {'error':'', 'dispatch_reason':service.quota_reason()}
    monkeypatch.setattr(service, 'inspect', inspect)
    return client, app, service


def enable(client, pid):
    base = '/api/v1/projects/' + pid
    assert client.put(base+'/capabilities',headers=ADMIN,json={'agent_modules_enabled':True}).status_code == 200
    result = client.put(base+'/assistant-source',headers=ADMIN,json={'allowed':True,'task':'official'})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize('account', [None, {}, {'type': 'apiKey'}])
def test_invalid_subscription_ends_wait_and_persists_repair_status(official, monkeypatch, account):
    from agent_platform.official_agent import OfficialAgent
    client, app, service = official
    _, headers = signup(client, '连接检查员工')
    pid = project(client, headers)
    enable(client, pid)
    calls = []

    class Control:
        async def request(self, method, params):
            calls.append(method)
            if method == 'account/read':
                return {'account': account}
            if method == 'model/list':
                return {'data': [{'model': 'gpt-5.6-luna', 'supportedReasoningEfforts': [{'reasoningEffort': 'max'}]}]}
            return {'rateLimits': {'primary': {'usedPercent': 0}}}

    async def transport():
        return Control()

    monkeypatch.setattr(service, 'transport', transport)
    monkeypatch.setattr(service, 'inspect', OfficialAgent.inspect.__get__(service))
    path = chat(client, pid, headers)
    client.post(path+'/messages', headers=headers, json={'message': '整理会议'}).raise_for_status()
    failed = wait(client, path, headers)
    assert failed['status'] == 'error'
    assert '认证失败' in failed['error']
    assert ('API Key' in failed['error']) == bool(account)
    if not account:
        assert '未检测到有效登录' in failed['error']
    assert failed['connection_status'] == 'blocked'
    assert calls == ['account/read']
    assert not FakeAgent.turns
    assert service.jobs()[0]['status'] == 'error'
    client.post(path+'/stop', headers=headers).raise_for_status()
    assert client.get(path, headers=headers).json()['connection_status'] == 'blocked'
    assert OfficialAgent(app.state.services).connection()['connection_status'] == 'blocked'
    assert client.get('/api/v1/projects/'+pid+'/assistant-source',headers=headers).json()['connection_status'] == 'blocked'
    account = {'type': 'chatgpt'}
    checked = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN).json()
    assert checked['connection_status'] == 'checked'
    client.post(path+'/messages', headers=headers, json={'message': '重新整理会议'}).raise_for_status()
    assert wait(client, path, headers)['status'] == 'idle'
    assert len(FakeAgent.turns) == 1


@pytest.mark.parametrize('raw,override,allowed', [(False,None,False), (True,None,True),
    (False,True,True), (True,False,False), (False,False,False)])
def test_official_egress_can_be_authorized_without_enabling_raw_api(official, tmp_path, raw, override, allowed):
    from agent_platform.codex_app_server import CodexAppServer, CodexError
    client, app, service = official
    app.state.services.settings.model_egress_enabled = raw
    app.state.services.settings.official_agent_egress_enabled = override
    service.client_factory = CodexAppServer
    connection = service.client('unused-project', tmp_path/'runtime')
    assert connection.allow_model_calls is allowed
    assert connection.subscription_only  # No API-key fallback.
    assert app.state.services.settings.model_egress_enabled is raw
    if not allowed:
        with pytest.raises(CodexError, match='模型出口已关闭'):
            asyncio.run(connection.turn('不会发出请求', None, None))
    assert connection.process is None  # This routing test never starts a provider.


def chat(client,pid,headers):
    base='/api/v1/projects/'+pid+'/conversations'
    response=client.post(base,headers=headers,json={})
    assert response.status_code==201,response.text
    return base+'/'+response.json()['id']


def wait(client,path,headers,statuses=('idle','error','interrupted')):
    for _ in range(300):
        state=client.get(path,headers=headers).json()
        if state['status'] in statuses:return state
        time.sleep(.02)
    raise AssertionError(state)


def test_two_employee_threads_tools_idempotency_and_raw_llm_stays_api(official):
    client,app,service=official
    alice,a=signup(client,'Alice');bob,b=signup(client,'Bob')
    pid=project(client,a)
    client.post('/api/v1/projects/'+pid+'/access-members',headers=a,json={'name':'Bob'})
    # API business model is independent and must not be overwritten.
    api_config={'provider':'api','base_url':'https://example.test/v1','api_key':'secret','model':'business'}
    assert client.put('/api/v1/projects/'+pid+'/agent-session',headers=a,json=api_config).status_code==200
    enable(client,pid)
    pa,pb=chat(client,pid,a),chat(client,pid,b)
    for path,h in ((pa,a),(pb,b)):
        result=client.post(path+'/messages',headers=h,json={'message':'查看文件','request_key':'same-id'})
        assert result.status_code==202,result.text
        state=wait(client,path,h)
        assert state['status']=='idle',state
        assert state['provider']=='official'
        assert any(e['kind']=='assistant' for e in state['events'])
        assert client.post(path+'/messages',headers=h,json={'message':'查看文件','request_key':'same-id'}).status_code==202
    assert len(FakeAgent.turns)==2
    assert FakeAgent.turns[0][0]!=FakeAgent.turns[1][0]
    assert all(t[2]['model']=='gpt-5.6-luna' and t[2]['thinking']=='max' for t in FakeAgent.turns)
    assert client.get(pa,headers=b).status_code==404
    assert client.get('/api/v1/admin/official-agent',headers=a).status_code==403
    assert len(service.jobs())==2
    assert all(j['tokens']==100 for j in service.jobs())
    assert app.state.services.local_agents.connections.load(pid).model=='business'


def test_capability_owner_and_revocation_during_queue(official):
    client,app,service=official
    _,a=signup(client,'Alice');_,b=signup(client,'Bob');pid=project(client,a)
    base='/api/v1/projects/'+pid
    assert client.put(base+'/assistant-source',headers=a,json={'allowed':True,'task':'official'}).status_code==403
    assert client.put(base+'/assistant-source',headers=ADMIN,json={'allowed':True,'task':'official'}).status_code==422
    enable(client,pid)
    client.post(base+'/access-members',headers=a,json={'name':'Bob'})
    pb=chat(client,pid,b)
    service.save_config(ServiceConfig(enabled=True,reserve_percent=100))
    assert client.post(pb+'/messages',headers=b,json={'message':'等待'}).status_code==202
    wait(client,pb,b,('queued',))
    assert not FakeAgent.turns
    bob_id=next(x['id'] for x in client.get(base+'/access-members',headers=a).json() if x['name']=='Bob')
    client.delete(base+'/access-members/'+bob_id,headers=a)
    for _ in range(100):
        if service.jobs()[0]['status']=='error':break
        time.sleep(.02)
    assert service.jobs()[0]['status']=='error'
    assert not FakeAgent.turns


def test_queued_cancel_and_duplicate_active_request(official):
    client,app,service=official
    _,a=signup(client,'Alice');pid=project(client,a);enable(client,pid)
    first,second=chat(client,pid,a),chat(client,pid,a)
    FakeAgent.hold=True
    body={'message':'继续','request_key':'once'}
    assert client.post(first+'/messages',headers=a,json=body).status_code==202
    wait(client,first,a,('running',))
    assert client.post(first+'/messages',headers=a,json=body).status_code==202
    assert client.post(second+'/messages',headers=a,json={'message':'第二个'}).status_code==202
    wait(client,second,a,('queued',))
    assert client.post(second+'/stop',headers=a).status_code==200
    assert client.get(first,headers=a).json()['status']=='running'
    FakeAgent.hold=False
    assert wait(client,first,a)['status']=='idle'
    assert len(FakeAgent.turns)==1
    assert {j['status'] for j in service.jobs()}=={'completed','interrupted'}


def test_generation_no_business_tools_and_api_credentials_not_required(official):
    client,app,service=official
    _,a=signup(client,'Alice');pid=project(client,a);enable(client,pid)
    path=chat(client,pid,a)
    response=client.post(path+'/workflow-generation',headers=a,json={'instruction':'生成返回 true 的流程','name':'新流程'})
    assert response.status_code==202,response.text
    job_id=response.json()['job_id']
    for _ in range(200):
        result=client.get('/api/v1/projects/'+pid+'/generation-jobs/'+job_id,headers=a).json()
        if result['status'] in {'completed','error'}:break
        time.sleep(.02)
    assert result['status']=='completed',result
    assert result['result']['draft']['revision']>=1
    assert service.jobs()[0]['kind']=='generation'
    assert len(FakeAgent.turns)==1
    assert '只生成图，不执行工作流或业务操作' in FakeAgent.instructions[0]
    assert not client.get('/api/v1/projects/'+pid+'/tasks',headers=a).json()


@pytest.mark.parametrize('kind', ['chat', 'generation'])
@pytest.mark.parametrize('limit', [None, 32000])
def test_optional_token_limit_preserves_accounting_and_explicit_limits(official, monkeypatch, kind, limit):
    client, app, service = official
    _, headers = signup(client, 'BudgetWorker')
    pid = project(client, headers); enable(client, pid)
    config = service.config().model_dump()
    config['max_tokens'] = limit
    saved = client.put('/api/v1/admin/official-agent', headers=ADMIN, json=config)
    assert saved.status_code == 200, saved.text
    assert client.get('/api/v1/admin/official-agent', headers=ADMIN).json()['config']['max_tokens'] == limit
    original = FakeAgent.turn
    async def turn(self, message, on_event, on_tool, **kwargs):
        self.interrupted = False
        async def event(method, params):
            if method == 'thread/tokenUsage/updated':
                params = {'tokenUsage': {'total': {'totalTokens': 65000, 'inputTokens': 60000, 'outputTokens': 5000}}}
            await on_event(method, params)
            await asyncio.sleep(0)
        result = await original(self, message, event, on_tool, **kwargs)
        return {'status': 'interrupted'} if self.interrupted else result
    async def interrupt(self):
        self.interrupted = True
        self.turn_id = None
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    monkeypatch.setattr(FakeAgent, 'interrupt', interrupt)
    path = chat(client, pid, headers)
    if kind == 'chat':
        assert client.post(path+'/messages', headers=headers, json={'message': '查看资料'}).status_code == 202
        result = wait(client, path, headers)
        assert result['status'] == ('idle' if limit is None else 'error'), result
    else:
        response = client.post(path+'/workflow-generation', headers=headers, json={'instruction': '创建空白工作流'})
        assert response.status_code == 202, response.text
        job = '/api/v1/projects/'+pid+'/generation-jobs/'+response.json()['job_id']
        for _ in range(200):
            result = client.get(job, headers=headers).json()
            if result['status'] in {'completed', 'error'}:
                break
            time.sleep(.02)
        assert result['status'] == ('completed' if limit is None else 'error'), result
    assert len(service.jobs()) == 1
    assert service.jobs()[0]['tokens'] == 65000


@pytest.mark.parametrize('limits,reserve,allowed', [({},0,False),({'primary':{'usedPercent':50}},50,False),
    ({'primary':{'usedPercent':49},'secondary':{'usedPercent':60}},50,False),
    ({'primary':{'usedPercent':99}},0,True),({'primary':{'usedPercent':100}},0,False)])
def test_reserve_all_windows_and_unknown(official,limits,reserve,allowed):
    _,_,service=official
    service.save_config(ServiceConfig(enabled=True,reserve_percent=reserve))
    service.rate_limits={'rateLimits':limits}
    assert (not service.quota_reason())==allowed


def test_live_capability_revocation_stops_tool_before_project_io(official):
    client,app,service=official
    _,a=signup(client,'Alice');pid=project(client,a);enable(client,pid);path=chat(client,pid,a)
    FakeAgent.hold=True
    client.post(path+'/messages',headers=a,json={'message':'列出资料'})
    wait(client,path,a,('running',))
    client.put('/api/v1/projects/'+pid+'/capabilities',headers=ADMIN,json={'agent_modules_enabled':False})
    FakeAgent.hold=False
    state=wait(client,path,a)
    assert state['status']=='error' and '未获准' in state['error']
    assert not any(e['kind']=='tool' for e in state['events'])


def test_restart_preserves_queued_only_and_does_not_restart_running(official):
    from agent_platform.conversation_scope import conversation_scope
    from agent_platform.project_store import connect
    client,app,service=official
    alice,a=signup(client,'Alice');pid=project(client,a);enable(client,pid);path=chat(client,pid,a)
    cid=path.rsplit('/',1)[-1]
    manager=app.state.services.local_agents
    with conversation_scope(pid,cid):
        state=manager.load(pid)
        state.update(request_id='queued-request',phase='coordinate',status='queued',conversation_enabled=True)
        manager.save(pid,state)
        manager.event(pid,'user','读取项目文件')
    with connect(service.db) as db:
        for ident,status in [('queued-request','queued'),('old-running','running')]:
            db.execute('INSERT INTO official_agent_jobs(id,project_id,conversation_id,user_id,kind,status,created) VALUES(?,?,?,?,?,?,?)',
                       (ident,pid,cid,alice['user']['id'],'chat',status,time.time()))
    client.portal.call(service.initialize)
    client.portal.call(service.recover)
    state=wait(client,path,a)
    assert state['status']=='idle',state
    assert len(FakeAgent.turns)==1
    jobs={j['id']:j for j in service.jobs()}
    assert jobs['queued-request']['status']=='completed'
    assert jobs['old-running']['status']=='interrupted'


def test_async_generation_queued_refresh_stop_and_private_result(official):
    client,app,service=official
    _,a=signup(client,'Alice');_,b=signup(client,'Bob');pid=project(client,a);enable(client,pid)
    client.post('/api/v1/projects/'+pid+'/access-members',headers=a,json={'name':'Bob'})
    path=chat(client,pid,a)
    service.save_config(ServiceConfig(enabled=True,reserve_percent=100))
    body={'instruction':'生成工作流','request_key':'generation-once'}
    response=client.post(path+'/workflow-generation',headers=a,json=body)
    assert response.status_code==202
    job_id=response.json()['job_id'];job='/api/v1/projects/'+pid+'/generation-jobs/'+job_id
    assert client.post(path+'/workflow-generation',headers=a,json=body).json()['job_id']==job_id
    assert client.get(job,headers=b).status_code==404
    assert client.get(job,headers=a).json()['status']=='queued'
    assert client.post(job+'/stop',headers=a).json()['status']=='interrupted'
    assert not FakeAgent.turns
    assert len(service.jobs())==1


@pytest.mark.parametrize('explicit_wait,short_wait,expected_turns', [
    (None, 15, 1), (True, 15, 1), (False, 15, 2), (None, 0, 2),
])
def test_short_workflow_returns_result_without_a_model_poll_and_long_work_resumes(
        official, monkeypatch, explicit_wait, short_wait, expected_turns):
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client, 'Worker')
    pid = project(client, headers); enable(client, pid)
    client.headers.update(headers)
    graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'answer': 42})], [edge('start', 'end')])
    monkeypatch.setattr('agent_platform.official_agent.SHORT_COMPUTE_WAIT_SECONDS', short_wait)
    run = app.state.services.projects._run
    async def delayed(task):
        await asyncio.sleep(.15)
        return await run(task)
    monkeypatch.setattr(app.state.services.projects, '_run', delayed)
    observations = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        context = json.loads(message)
        if not observations:
            arguments = {'action': 'start', 'request_key': 'one-computation'}
            if explicit_wait is not None:
                arguments['wait'] = explicit_wait
            result = await on_tool('workflow_run', arguments)
        else:
            result = context['recent_results'][0]
        observations.append(result)
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': result['status']}})
        return {'status': 'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    path = chat(client, pid, headers)
    assert client.post(path+'/messages', json={'message': '运行已有工作流', 'request_key': 'employee-once'}).status_code == 202
    state = wait(client, path, headers)
    assert state['status'] == 'idle', state
    assert len(observations) == expected_turns
    assert observations[-1]['outputs'] == {'answer': 42}
    if expected_turns == 2:
        assert observations[0]['status'] in {'queued', 'running'}
        assert observations[0]['id'] == observations[-1]['id']
    tasks = client.get('/api/v1/projects/'+pid+'/tasks').json()
    assert len(tasks) == 1 and tasks[0]['status'] == 'succeeded'
    assert len(service.jobs()) == 1


def test_stop_during_short_tool_wait_cancels_the_same_computation(official, monkeypatch):
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client, 'StopWorker')
    pid = project(client, headers); enable(client, pid)
    client.headers.update(headers)
    graph(client, pid, [node('start', 'start'), node('end', 'end', outputs={'answer': 42})], [edge('start', 'end')])
    run = app.state.services.projects._run
    async def delayed(task):
        await asyncio.sleep(30)
        return await run(task)
    monkeypatch.setattr(app.state.services.projects, '_run', delayed)
    calls = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        calls.append(message)
        await on_tool('workflow_run', {'action': 'start', 'request_key': 'stopped-once'})
        return {'status': 'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    path = chat(client, pid, headers)
    assert client.post(path+'/messages', json={'message': '运行工作流'}).status_code == 202
    for _ in range(150):
        tasks = client.get('/api/v1/projects/'+pid+'/tasks').json()
        if tasks:
            break
        time.sleep(.01)
    assert len(tasks) == 1 and tasks[0]['status'] in {'queued', 'running'}
    assert client.post(path+'/stop').status_code == 200
    assert wait(client, path, headers)['status'] == 'interrupted'
    task = client.get('/api/v1/projects/'+pid+'/tasks/'+tasks[0]['id']).json()
    assert task['status'] == 'interrupted' and not task['outputs']
    assert len(calls) == 1 and service.jobs()[0]['status'] == 'interrupted'


@pytest.mark.parametrize('threshold', [None, 32768])
def test_context_threshold_reconnects_same_thread_without_task_token_limit(official, threshold):
    client, app, service = official
    _, headers = signup(client, 'ContextWorker')
    pid = project(client, headers); enable(client, pid)
    config = service.config().model_dump()
    config.update(max_tokens=None, auto_compact_token_limit=threshold)
    assert client.put('/api/v1/admin/official-agent', headers=ADMIN, json=config).status_code == 200
    path = chat(client, pid, headers)
    assert client.post(path+'/messages', headers=headers, json={'message':'查看文件'}).status_code == 202
    first = wait(client, path, headers)
    assert first['status'] == 'idle'
    thread = FakeAgent.turns[-1][0]
    assert FakeAgent.turns[-1][2]['auto_compact_token_limit'] == threshold
    # Mimic the native client marker used when closing only official sessions.
    for agent in app.state.services.local_agents.clients.values():
        agent.subscription_only = True
    config['auto_compact_token_limit'] = 65536 if threshold is None else None
    assert client.put('/api/v1/admin/official-agent', headers=ADMIN, json=config).status_code == 200
    assert client.post(path+'/messages', headers=headers, json={'message':'继续查看文件'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    assert FakeAgent.turns[-1][0] == thread
    assert FakeAgent.turns[-1][2]['auto_compact_token_limit'] == config['auto_compact_token_limit']
    assert service.config().max_tokens is None
    assert service.config().model == 'gpt-5.6-luna'
    assert service.config().thinking == 'max'
    assert not client.get('/api/v1/projects/'+pid+'/tasks', headers=headers).json()


def test_context_setting_rejects_mid_task_change_and_keeps_stop(official):
    client, app, service = official
    _, headers = signup(client, 'BusyContextWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    FakeAgent.hold = True
    assert client.post(path+'/messages', headers=headers, json={'message':'查看文件'}).status_code == 202
    wait(client, path, headers, ('running',))
    config = service.config().model_dump(); config['auto_compact_token_limit'] = 32768
    assert client.put('/api/v1/admin/official-agent', headers=ADMIN, json=config).status_code == 409
    assert service.config().auto_compact_token_limit is None
    assert client.post(path+'/stop', headers=headers).status_code == 200
    assert wait(client, path, headers)['status'] == 'interrupted'
    FakeAgent.hold = False
    assert client.put('/api/v1/admin/official-agent', headers=ADMIN, json=config).status_code == 200


def test_compaction_progress_is_visible_without_an_extra_turn(official, monkeypatch):
    client, app, service = official
    original = FakeAgent.turn
    async def turn(self, message, on_event, on_tool, **kwargs):
        for phase in ('started', 'completed'):
            await on_event('item/'+phase, {'item': {'id':'compact-one', 'type':'contextCompaction'}})
        return await original(self, message, on_event, on_tool, **kwargs)
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    _, headers = signup(client, 'CompactionWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    client.post(path+'/messages', headers=headers, json={'message':'继续'})
    state = wait(client, path, headers)
    assert state['status'] == 'idle'
    events = [e for e in client.get(path, headers=headers, params={'kind':'tools'}).json()['events']
              if e.get('operation') == 'context_compaction']
    assert [e['stage'] for e in events] == ['started', 'completed']
    assert all(e['request_id'] == state['request_id'] for e in events)
    assert events[0]['operation_id'] == events[1]['operation_id']
    assert any(a['title'] == '整理对话历史' for a in client.get(path, headers=headers,
               params={'kind':'activity'}).json()['events'])
    assert len(FakeAgent.turns) == 1


def test_stop_during_history_compaction_keeps_original_thread_for_manual_resume(official, monkeypatch):
    client, app, service = official
    original = FakeAgent.turn
    async def turn(self, message, on_event, on_tool, **kwargs):
        await on_event('item/started', {'item': {'id':'compact', 'type':'contextCompaction'}})
        result = await original(self, message, on_event, on_tool, **kwargs)
        await on_event('item/completed', {'item': {'id':'compact', 'type':'contextCompaction'}})
        return result
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    _, headers = signup(client, 'StopCompactionWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    FakeAgent.hold = True
    client.post(path+'/messages', headers=headers, json={'message':'继续原任务'})
    for _ in range(200):
        state = client.get(path, headers=headers).json()
        if (state.get('current_activity') or {}).get('title') == '整理对话历史': break
        time.sleep(.01)
    else: raise AssertionError('Compaction activity not shown')
    thread = FakeAgent.turns[0][0]
    assert client.post(path+'/stop', headers=headers).status_code == 200
    stopped = wait(client, path, headers)
    assert stopped['status'] == 'interrupted'
    assert stopped['current_activity']['status'] == 'interrupted'
    assert len(FakeAgent.turns) == 1
    FakeAgent.hold = False
    client.post(path+'/messages', headers=headers, json={'message':'手动继续'})
    assert wait(client, path, headers)['status'] == 'idle'
    assert FakeAgent.turns[-1][0] == thread
    assert len(FakeAgent.turns) == 2


def test_missing_rollout_recovers_same_conversation_with_context_and_preserves_business(official, monkeypatch):
    from agent_platform.conversation_scope import conversation_scope
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client, 'RecoverWorker')
    pid = project(client, headers); enable(client, pid)
    client.headers.update(headers)
    graph(client, pid, [node('start', 'start'),
        node('save', 'project_record', action='put', collection='requests', key='once',
             value={'done': True}, expected_revision=0),
        node('end', 'end', outputs={'answer': 42})], [edge('start', 'save'), edge('save', 'end')])
    path = chat(client, pid, headers)
    cid = path.rsplit('/', 1)[-1]
    contexts, starts, business = [], [], []
    original_turn, original_start = FakeAgent.turn, FakeAgent.start
    async def turn(self, message, on_event, on_tool, **kwargs):
        context = json.loads(message)
        contexts.append(context)
        if len(contexts) == 1:
            business.append(await on_tool('workflow_run', {'action': 'start', 'request_key': 'business-once'}))
        else:
            business.append(await on_tool('workflow_run', {'action': 'inspect', 'task_id': business[0]['id']}))
        result = await original_turn(self, message, on_event, on_tool, **kwargs)
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '原业务操作已完成'}})
        return result
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    assert client.post(path+'/messages', json={'message': '执行一次已有工作流'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    manager = app.state.services.local_agents
    with conversation_scope(pid, cid):
        before = manager.load(pid)
        old_thread = before['thread_id']
        before['official_total_tokens'] = 850000
        manager.save(pid, before)
        old_client = manager.clients.pop(manager.key(pid))
    client.portal.call(old_client.close)
    old_tasks = client.get('/api/v1/projects/'+pid+'/tasks').json()
    old_record = client.get('/api/v1/projects/'+pid+'/records/requests/once').json()
    assert len(old_tasks) == 1 and old_tasks[0]['status'] == 'succeeded'
    async def start(self, tools, instructions, thread_id=None):
        starts.append(thread_id)
        if thread_id == old_thread:
            raise CodexError('no rollout found for thread id ' + old_thread)
        return await original_start(self, tools, instructions, thread_id)
    monkeypatch.setattr(FakeAgent, 'start', start)
    assert client.post(path+'/messages', json={'message': '只解释现有结果，不要再次运行'}).status_code == 202
    state = wait(client, path, headers)
    assert state['status'] == 'idle', state
    with conversation_scope(pid, cid):
        after = manager.load(pid)
    assert starts == [old_thread, None]
    assert after['thread_id'] != old_thread and after['previous_threads'] == [old_thread]
    assert after['session_id'] == before['session_id'] and after['conversation_id'] == cid
    assert {event['id'] for event in before['events']} <= {event['id'] for event in after['events']}
    assert client.get('/api/v1/projects/'+pid+'/tasks').json() == old_tasks
    assert client.get('/api/v1/projects/'+pid+'/records/requests/once').json() == old_record
    assert len(FakeAgent.turns) == 2 and len(contexts) == 2
    recovered = contexts[1]
    assert recovered['project']['id'] == pid and recovered['workflows'][0]['id'] == pid
    assert {message['text'] for message in recovered['recent_project_messages']} >= {
        '执行一次已有工作流', '原业务操作已完成', '只解释现有结果，不要再次运行'}
    assert '恢复文件缺失' in recovered['instruction'] and '工具已升级' not in recovered['instruction']
    assert any(event['kind'] == 'status' and '恢复文件缺失' in event['text'] for event in after['events'])
    assert not after.get('context_handoff') and not after.get('context_handoff_reason')
    assert after['official_total_tokens'] == 100
    assert [job['tokens'] for job in service.jobs()] == [100, 100]
    assert app.state.services.settings.model_egress_enabled is False


@pytest.mark.parametrize('failure', [
    'no rollout found for thread id another-thread',
    'no rollout found for thread id {thread}: permission denied',
    'Codex 请求 thread/resume 长时间无响应',
    '官方智能体需要订阅账号登录，不能使用 API Key',
    '本机 Codex 连接已断开',
])
def test_other_resume_errors_do_not_replace_the_provider_thread(official, monkeypatch, failure):
    from agent_platform.conversation_scope import conversation_scope
    client, app, _ = official
    _, headers = signup(client, 'ResumeErrorWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    assert client.post(path+'/messages', headers=headers, json={'message': '读取文件'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    manager = app.state.services.local_agents
    with conversation_scope(pid, path.rsplit('/', 1)[-1]):
        before = manager.load(pid)
        old_client = manager.clients.pop(manager.key(pid))
    client.portal.call(old_client.close)
    attempts = []
    async def start(self, tools, instructions, thread_id=None):
        attempts.append(thread_id)
        raise CodexError(failure.format(thread=before['thread_id']))
    monkeypatch.setattr(FakeAgent, 'start', start)
    assert client.post(path+'/messages', headers=headers, json={'message': '继续'}).status_code == 202
    state = wait(client, path, headers)
    assert state['status'] == 'error' and state['error'] == failure.format(thread=before['thread_id'])
    assert attempts == [before['thread_id']] and len(FakeAgent.turns) == 1
    with conversation_scope(pid, path.rsplit('/', 1)[-1]):
        after = manager.load(pid)
    assert after['thread_id'] == before['thread_id'] and after['session_id'] == before['session_id']
    assert not after.get('previous_threads') and not after.get('context_handoff')
    assert {event['id'] for event in before['events']} <= {event['id'] for event in after['events']}


def test_missing_rollout_replacement_failure_keeps_original_thread_and_budget(official, monkeypatch):
    from agent_platform.conversation_scope import conversation_scope
    client, app, _ = official
    _, headers = signup(client, 'ReplacementErrorWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    assert client.post(path+'/messages', headers=headers, json={'message': '读取文件'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    manager = app.state.services.local_agents
    with conversation_scope(pid, path.rsplit('/', 1)[-1]):
        before = manager.load(pid)
        old_client = manager.clients.pop(manager.key(pid))
    client.portal.call(old_client.close)
    attempts = []
    async def start(self, tools, instructions, thread_id=None):
        attempts.append(thread_id)
        raise CodexError('no rollout found for thread id ' + thread_id if thread_id else '订阅登录已过期')
    monkeypatch.setattr(FakeAgent, 'start', start)
    assert client.post(path+'/messages', headers=headers, json={'message': '继续'}).status_code == 202
    state = wait(client, path, headers)
    assert state['status'] == 'error' and state['error'] == '订阅登录已过期'
    assert attempts == [before['thread_id'], None] and len(FakeAgent.turns) == 1
    with conversation_scope(pid, path.rsplit('/', 1)[-1]):
        after = manager.load(pid)
    assert after['thread_id'] == before['thread_id'] and after['session_id'] == before['session_id']
    assert after['official_total_tokens'] == before['official_total_tokens']
    assert not after.get('previous_threads') and not after.get('context_handoff')


def test_rollout_error_during_a_turn_is_not_retried(official, monkeypatch):
    client, _, _ = official
    _, headers = signup(client, 'TurnErrorWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    attempts = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        attempts.append(self.thread_id)
        raise CodexError('no rollout found for thread id ' + self.thread_id)
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    assert client.post(path+'/messages', headers=headers, json={'message': '读取文件'}).status_code == 202
    state = wait(client, path, headers)
    assert state['status'] == 'error' and len(attempts) == 1
    assert state['error'] == 'no rollout found for thread id ' + attempts[0]
    assert not any('恢复文件缺失' in event['text'] for event in state['events'])


def test_local_codex_missing_rollout_recovers_after_cli_relocation(legacy_configured, monkeypatch):
    import errno
    from tests.test_local_agents import ScriptedCodex, select, settled
    client, app, base, pid, _, _ = legacy_configured
    select(client, base)
    assert client.post(base+'/agent-session/messages', json={'message': '先读资料'}).status_code == 202
    before = settled(client, base)
    assert before['status'] == 'idle'
    manager = app.state.services.local_agents
    client.portal.call(manager.clients.pop(pid).close)
    attempts = []
    class RelocatedMissingCodex(ScriptedCodex):
        def __init__(self, executable, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executable = executable
        async def start(self, tools, instructions, thread_id=None):
            attempts.append((self.executable, thread_id))
            if self.executable == before['executable']:
                raise FileNotFoundError(errno.ENOENT, 'No such file or directory', self.executable)
            if thread_id:
                raise CodexError('no rollout found for thread id ' + thread_id)
            await super().start(tools, instructions, thread_id)
            self.thread_id = 'recovered-thread'
            return self.thread_id
    async def inspect(_):
        return {'path': '/test/updated-codex', 'version': 'codex-cli 0.154.0'}
    manager.client_factory = RelocatedMissingCodex
    monkeypatch.setattr('agent_platform.local_agents.inspect_executable', inspect)
    assert client.post(base+'/agent-session/messages', json={'message': '继续讨论现有需求'}).status_code == 202
    after = settled(client, base)
    assert after['status'] == 'idle', after
    assert attempts == [(before['executable'], before['thread_id']),
                        ('/test/updated-codex', before['thread_id']), ('/test/updated-codex', None)]
    assert after['thread_id'] == 'recovered-thread' and after['previous_threads'] == [before['thread_id']]
    assert after['session_id'] == before['session_id']
    assert {event['id'] for event in before['events']} <= {event['id'] for event in after['events']}
    assert '恢复文件缺失' in ScriptedCodex.contexts[-1]['instruction']
    assert '工具已升级' not in ScriptedCodex.contexts[-1]['instruction']
    assert any(message['text'] == '先读资料' for message in ScriptedCodex.contexts[-1]['recent_project_messages'])


@pytest.mark.parametrize('method', ['account/read', 'account/rateLimits/read'])
def test_explicit_auth_error_is_terminal_not_quota_wait(official, monkeypatch, method):
    from agent_platform.codex_app_server import CodexAuthenticationError
    from agent_platform.official_agent import OfficialAgent
    client, app, service = official
    _, headers = signup(client, '认证过期员工')
    pid = project(client, headers)
    enable(client, pid)
    class Control:
        async def request(self, name, params):
            if name == method:
                raise CodexAuthenticationError('账号登录已过期，请管理员重新连接')
            if name == 'account/read':
                return {'account': {'type':'chatgpt'}}
            return {'data':[{'model':'gpt-5.6-luna','supportedReasoningEfforts':[{'reasoningEffort':'max'}]}]}
    async def transport():
        return Control()
    monkeypatch.setattr(service, 'transport', transport)
    monkeypatch.setattr(service, 'inspect', OfficialAgent.inspect.__get__(service))
    path = chat(client, pid, headers)
    response = client.post(path+'/workflow-generation', headers=headers, json={'instruction':'创建空白工作流'})
    response.raise_for_status()
    job_path = '/api/v1/projects/'+pid+'/generation-jobs/'+response.json()['job_id']
    result = wait(client, job_path, headers)
    assert result['status'] == 'error'
    assert '登录已过期' in result['error']
    assert service.connection()['connection_status'] == 'blocked'
    assert not service.active_jobs()
    assert not FakeAgent.turns
