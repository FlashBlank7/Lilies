"""Subscription integration tests use a protocol substitute, never provider HTTP."""
import asyncio
import json
import time
from uuid import uuid4

import pytest

from agent_platform.official_agent import ServiceConfig
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
        if self.tools:
            result = await on_tool('project_file', {'action': 'list'})
            text = json.dumps(result, ensure_ascii=False)
        else:
            text = json.dumps({'workflow': {'nodes': [
                {'id':'start','type':'start','title':'输入','config':{},'position':{'x':0,'y':0}},
                {'id':'end','type':'end','title':'输出','config':{'outputs':{'ok':True}},'position':{'x':200,'y':0}}
            ], 'edges':[{'id':'edge','source':'start','target':'end'}]}})
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


def test_generation_no_tools_and_api_credentials_not_required(official):
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
    assert '只生成图，不调用工具' in FakeAgent.instructions[0]
    assert not client.get('/api/v1/projects/'+pid+'/tasks',headers=a).json()


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
