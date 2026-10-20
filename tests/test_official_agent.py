"""Subscription integration tests use a protocol substitute, never provider HTTP."""
import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
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


@pytest.fixture
def resident_control(official, monkeypatch):
    from agent_platform.official_agent import OfficialAgent
    client, _, service = official
    controls = []
    state = {'account': {'type': 'chatgpt'}}

    class Control:
        def __init__(self, executable, runtime_dir, **options):
            self.account = state['account'] if controls else None
            self.process = SimpleNamespace(returncode=None)
            self.options = options
            self.calls = []
            self.closed = False
            controls.append(self)

        async def connect(self):
            pass

        async def request(self, method, params):
            self.calls.append(method)
            if method == 'account/read':
                return {'account': self.account}
            if method == 'model/list':
                return {'data': [{'model': 'gpt-5.6-luna', 'supportedReasoningEfforts': [{'reasoningEffort': 'max'}]}]}
            if method == 'account/rateLimits/read':
                return {'rateLimits': {'primary': {'usedPercent': 0}}}
            raise AssertionError('Connection checks must not start login, threads or turns')

        async def close(self):
            self.closed = True
            self.process.returncode = 0

    async def executable(_):
        return {'path': '/test/codex', 'version': 'codex-cli 0.154.0'}

    def factory(executable, runtime_dir, **options):
        if runtime_dir == service.root / 'account':
            return Control(executable, runtime_dir, **options)
        return FakeAgent(executable, runtime_dir, **options)

    service.control = Control('/test/codex', service.root / 'account')
    service.client_factory = factory
    monkeypatch.setattr('agent_platform.official_agent.inspect_executable', executable)
    monkeypatch.setattr(service, 'inspect', OfficialAgent.inspect.__get__(service))
    return client, service, controls, state


def test_admin_refresh_replaces_a_live_control_with_an_empty_account(resident_control):
    client, service, controls, _ = resident_control
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    response.raise_for_status()
    assert response.json()['connection_status'] == 'checked'
    assert len(controls) == 2 and controls[0].closed
    assert service.control is controls[1] and not controls[1].closed
    assert controls[1].options['auth_file'] == service.auth_file
    assert not service.auth_file.exists()  # Checks never copy personal credentials.
    assert controls[0].calls == ['account/read']
    assert controls[1].calls == ['account/read', 'model/list', 'account/rateLimits/read']


@pytest.mark.parametrize('account', [None, {}, {'type': 'apiKey'}])
def test_admin_refresh_does_not_loop_when_replacement_has_no_subscription(resident_control, account):
    client, service, controls, state = resident_control
    state['account'] = account
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    response.raise_for_status()
    assert response.json()['connection_status'] == 'blocked'
    assert len(controls) == 2 and controls[0].closed and not controls[1].closed
    assert controls[1].calls == ['account/read']
    assert not service.auth_file.exists()


@pytest.mark.parametrize('status', ['queued', 'running', 'waiting'])
def test_admin_refresh_preserves_control_while_jobs_are_active(resident_control, status):
    from agent_platform.project_store import connect
    client, service, controls, _ = resident_control
    with connect(service.db) as db:
        db.execute('INSERT INTO official_agent_jobs(id,project_id,conversation_id,user_id,kind,status,created) '
                   'VALUES(?,?,?,?,?,?,?)', ('active', 'project', 'conversation', 'user', 'chat', status, time.time()))
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    response.raise_for_status()
    assert response.json()['connection_status'] == 'blocked'
    assert len(controls) == 1 and not controls[0].closed
    assert service.job('active')['status'] == status


@pytest.mark.parametrize('login', [
    {'loginId': 'browser-login', 'authUrl': 'https://login.invalid'},
    {'verificationUrl': 'https://login.invalid', 'userCode': 'test-device-code'},
])
def test_admin_refresh_preserves_pending_login_until_subscription_is_verified(resident_control, login):
    client, service, controls, _ = resident_control
    service.login = login
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    response.raise_for_status()
    assert response.json()['login'] == login and service.login == login
    assert len(controls) == 1 and not controls[0].closed
    controls[0].account = {'type': 'chatgpt'}
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    response.raise_for_status()
    assert response.json()['connection_status'] == 'checked'
    assert service.login is None and response.json()['login'] is None
    assert len(controls) == 1 and not controls[0].closed


@pytest.mark.parametrize('method', ['model/list', 'account/rateLimits/read'])
def test_failed_metadata_check_keeps_pending_login_protected(resident_control, method):
    from agent_platform.codex_app_server import CodexAuthenticationError
    client, service, controls, _ = resident_control
    login = service.login = {'loginId': 'pending', 'authUrl': 'https://login.invalid'}
    controls[0].account = {'type': 'chatgpt'}
    request = controls[0].request

    async def expired(name, params):
        if name == method:
            raise CodexAuthenticationError('登录尚未完成')
        return await request(name, params)

    controls[0].request = expired
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    assert response.status_code == 422
    assert service.login == login
    controls[0].account = None
    response = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    response.raise_for_status()
    assert response.json()['login'] == login
    assert len(controls) == 1 and not controls[0].closed


def test_employee_checks_never_replace_the_resident_control(resident_control):
    client, service, controls, _ = resident_control
    _, headers = signup(client, '连接失效员工')
    pid = project(client, headers)
    enable(client, pid)
    path = chat(client, pid, headers)
    for _ in range(2):
        client.post(path + '/messages', headers=headers, json={'message': '整理会议'}).raise_for_status()
        failed = wait(client, path, headers)
        assert failed['status'] == 'error' and failed['connection_status'] == 'blocked', failed.get('error')
    assert len(controls) == 1 and not controls[0].closed
    assert controls[0].calls == ['account/read']  # Later employee requests see the saved failure.
    assert not FakeAgent.turns
    response = client.get('/api/v1/admin/official-agent', headers=ADMIN)
    response.raise_for_status()
    assert len(controls) == 1 and not controls[0].closed


@pytest.mark.parametrize('account', [None, {'type': 'chatgpt'}])
def test_concurrent_admin_checks_replace_the_observed_control_only_once(resident_control, account):
    client, service, controls, state = resident_control
    state['account'] = account

    async def check_concurrently():
        entered = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()
        request = controls[0].request

        async def held_request(method, params):
            entered.set()
            await release.wait()
            return await request(method, params)

        async def second_check():
            second_started.set()
            return await service.check_connection()

        controls[0].request = held_request
        first = asyncio.create_task(service.check_connection())
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(second_check())
        await asyncio.wait_for(second_started.wait(), 2)
        assert not controls[0].closed
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, second), 2)

    results = client.portal.call(check_concurrently)
    assert [result['connection_status'] for result in results] == ['checked' if account else 'blocked'] * 2
    assert len(controls) == 2 and controls[0].closed and not controls[1].closed


def test_admin_refresh_waits_for_login_start_without_closing_its_control(resident_control):
    client, service, controls, state = resident_control
    state['account'] = None
    login = {'loginId': 'pending', 'authUrl': 'https://login.invalid'}

    async def login_and_check():
        entered = asyncio.Event()
        release = asyncio.Event()
        checking = asyncio.Event()
        factory = service.client_factory

        def login_factory(*args, **options):
            control = factory(*args, **options)
            request = control.request

            async def login_request(method, params):
                if method == 'account/login/start':
                    entered.set()
                    await release.wait()
                    return login
                return await request(method, params)

            control.request = login_request
            return control

        async def check():
            checking.set()
            return await service.check_connection()

        service.client_factory = login_factory
        started = asyncio.create_task(service.connect_account('browser'))
        await asyncio.wait_for(entered.wait(), 2)
        checked = asyncio.create_task(check())
        await asyncio.wait_for(checking.wait(), 2)
        assert len(controls) == 2 and not controls[1].closed
        release.set()
        return await asyncio.wait_for(asyncio.gather(started, checked), 2)

    started, checked = client.portal.call(login_and_check)
    assert started['login'] == checked['login'] == service.login == login
    assert len(controls) == 2 and not controls[1].closed
    assert not service.auth_file.exists()


def test_disconnect_still_logs_out_and_closes_the_control(resident_control):
    client, service, controls, _ = resident_control
    request = controls[0].request
    logged_out = []

    async def logout(method, params):
        if method == 'account/logout':
            logged_out.append(True)
            return {}
        return await request(method, params)

    controls[0].request = logout
    response = client.post('/api/v1/admin/official-agent/disconnect', headers=ADMIN)
    response.raise_for_status()
    assert response.json() == {'ok': True}
    assert logged_out == [True]
    assert service.control is None and controls[0].closed
    assert not service.config().enabled


def enable(client, pid):
    base = '/api/v1/projects/' + pid
    assert client.put(base+'/capabilities',headers=ADMIN,json={'agent_modules_enabled':True}).status_code == 200
    result = client.put(base+'/assistant-source',headers=ADMIN,json={'allowed':True,'task':'official'})
    assert result.status_code == 200, result.text


@pytest.mark.parametrize('fault', ['closed_input', 'invalid_json', 'exited', 'healthy'])
def test_account_probe_recovers_stale_control_before_dispatching_new_work(platform, tmp_path, fault):
    from agent_platform.codex_app_server import CodexAppServer
    client, app = platform
    service = app.state.services.official_agent
    trace = tmp_path/'account-protocol.jsonl'
    executable = tmp_path/'account_fixture'
    executable.write_text(f'#!{sys.executable}\ntrace={str(trace)!r}\nfault={fault!r}\n' + '''
import json,os,sys,time
from pathlib import Path
if '--version' in sys.argv:
    print('codex-cli 0.162.0');sys.exit(0)
def emit(value):print(json.dumps(value),flush=True)
def record(method):
    with open(trace,'a') as stream:stream.write(json.dumps({'pid':os.getpid(),'method':method})+'\\n')
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method:record(method)
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method=='model/list':emit({'id':m['id'],'result':{'data':[{'model':'protocol-fixture','supportedReasoningEfforts':[{'reasoningEffort':'max'}]}]}})
    elif method=='account/rateLimits/read':emit({'id':m['id'],'result':{'rateLimits':{'primary':{'usedPercent':10}}}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'id':'write','method':'item/tool/call','params':{'threadId':'child','turnId':'child-turn',
            'tool':'project_file','arguments':{'action':'write','path':'results/result.txt','content':'delivered once'}}})
    elif m.get('id')=='write':
        assert m['result']['success'];record('tool_result')
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':'child-turn','status':'completed'}}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
    elif method=='thread/read':emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root','status':{'type':'idle'}}}})
    elif method=='fixture/fault':
        emit({'id':m['id'],'result':{}})
        if fault=='exited':sys.exit(0)
        print('invalid json',flush=True)
if fault=='closed_input':
    Path('stdin-eof').write_text('closed input while process remains alive')
    time.sleep(60)
''')
    executable.chmod(0o700)
    service.client_factory = CodexAppServer
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), model='protocol-fixture', max_tokens=None))
    app.state.services.settings.official_agent_egress_enabled = True  # Local Python fixture only.
    # Exercise the real account probe rather than the fixture's overridden inspect.
    first = client.get('/api/v1/admin/official-agent?refresh=true', headers=ADMIN)
    assert first.status_code == 200 and not first.json()['error'], first.text
    original = service.control
    if fault == 'closed_input':
        async def close_input():
            original.process.stdin.close()
            await original.process.stdin.wait_closed()
        client.portal.call(close_input)
        for _ in range(300):
            if (original.runtime_dir/'empty-workspace/stdin-eof').exists():break
            time.sleep(.01)
        else:raise AssertionError('Control process did not observe input EOF')
        assert original.process.returncode is None and not original.reader.done() and not original.closing
    elif fault != 'healthy':
        client.portal.call(original.request, 'fixture/fault', {})
        async def stopped_reader():await asyncio.wait_for(asyncio.shield(original.reader), 3)
        client.portal.call(stopped_reader)
        assert (original.process.returncode is None) == (fault == 'invalid_json')
    _, headers = signup(client, 'ControlRecoveryWorker')
    pid = project(client, headers); enable(client, pid)
    path = chat(client, pid, headers)
    body = {'message':'通过子任务保存结果','request_key':'control-recovery'}
    try:
        assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
        state = wait(client, path, headers)
        assert state['status'] == 'idle', state
        job = service.job(state['request_id'])
        assert job['status'] == 'completed' and not job['error']
        assert (app.state.services.settings.workspace_root/pid/'results/result.txt').read_text() == 'delivered once'
        entries = [json.loads(line) for line in trace.read_text().splitlines()]
        assert sum(e['method']=='turn/start' for e in entries) == 1
        assert sum(e['method']=='tool_result' for e in entries) == 1
        assert sum(e['method']=='model/list' for e in entries) == 2
        assert (service.control is original) == (fault == 'healthy')
        if fault != 'healthy':assert original.process.returncode is not None
        assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
        assert service.jobs() == [job]
    finally:
        for row in service.jobs():
            if row['status'] in {'queued','running','waiting'}:
                assert client.post('/api/v1/admin/official-agent/jobs/'+row['id']+'/stop', headers=ADMIN).status_code == 200
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

    monkeypatch.setattr(service, '_transport', transport)
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
    assert not FakeAgent.instructions  # No provider thread before an execution slot.
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


def test_child_usage_counts_towards_same_budget_and_stops_followup_tools(official, monkeypatch):
    from agent_platform.conversation_scope import conversation_scope
    client, app, service = official
    _, headers = signup(client, 'ChildBudgetWorker')
    pid = project(client, headers); enable(client, pid)
    service.save_config(ServiceConfig(enabled=True, max_tokens=1000))
    path = chat(client, pid, headers)
    reached_after_limit = []

    async def turn(self, message, on_event, on_tool, **kwargs):
        for thread, total in [(self.thread_id,600),('child',700)]:
            await on_event('thread/tokenUsage/updated', {'threadId':thread,'turnId':thread+'-turn',
                'tokenUsage':{'total':{'totalTokens':total}},'usageDelta':total})
            await asyncio.sleep(0)
        reached_after_limit.append(True)
        await on_tool('project_file', {'action':'write','path':'results/late.txt','content':'must not happen'})
        return {'status':'completed'}

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    assert client.post(path+'/messages',headers=headers,json={'message':'启动子任务'}).status_code==202
    state=wait(client,path,headers)
    assert state['status']=='error' and '上限' in state['error']
    assert service.jobs()[0]['tokens']==1300
    assert not reached_after_limit
    assert not (app.state.services.settings.workspace_root/pid/'results/late.txt').exists()
    with conversation_scope(pid,path.rsplit('/',1)[-1]):
        assert app.state.services.local_agents.load(pid)['official_total_tokens']==600


@pytest.mark.parametrize('action', ['supplement', 'stop_resume'])
def test_native_children_drain_with_new_messages_and_independent_conversations(official, tmp_path, action):
    from agent_platform.codex_app_server import CodexAppServer
    client, app, service = official
    executable = tmp_path / 'protocol_fixture'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
from pathlib import Path
cwd=Path.cwd();root=cwd.parent.parent.name;child=root+'-child'
number=int(Path('number').read_text()) if Path('number').exists() else 0
active=False
def emit(value):print(json.dumps(value),flush=True)
def note(method,**params):emit({'method':method,'params':params})
def usage(thread,turn,total):note('thread/tokenUsage/updated',threadId=thread,turnId=turn,tokenUsage={'total':{'totalTokens':total}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method in ['thread/start','thread/resume']:
        emit({'id':m['id'],'result':{'thread':{'id':root}}})
        if method=='thread/resume':
            usage(root,'root-1',100);usage(child,'child-1',50)
    elif method=='turn/start':
        number+=1;Path('number').write_text(str(number));turn='root-'+str(number)
        context=json.loads(m['params']['input'][0]['text'])
        Path('context-'+str(number)+'.json').write_text(json.dumps(context))
        emit({'id':m['id'],'result':{'turn':{'id':turn}}})
        usage(root,turn,number*100)
        if number==1:
            active=True
            note('thread/started',thread={'id':child,'parentThreadId':root})
            note('turn/started',threadId=child,turn={'id':'child-1'})
            usage(child,'child-1',50)
            note('turn/completed',threadId=root,turn={'id':turn,'status':'completed'})
        else:
            emit({'id':'write','method':'item/tool/call','params':{'threadId':root,'turnId':turn,
                'tool':'project_file','arguments':{'action':'write','path':'results/'+root+'.json',
                'content':json.dumps(context.get('latest_messages') or [context['user_message']])}}})
    elif method=='thread/read':
        Path('draining').write_text('yes')
        if Path('release').exists():active=False
        emit({'id':m['id'],'result':{'thread':{'id':child,'parentThreadId':root,
            'status':{'type':'active','activeFlags':[]} if active else {'type':'idle'}}}})
    elif method=='turn/steer':emit({'id':m['id'],'error':{'code':-32600,'message':'No active turn to steer'}})
    elif m.get('id')=='write':
        assert m['result']['success']
        note('turn/completed',threadId=root,turn={'id':turn,'status':'completed'})
''')
    executable.chmod(0o700)
    service.client_factory = CodexAppServer
    app.state.services.settings.official_agent_egress_enabled = True  # Local fixture only.
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), concurrency=2, max_tokens=None))
    _, headers = signup(client, 'ConcurrentWorker')
    pid = project(client, headers); enable(client, pid)
    first, second = chat(client, pid, headers), chat(client, pid, headers)
    for path in (first, second):
        assert client.post(path+'/messages', headers=headers, json={'message':'启动子任务'}).status_code == 202
    manager = app.state.services.local_agents
    clients = {}
    for path in (first, second):
        key = pid + ':' + path.rsplit('/',1)[-1]
        for _ in range(200):
            connection = manager.clients.get(key)
            if connection and (connection.runtime_dir/'empty-workspace/draining').exists():
                clients[path] = connection
                break
            time.sleep(.02)
        else:
            raise AssertionError(client.get(path, headers=headers).json())
    assert all(client.get(p, headers=headers).json()['status']=='running' for p in (first, second))
    if action == 'stop_resume':
        assert client.post(first+'/stop', headers=headers).json()['status'] == 'interrupted'
        assert clients[first].process.returncode is not None
    response = client.post(first+'/messages', headers=headers, json={'message':'只给第一个会话保存补充结果'})
    assert response.status_code == 202, response.text
    assert client.get(second, headers=headers).json()['status'] == 'running'
    if action == 'supplement':
        time.sleep(.05)
        assert not (clients[first].runtime_dir/'empty-workspace/context-2.json').exists()
    for connection in clients.values():
        (connection.runtime_dir/'empty-workspace/release').touch()
    assert wait(client, first, headers)['status'] == 'idle'
    assert wait(client, second, headers)['status'] == 'idle'
    workspace = app.state.services.settings.workspace_root / pid
    assert json.loads((workspace/'results'/f'{clients[first].thread_id}.json').read_text()) == ['只给第一个会话保存补充结果']
    assert not (workspace/'results'/f'{clients[second].thread_id}.json').exists()
    first_jobs = [j for j in service.jobs() if j['conversation_id']==first.rsplit('/',1)[-1]]
    second_jobs = [j for j in service.jobs() if j['conversation_id']==second.rsplit('/',1)[-1]]
    assert len(second_jobs)==1 and second_jobs[0]['status']=='completed' and second_jobs[0]['tokens']==150
    assert sum(j['tokens'] for j in first_jobs)==250
    assert {j['status'] for j in first_jobs} == ({'completed','interrupted'} if action=='stop_resume' else {'completed'})


@pytest.mark.parametrize('failure', ['before_turn','budget_before_turn','event_save','dequeue_save','after_delivery'])
def test_pending_supplement_survives_preflight_failure_without_replaying_delivered_work(official,monkeypatch,failure):
    from agent_platform.conversation_scope import conversation_scope
    client,app,service=official
    started,release=threading.Event(),threading.Event()
    contexts=[];supplements=[]
    class Session(FakeAgent):
        async def turn(self,message,on_event,on_tool,**kwargs):
            context=json.loads(message);contexts.append(context)
            if len(contexts)==1:
                await on_tool('project_file',{'action':'write','path':'results/first.txt','content':'first result'})
                started.set()
                while not release.is_set():await asyncio.sleep(.01)
                if failure=='budget_before_turn':
                    service.update(service.jobs()[0]['id'],execution_seconds=service.config().max_seconds)
            else:
                for text in context.get('latest_messages',[]):
                    if text == '明确继续，保留已有结果':
                        continue
                    supplements.append(text)
                    await on_tool('project_file',{'action':'write','path':'results/supplement.txt','content':text})
                if failure=='after_delivery' and len(contexts)==2:raise CodexError('failed after delivered work')
            return {'status':'completed'}
    service.client_factory=Session
    _,headers=signup(client,'PendingPreflightWorker')
    pid=project(client,headers);enable(client,pid);path=chat(client,pid,headers)
    base='/api/v1/projects/'+pid
    manager=app.state.services.local_agents
    save=manager.save;injected=False
    def fail_preparation_save(project_id,state):
        nonlocal injected
        if started.is_set() and not injected:
            saving_event=(state.get('events') or [{}])[-1].get('kind')=='agent_turn_started'
            taking_pending=manager.load(project_id).get('pending_messages') and not state.get('pending_messages')
            if (failure=='event_save' and saving_event) or (failure=='dequeue_save' and taking_pending):
                injected=True
                raise OSError('simulated '+failure+' failure')
        return save(project_id,state)
    monkeypatch.setattr(manager,'save',fail_preparation_save)
    def pending():
        async def read():
            with conversation_scope(pid,path.rsplit('/',1)[-1]):return manager.load(pid).get('pending_messages',[])
        return client.portal.call(read)
    assert client.post(path+'/messages',headers=headers,json={'message':'先生成结果','request_key':'first'}).status_code==202
    try:
        assert started.wait(5)
        assert client.post(path+'/messages',headers=headers,json={'message':'补充内容只保存一次','request_key':'supplement'}).status_code==202
        assert pending()==['补充内容只保存一次']
        if failure=='before_turn':
            assert client.put(base+'/capabilities',headers=ADMIN,json={'agent_modules_enabled':False}).status_code==200
    finally:
        release.set()
    failed=wait(client,path,headers)
    assert failed['status']=='error'
    expected={'before_turn':'此项目未获准使用官方智能体','budget_before_turn':'已达到本任务模型执行时限',
              'event_save':'simulated event_save failure','dequeue_save':'simulated dequeue_save failure',
              'after_delivery':'failed after delivered work'}[failure]
    assert expected in failed['error']
    first_job=service.job(failed['request_id'])
    assert len(contexts)==(2 if failure=='after_delivery' else 1)
    if failure in {'event_save','dequeue_save'}:assert injected
    assert pending()==([] if failure=='after_delivery' else ['补充内容只保存一次'])
    assert client.put(base+'/capabilities',headers=ADMIN,json={'agent_modules_enabled':True}).status_code==200
    body={'message':'明确继续，保留已有结果','request_key':'resume'}
    assert client.post(path+'/messages',headers=headers,json=body).status_code==202
    completed=wait(client,path,headers)
    assert completed['status']=='idle' and supplements==['补充内容只保存一次']
    assert contexts[-1].get('latest_messages',[]) == ([] if failure=='after_delivery' else ['补充内容只保存一次',body['message']])
    assert client.post(path+'/messages',headers=headers,json=body).status_code==202
    assert not pending() and service.job(first_job['id'])==first_job and len(service.jobs())==2
    workspace=app.state.services.settings.workspace_root/pid
    assert (workspace/'results/first.txt').read_text()=='first result'
    assert (workspace/'results/supplement.txt').read_text()=='补充内容只保存一次'


@pytest.mark.parametrize('resume', [False, True], ids=['cancel-pending', 'continue-pending'])
def test_new_request_after_stop_orders_pending_inputs_before_current_instruction(official, resume):
    from agent_platform.conversation_scope import conversation_scope
    client, app, service = official
    started = threading.Event()
    contexts = []
    supplement = '执行补充计算一次'
    instruction = '继续执行尚未送出的补充' if resume else '只读取已有结果，取消未执行的补充'

    class Session(FakeAgent):
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message)
            contexts.append(context)
            if len(contexts) == 1:
                await on_tool('project_file', {'action': 'write', 'path': 'results/first.txt', 'content': 'first result'})
                started.set()
                await asyncio.Event().wait()
            else:
                # This scripted consumer makes the ordering contract observable;
                # it does not claim to test a real model's instruction following.
                inputs = context.get('latest_messages') or [context['user_message']]
                selected = inputs[-1]
                await on_tool('project_file', {'action': 'read', 'path': 'results/first.txt'})
                await on_tool('project_file', {'action': 'write', 'path': 'results/selected.txt', 'content': selected})
                if selected == supplement or selected == '继续执行尚未送出的补充':
                    await on_tool('project_file', {'action': 'write', 'path': 'results/supplement.txt', 'content': 'executed once'})
            return {'status': 'completed'}

    service.client_factory = Session
    _, headers = signup(client, 'PendingOrderWorker')
    pid = project(client, headers)
    enable(client, pid)
    path = chat(client, pid, headers)
    manager = app.state.services.local_agents

    async def pending():
        with conversation_scope(pid, path.rsplit('/', 1)[-1]):
            return manager.load(pid).get('pending_messages', [])

    assert client.post(path+'/messages', headers=headers, json={'message': '先生成结果', 'request_key': 'first'}).status_code == 202
    assert started.wait(5)
    assert client.post(path+'/messages', headers=headers, json={'message': supplement, 'request_key': 'supplement'}).status_code == 202
    assert client.portal.call(pending) == [supplement]
    stopped = client.post(path+'/stop', headers=headers).json()
    assert stopped['status'] == 'interrupted'
    old_job = service.job(stopped['request_id'])
    body = {'message': instruction, 'request_key': 'new-request'}
    assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    assert contexts[1]['latest_messages'] == [supplement, instruction]
    assert contexts[1]['user_message'] == instruction
    assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
    assert len(contexts) == 2 and not client.portal.call(pending)
    assert len(service.jobs()) == 2 and service.job(old_job['id']) == old_job
    workspace = app.state.services.settings.workspace_root / pid
    assert (workspace/'results/first.txt').read_text() == 'first result'
    assert (workspace/'results/selected.txt').read_text() == instruction
    assert (workspace/'results/supplement.txt').exists() == resume


@pytest.mark.parametrize('failure', ['resume_rejected','resume_stopped','resume_timeout','delivered_without_reply','closed_input_resume_rejected'])
def test_pending_supplement_waits_for_connection_recovery_but_never_replays_unknown_delivery(
        official,tmp_path,failure):
    from agent_platform.codex_app_server import CodexAppServer
    from agent_platform.conversation_scope import conversation_scope
    client,app,service=official
    trace=tmp_path/'calls.jsonl';repaired=tmp_path/'repaired'
    executable=tmp_path/'protocol_fixture'
    executable.write_text(f'#!{sys.executable}\nfailure={failure!r}\ntrace={str(trace)!r}\nrepaired={str(repaired)!r}\n'+'''
import json,sys,time
from pathlib import Path
active=False
def emit(value):print(json.dumps(value),flush=True)
def record(**value):
    with open(trace,'a') as stream:stream.write(json.dumps(value)+'\\n')
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method in {'thread/start','thread/resume'}:
        record(method=method)
        if method=='thread/resume' and not Path(repaired).exists():
            if failure in {'resume_rejected','closed_input_resume_rejected'}:
                emit({'id':m['id'],'error':{'code':-32000,'message':'native resume rejected'}});continue
            if failure in {'resume_stopped','resume_timeout'}:continue
        emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        context=json.loads(m['params']['input'][0]['text']);record(method=method,context=context)
        if failure=='delivered_without_reply' and context.get('latest_messages') and not Path(repaired).exists():sys.exit(0)
        emit({'id':m['id'],'result':{'turn':{'id':'turn'}}})
        if context['user_message']=='first' and not context.get('latest_messages'):
            emit({'id':'first','method':'item/tool/call','params':{'threadId':'root','turnId':'turn',
                'tool':'project_file','arguments':{'action':'write','path':'results/first.txt','content':'saved once'}}})
        elif context.get('latest_messages'):
            emit({'id':'supplement','method':'item/tool/call','params':{'threadId':'root','turnId':'turn',
                'tool':'project_file','arguments':{'action':'write','path':'results/supplement.txt','content':context['latest_messages'][0]}}})
        else:emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'turn','status':'completed'}}})
    elif m.get('id')=='first':
        assert m['result']['success'];active=True
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'method':'turn/started','params':{'threadId':'child','turn':{'id':'child-turn'}}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'turn','status':'completed'}}})
    elif m.get('id')=='supplement':
        assert m['result']['success']
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'turn','status':'completed'}}})
    elif method=='thread/read':
        record(method=method)
        emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root','status':{'type':'active' if active else 'idle'}}}})
    elif method=='fixture/finish':
        active=False;emit({'id':m['id'],'result':{}})
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':'child-turn','status':'completed'}}})
    elif method=='fixture/disconnect':emit({'id':m['id'],'result':{}});sys.exit(0)
if failure=='closed_input_resume_rejected':
    Path('stdin-eof').write_text('input closed; process remains alive')
    time.sleep(60)
''')
    executable.chmod(0o700)
    service.client_factory=CodexAppServer
    service.save_config(ServiceConfig(enabled=True,executable=str(executable),max_tokens=None))
    app.state.services.settings.official_agent_egress_enabled=True  # Python fixture only.
    _,headers=signup(client,'ReconnectSupplementWorker')
    pid=project(client,headers);enable(client,pid);path=chat(client,pid,headers)
    manager=app.state.services.local_agents;key=pid+':'+path.rsplit('/',1)[-1]
    waiting,release=threading.Event(),threading.Event();acquire=service.acquire;acquires=0
    async def gated_acquire(*args):
        nonlocal acquires
        acquires+=1
        if acquires==2:
            waiting.set()
            while not release.is_set():await asyncio.sleep(.01)
        result=await acquire(*args)
        if acquires==2 and failure=='resume_timeout':
            service.update(args[1],execution_seconds=service.config().max_seconds-.7)
        return result
    service.acquire=gated_acquire
    def calls():return [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    def pending():
        async def read():
            with conversation_scope(pid,path.rsplit('/',1)[-1]):return manager.load(pid).get('pending_messages',[])
        return client.portal.call(read)
    assert client.post(path+'/messages',headers=headers,json={'message':'first','request_key':'first'}).status_code==202
    for _ in range(300):
        if any(c['method']=='thread/read' for c in calls()):break
        time.sleep(.01)
    else:raise AssertionError('First child did not enter drain')
    connection=manager.clients[key]
    assert client.post(path+'/messages',headers=headers,json={'message':'supplement once','request_key':'supplement'}).status_code==202
    assert pending()==['supplement once']
    try:
        client.portal.call(connection.request,'fixture/finish',{})
        assert waiting.wait(5)
        if failure=='closed_input_resume_rejected':
            async def close_input():
                connection.process.stdin.close()
                await connection.process.stdin.wait_closed()
            client.portal.call(close_input)
            for _ in range(300):
                if (connection.runtime_dir/'empty-workspace/stdin-eof').exists():break
                time.sleep(.01)
            else:raise AssertionError('Protocol process did not observe input EOF')
            assert connection.process.returncode is None and not connection.reader.done() and not connection.closing
        else:
            client.portal.call(connection.request,'fixture/disconnect',{})
            async def reader_stopped():await asyncio.wait_for(asyncio.shield(connection.reader),5)
            client.portal.call(reader_stopped)
    finally:release.set()
    if failure=='resume_stopped':
        for _ in range(300):
            if any(c['method']=='thread/resume' for c in calls()):break
            time.sleep(.01)
        else:raise AssertionError('Thread resume did not start')
        assert client.post(path+'/stop',headers=headers).status_code==200
    failed=wait(client,path,headers)
    assert failed['status']==('interrupted' if failure=='resume_stopped' else 'error')
    if failure=='resume_timeout':assert '模型执行时限' in failed['error']
    first_job=service.job(failed['request_id']);unknown=failure=='delivered_without_reply'
    assert len([c for c in calls() if c['method']=='turn/start'])==(2 if unknown else 1)
    assert pending()==([] if unknown else ['supplement once'])
    repaired.write_text('resume explicitly allowed by fixture')
    assert client.post(path+'/messages',headers=headers,json={'message':'explicit resume','request_key':'resume'}).status_code==202
    assert wait(client,path,headers)['status']=='idle'
    delivered=[text for c in calls() if c['method']=='turn/start' for text in c['context'].get('latest_messages',[])]
    assert delivered==(['supplement once'] if unknown else ['supplement once','explicit resume'])
    assert not pending() and service.job(first_job['id'])==first_job
    workspace=app.state.services.settings.workspace_root/pid
    assert (workspace/'results/first.txt').read_text()=='saved once'
    if unknown:assert not (workspace/'results/supplement.txt').exists()
    else:assert (workspace/'results/supplement.txt').read_text()=='supplement once'


@pytest.mark.parametrize('delivery', ['accepted', 'ended_first', 'rejected_first', 'other_error', 'disconnect'])
def test_message_arriving_at_native_turn_end_is_delivered_once(official, tmp_path, delivery):
    from agent_platform.codex_app_server import CodexAppServer
    client, app, service = official
    executable = tmp_path / 'protocol_fixture'
    executable.write_text(f'#!{sys.executable}\ndelivery={delivery!r}\n' + '''
import json,sys,time
from pathlib import Path
number=0
def emit(value):print(json.dumps(value),flush=True)
def finish():emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'turn-'+str(number),'status':'completed'}}})
def write(message):
    emit({'id':'write','method':'item/tool/call','params':{'threadId':'root','turnId':'turn-'+str(number),
        'tool':'project_file','arguments':{'action':'write','path':'results/supplement.txt','content':message}}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        number+=1;Path('number').write_text(str(number))
        emit({'id':m['id'],'result':{'turn':{'id':'turn-'+str(number)}}})
        if number>1:
            context=json.loads(m['params']['input'][0]['text'])
            write('|'.join(context.get('latest_messages',[])))
    elif method=='turn/steer':
        if delivery=='disconnect':sys.exit(0)
        if delivery=='accepted':
            write(json.loads(m['params']['input'][0]['text'])['message'])
            emit({'id':m['id'],'result':{'turnId':'turn-'+str(number)}})
        else:
            if delivery=='ended_first':finish();time.sleep(.1)
            emit({'id':m['id'],'error':{'code':-32600,'message':
                'cannot steer a compact turn' if delivery=='other_error' else 'no active turn to steer'}})
            if delivery!='ended_first':time.sleep(.1);finish()
    elif m.get('id')=='write':
        assert m['result']['success'];finish()
''')
    executable.chmod(0o700)
    service.client_factory = CodexAppServer
    app.state.services.settings.official_agent_egress_enabled = True
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), max_tokens=None))
    _, headers = signup(client, 'FinishRaceWorker')
    pid = project(client, headers); enable(client, pid)
    path = chat(client, pid, headers)
    assert client.post(path+'/messages',headers=headers,json={'message':'开始处理'}).status_code == 202
    manager = app.state.services.local_agents
    key = pid+':'+path.rsplit('/',1)[-1]
    for _ in range(300):
        connection = manager.clients.get(key)
        if connection and connection.turn_id:break
        time.sleep(.02)
    else:raise AssertionError(client.get(path,headers=headers).json())
    body = {'message':'保存这条补充一次','request_key':'one-supplement'}
    response = client.post(path+'/messages',headers=headers,json=body)
    expected = 422 if delivery in {'other_error','disconnect'} else 202
    assert response.status_code == expected, response.text
    state = wait(client,path,headers)
    if expected == 202:
        assert state['status']=='idle', state
        output = app.state.services.settings.workspace_root/pid/'results/supplement.txt'
        assert output.read_text() == body['message']
        assert client.post(path+'/messages',headers=headers,json=body).status_code == 202
        assert int((connection.runtime_dir/'empty-workspace/number').read_text()) == (1 if delivery=='accepted' else 2)
        from agent_platform.conversation_scope import conversation_scope
        with conversation_scope(pid,path.rsplit('/',1)[-1]):
            writes = [e for e in manager.load(pid)['events'] if e['kind']=='tool' and e['text']=='project_file']
        assert len(writes)==1 and writes[0]['success']
        assert len(service.jobs())==1 and service.jobs()[0]['status']=='completed'
    else:
        assert int((connection.runtime_dir/'empty-workspace/number').read_text())==1
        assert not (app.state.services.settings.workspace_root/pid/'results/supplement.txt').exists()
        assert state['status'] == ('error' if delivery=='disconnect' else 'idle')


@pytest.mark.parametrize('target', ['sender', 'other_conversation'])
def test_public_stop_does_not_wait_for_pending_message_ack(official, tmp_path, target):
    from agent_platform.codex_app_server import CodexAppServer
    client, app, service = official
    executable = tmp_path/'protocol_fixture'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
from pathlib import Path
root=Path.cwd().parent.parent.name
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':root}}})
    elif method=='turn/start':emit({'id':m['id'],'result':{'turn':{'id':'turn-1'}}})
    elif method=='turn/steer':
        pending=m['id'];Path('pending').touch()
    elif method=='fixture/release':
        emit({'id':m['id'],'result':{}})
        emit({'id':pending,'result':{'turnId':'turn-1'}})
''')
    executable.chmod(0o700)
    service.client_factory = CodexAppServer
    app.state.services.settings.official_agent_egress_enabled = True
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), concurrency=2))
    _, headers = signup(client,'StopPendingWorker')
    pid = project(client,headers); enable(client,pid)
    first, second = chat(client,pid,headers), chat(client,pid,headers)
    manager = app.state.services.local_agents
    connections = {}
    for path in (first, second):
        assert client.post(path+'/messages',headers=headers,json={'message':'保持运行'}).status_code==202
        for _ in range(300):
            connection = manager.clients.get(pid+':'+path.rsplit('/',1)[-1])
            if connection and connection.turn_id:
                connections[path]=connection
                break
            time.sleep(.02)
        else:raise AssertionError(client.get(path,headers=headers).json())
    saved = app.state.services.settings.workspace_root/pid/'results/saved.txt'
    saved.parent.mkdir(exist_ok=True);saved.write_text('previous result')
    stopped_path = first if target=='sender' else second
    # Make repeated cancellation observable while the original run cleans up.
    interrupted_cleanup = []
    original_close = connections[stopped_path].close
    async def close():
        try:await asyncio.sleep(.08)
        except asyncio.CancelledError:
            interrupted_cleanup.append(True)
            raise
        await original_close()
    connections[stopped_path].close = close
    with ThreadPoolExecutor(max_workers=3) as executor:
        sender = executor.submit(client.post,first+'/messages',headers=headers,json={'message':'追加要求'})
        stops = []
        try:
            for _ in range(200):
                if (connections[first].runtime_dir/'empty-workspace/pending').exists():break
                time.sleep(.01)
            else:raise AssertionError('Steer was not sent')
            stops = [executor.submit(client.post,stopped_path+'/stop',headers=headers) for _ in range(2)]
            for stop in stops:
                response = stop.result(timeout=2)
                assert response.status_code==200 and response.json()['status']=='interrupted'
            assert not interrupted_cleanup
            assert connections[stopped_path].process.returncode is not None
            assert saved.read_text()=='previous result'
            survivor = second if target=='sender' else first
            assert client.get(survivor,headers=headers).json()['status']=='running'
            if target=='sender':assert sender.result(timeout=2).status_code==422
            else:assert not sender.done()
        finally:
            if not sender.done():
                client.portal.call(connections[first].request,'fixture/release',{})
            sender.result(timeout=3)
            for stop in stops:stop.result(timeout=3)
            for path in (first,second):client.post(path+'/stop',headers=headers)
    assert len(service.jobs())==2 and all(j['status']=='interrupted' for j in service.jobs())


@pytest.mark.parametrize('ending', ['complete', 'stop', 'shutdown', 'save_error'])
def test_final_state_is_saved_before_completion_and_survives_late_stop(official, monkeypatch, ending):
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client,'FinalizeWorker')
    client.headers.update(headers)
    pid = project(client,headers); enable(client,pid)
    graph(client,pid,[node('start','start'),node('end','end',outputs={'answer':42})],[edge('start','end')])
    path = chat(client,pid,headers)
    manager = app.state.services.local_agents
    key = pid+':'+path.rsplit('/',1)[-1]
    returned, saving, release = threading.Event(), threading.Event(), threading.Event()
    calls = []
    task_id = []
    async def turn(self,message,on_event,on_tool,**kwargs):
        calls.append(message)
        result = await on_tool('project_action',{'action':'trial','request_key':'actual-result'})
        assert result['status']=='succeeded' and result['outputs']=={'answer':42}
        task_id.append(result['id'])
        await on_tool('project_file',{'action':'write','path':'results/saved.txt','content':'answer=42'})
        returned.set()
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent,'turn',turn)
    original_get = app.state.services.projects.store.get_task
    async def get_task(project_id,identity,**kwargs):
        if (returned.is_set() and not saving.is_set() and identity in task_id
                and asyncio.current_task() is manager.tasks.get(key)):
            saving.set()
            while not release.is_set():await asyncio.sleep(.01)
            if ending=='save_error':raise OSError('模拟最终任务状态保存失败')
        return await original_get(project_id,identity,**kwargs)
    monkeypatch.setattr(app.state.services.projects.store,'get_task',get_task)
    assert client.post(path+'/messages',json={'message':'执行并保存结果'}).status_code==202
    assert saving.wait(6), client.get(path).json()
    try:
        if ending=='complete':
            assert service.jobs()[0]['status']!='completed'
            assert client.get(path).json()['status']=='running'
        elif ending in {'stop','shutdown'}:
            with ThreadPoolExecutor(max_workers=1) as executor:
                stopped = (executor.submit(client.post,path+'/stop') if ending=='stop' else
                           executor.submit(client.portal.call,manager.close))
                time.sleep(.08)
                waited = not stopped.done()
                release.set()
                response = stopped.result(timeout=3)
                assert waited  # Finishing saved results must not be cancelled halfway.
                if ending=='stop':assert response.status_code==200 and response.json()['status']=='idle'
    finally:
        release.set()
    state = wait(client,path,headers)
    assert state['status']==('error' if ending=='save_error' else 'idle'),state
    job = service.jobs()[0]
    assert job['status']==('error' if ending=='save_error' else 'completed')
    if ending=='save_error':assert '最终任务状态保存失败' in job['error'] and job['error']==state['error']
    result = client.get('/api/v1/projects/'+pid+'/tasks/'+task_id[0]).json()
    assert result['status']=='succeeded' and result['outputs']=={'answer':42}
    assert (app.state.services.settings.workspace_root/pid/'results/saved.txt').read_text()=='answer=42'
    assert len(calls)==1
    assert not manager.finalizing


@pytest.mark.parametrize('ending', ['completed', 'failed'])
@pytest.mark.parametrize('fault', ['record', 'session', 'job'])
def test_final_status_storage_failure_is_reported_without_replaying_saved_work(official, monkeypatch, ending, fault):
    from pathlib import Path

    client, app, service = official
    _, headers = signup(client, 'FinalRecordWorker')
    pid = project(client, headers); enable(client, pid)
    path = chat(client, pid, headers)
    manager = app.state.services.local_agents
    cid = path.rsplit('/', 1)[-1]
    saved = manager.root / pid / 'conversations' / cid / 'session.json'
    calls = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        calls.append(message)
        if len(calls) == 1:
            await on_tool('project_file', {'action': 'write', 'path': 'results/retained.txt', 'content': 'saved once'})
            return {'status': ending, 'error': {'message': 'original native failure'} if ending == 'failed' else None}
        result = await on_tool('project_file', {'action': 'read', 'path': 'results/retained.txt'})
        assert 'saved once' in str(result)
        return {'status': 'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    replace = Path.replace
    failures = []
    failure_text = f'final {fault} could not be saved'
    def fail_final_record(source, target):
        if Path(target) == saved and not failures:
            state = json.loads(source.read_text())
            last = state['events'][-1] if state['events'] else {}
            terminal = state['status'] in {'idle', 'error'}
            if (last.get('kind') == 'status' and last['text'] in {'本轮已结束', 'original native failure'}
                    and (fault == 'record' and not terminal or fault == 'session' and terminal)):
                failures.append(state)
                raise OSError(failure_text)
        return replace(source, target)
    monkeypatch.setattr(Path, 'replace', fail_final_record)
    update = service.update
    def fail_final_job(job_id, **values):
        if fault == 'job' and not failures and values.get('status') in {'completed', 'error'}:
            failures.append(values)
            raise OSError(failure_text)
        return update(job_id, **values)
    monkeypatch.setattr(service, 'update', fail_final_job)
    assert client.post(path+'/messages', headers=headers, json={'message':'保存结果', 'request_key':'first'}).status_code == 202
    async def joined():
        return await asyncio.gather(manager.tasks[pid+':'+cid], return_exceptions=True)
    client.portal.call(joined)
    state = client.get(path, headers=headers).json()
    first_job = service.jobs()[0]
    assert len(failures) == 1
    assert state['status'] == first_job['status'] == 'error'
    assert failure_text in state['error'] == first_job['error']
    if ending == 'failed': assert 'original native failure' in state['error']
    assert not manager.finalizing and len(calls) == 1
    result = app.state.services.settings.workspace_root / pid / 'results/retained.txt'
    assert result.read_text() == 'saved once'
    assert client.post(path+'/messages', headers=headers, json={'message':'读取原结果，不重做', 'request_key':'next'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    assert len(calls) == 2 and result.read_text() == 'saved once'
    assert service.job(first_job['id'])['status'] == 'error'


@pytest.mark.parametrize('ending', ['complete', 'state_error', 'sender_cancelled'])
def test_message_during_finalization_runs_as_its_own_request_once(official, monkeypatch, ending):
    from agent_platform.conversation_scope import conversation_scope
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client,'FinalMessageWorker')
    client.headers.update(headers)
    pid = project(client,headers); enable(client,pid)
    graph(client,pid,[node('start','start'),node('end','end',outputs={'answer':42})],[edge('start','end')])
    path = chat(client,pid,headers)
    manager = app.state.services.local_agents
    key = pid+':'+path.rsplit('/',1)[-1]
    returned, saving, release = threading.Event(), threading.Event(), threading.Event()
    calls, task_id = [], []
    async def turn(self,message,on_event,on_tool,**kwargs):
        context = json.loads(message)
        calls.append(context)
        if len(calls)==1:
            result = await on_tool('project_action',{'action':'trial','request_key':'first-result'})
            assert result['status']=='succeeded'
            task_id.append(result['id'])
            await on_tool('project_file',{'action':'write','path':'results/first.txt','content':'answer=42'})
            returned.set()
        else:
            await on_tool('project_file',{'action':'write','path':'results/followup.txt','content':context['user_message']})
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent,'turn',turn)
    original_get = app.state.services.projects.store.get_task
    async def get_task(project_id,identity,**kwargs):
        if (returned.is_set() and not saving.is_set() and identity in task_id
                and asyncio.current_task() is manager.tasks.get(key)):
            saving.set()
            while not release.is_set():await asyncio.sleep(.01)
            if ending=='state_error':raise OSError('模拟收尾状态读取失败')
        return await original_get(project_id,identity,**kwargs)
    monkeypatch.setattr(app.state.services.projects.store,'get_task',get_task)
    assert client.post(path+'/messages',json={'message':'生成第一个结果','request_key':'first-request'}).status_code==202
    assert saving.wait(6), client.get(path).json()
    first_job = service.jobs()[0]['id']
    original_task = manager.tasks[key]
    body = {'message':'基于第一个结果保存后续说明','request_key':'followup-request','task_id':task_id[0]}
    if ending=='sender_cancelled':
        import httpx
        async def begin_send():
            async def send():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as api:
                    return await api.post(path+'/messages',headers=headers,json=body)
            return asyncio.create_task(send())
        pending = client.portal.call(begin_send)
        try:
            time.sleep(.1)
            assert not pending.done()
            async def cancel_send():
                pending.cancel()
                await asyncio.gather(pending,return_exceptions=True)
            client.portal.call(cancel_send)
            assert not original_task.done()
        finally:
            release.set()
        assert wait(client,path,headers)['status']=='idle'
        assert len(service.jobs())==1 and len(calls)==1
        assert client.post(path+'/messages',json=body).status_code==202
    else:
        with ThreadPoolExecutor(max_workers=2) as executor:
            sends = [executor.submit(client.post,path+'/messages',json=body) for _ in range(2)]
            try:
                time.sleep(.1)
                assert len(calls)==1
            finally:
                release.set()
            assert all(f.result(timeout=3).status_code==202 for f in sends)
    state = wait(client,path,headers)
    assert state['status']=='idle'
    assert len(calls)==2
    assert calls[1]['user_message']==body['message']
    assert calls[1]['conversation_context']['task_id']==task_id[0]
    assert not calls[1].get('latest_messages')
    jobs = service.jobs()
    assert len(jobs)==2
    first = next(j for j in jobs if j['id']==first_job)
    second = next(j for j in jobs if j['id']!=first_job)
    assert first['status']==('error' if ending=='state_error' else 'completed')
    assert second['status']=='completed' and state['request_id']==second['id']
    with conversation_scope(pid,path.rsplit('/',1)[-1]):
        saved = manager.load(pid)
    users = [e for e in saved['events'] if e['kind']=='user']
    assert len(users)==2 and [e['request_id'] for e in users]==[first_job,second['id']]
    assert not saved.get('pending_messages')
    workspace = app.state.services.settings.workspace_root/pid
    assert (workspace/'results/first.txt').read_text()=='answer=42'
    assert (workspace/'results/followup.txt').read_text()==body['message']
    assert len(client.get('/api/v1/projects/'+pid+'/tasks').json())==1


@pytest.mark.parametrize('phase,cancelled', [
    ('finalizing', 0), ('finalizing', 1), ('finalizing', 2), ('running', 1),
])
def test_stop_waiters_preserve_finalization_and_the_next_request(official, monkeypatch, phase, cancelled):
    import httpx
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client, 'StopBoundaryWorker')
    client.headers.update(headers)
    pid = project(client, headers); enable(client, pid)
    graph(client, pid, [node('start','start'), node('end','end',outputs={'answer':42})], [edge('start','end')])
    path = chat(client, pid, headers)
    manager = app.state.services.local_agents
    key = pid + ':' + path.rsplit('/', 1)[-1]
    first_returned, saving, release = threading.Event(), threading.Event(), threading.Event()
    next_started, release_next = threading.Event(), threading.Event()
    stop_entered = [threading.Event(), threading.Event()]
    calls, task_ids = [], []
    async def turn(self, message, on_event, on_tool, **kwargs):
        calls.append(json.loads(message))
        if len(calls) == 1:
            result = await on_tool('project_action', {'action':'trial', 'request_key':'saved-result'})
            assert result['status'] == 'succeeded'
            task_ids.append(result['id'])
            await on_tool('project_file', {'action':'write','path':'results/first.txt','content':'answer=42'})
            first_returned.set()
            if phase == 'running':
                await asyncio.Event().wait()
        else:
            next_started.set()
            while not release_next.is_set(): await asyncio.sleep(.01)
            await on_tool('project_file', {'action':'write','path':'results/next.txt','content':'next request finished'})
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    original_get = app.state.services.projects.store.get_task
    async def get_task(project_id, identity, **kwargs):
        if (first_returned.is_set() and not saving.is_set() and identity in task_ids
                and asyncio.current_task() is manager.tasks.get(key)):
            saving.set()
            while not release.is_set(): await asyncio.sleep(.01)
        return await original_get(project_id, identity, **kwargs)
    monkeypatch.setattr(app.state.services.projects.store, 'get_task', get_task)
    original_stop = manager.stop
    stop_count = 0
    async def stop(project_id):
        nonlocal stop_count
        index = stop_count
        stop_count += 1
        stop_entered[index].set()
        return await original_stop(project_id)
    monkeypatch.setattr(manager, 'stop', stop)
    assert client.post(path+'/messages', json={'message':'先生成结果','request_key':'first'}).status_code == 202
    assert first_returned.wait(6)
    first_task = manager.tasks[key]
    first_job = service.jobs()[0]['id']
    pending = []
    async def begin_request(suffix, body=None):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
                return await api.post(path+suffix, headers=headers, json=body)
        return asyncio.create_task(request())
    async def collect(tasks):
        return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 6)
    try:
        if phase == 'running':
            pending.append(client.portal.call(begin_request, '/stop'))
        assert saving.wait(6)
        # The new message waits for the old finalizer, before the remaining
        # stop waiters. It must survive their eventual return.
        followup = client.portal.call(begin_request, '/messages', {'message':'生成下一份结果','request_key':'next'})
        for _ in range(2-len(pending)):
            pending.append(client.portal.call(begin_request, '/stop'))
        assert all(event.wait(3) for event in stop_entered)
        time.sleep(.05)
        assert not followup.done() and all(not request.done() for request in pending)
        if cancelled:
            async def cancel_requests():
                for request in pending[:cancelled]: request.cancel()
                return await asyncio.gather(*pending[:cancelled], return_exceptions=True)
            results = client.portal.call(cancel_requests)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
            assert not first_task.done()  # Cancelling an HTTP waiter must not cancel saving.
            assert not followup.done()
        assert service.job(first_job)['status'] != 'completed'
        release.set()
        results = client.portal.call(collect, pending)
        assert all(result.status_code == 200 for result in results[cancelled:])
        response = client.portal.call(collect, [followup])[0]
        assert response.status_code == 202
        assert next_started.wait(6)
        assert manager.tasks[key] is not first_task and not manager.tasks[key].done()
        assert client.get(path).json()['status'] == 'running'
        assert service.job(first_job)['status'] == ('interrupted' if phase == 'running' else 'completed')
    finally:
        release.set(); release_next.set()
        client.portal.call(collect, pending + ([followup] if 'followup' in locals() else []))
    assert wait(client, path, headers)['status'] == 'idle'
    assert len(calls) == 2 and len(service.jobs()) == 2
    assert next(job for job in service.jobs() if job['id'] != first_job)['status'] == 'completed'
    result = client.get('/api/v1/projects/'+pid+'/tasks/'+task_ids[0]).json()
    assert result['status'] == 'succeeded' and result['outputs'] == {'answer':42}
    workspace = app.state.services.settings.workspace_root/pid
    assert (workspace/'results/first.txt').read_text() == 'answer=42'
    assert (workspace/'results/next.txt').read_text() == 'next request finished'
    assert not manager.finalizing


@pytest.mark.parametrize('ending', ['stop', 'prepare_error', 'sender_cancelled', 'stop_cancelled'])
def test_stop_during_first_message_preparation_does_not_launch_tools(official, monkeypatch, ending):
    import httpx
    from tests.test_projects import graph, node, edge
    client, app, service = official
    _, headers = signup(client, 'PreparingWorker')
    client.headers.update(headers)
    pid = project(client, headers); enable(client, pid)
    graph(client, pid, [node('start','start'), node('end','end',outputs={'answer':42})], [edge('start','end')])
    path = chat(client, pid, headers)
    manager = app.state.services.local_agents
    key = pid + ':' + path.rsplit('/', 1)[-1]
    preparing, release_prepare = threading.Event(), threading.Event()
    finishing, release_finish, stop_entered = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original_require = manager.require_project_model
    async def require(project_id, provider):
        await original_require(project_id, provider)
        if not preparing.is_set():
            preparing.set()
            while not release_prepare.is_set(): await asyncio.sleep(.01)
            if ending == 'prepare_error': raise ValueError('模拟准备阶段授权失败')
    monkeypatch.setattr(manager, 'require_project_model', require)
    original_stop = manager.stop
    async def stop(project_id):
        stop_entered.set()
        return await original_stop(project_id)
    monkeypatch.setattr(manager, 'stop', stop)
    original_pause = app.state.services.projects.conversation.pause
    async def pause(project_id, reason, **kwargs):
        if not finishing.is_set():
            finishing.set()
            while not release_finish.is_set(): await asyncio.sleep(.01)
        return await original_pause(project_id, reason, **kwargs)
    monkeypatch.setattr(app.state.services.projects.conversation, 'pause', pause)
    async def turn(self, message, on_event, on_tool, **kwargs):
        calls.append(json.loads(message))
        result = await on_tool('project_action', {'action':'trial','request_key':'explicit-followup'})
        assert result['status'] == 'succeeded' and result['outputs'] == {'answer':42}
        await on_tool('project_file', {'action':'write','path':'results/next.txt','content':'answer=42'})
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    async def begin_request(suffix, body=None):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
                return await api.post(path+suffix, headers=headers, json=body)
        return asyncio.create_task(request())
    async def collect(tasks):
        return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 6)
    async def cancel_request(task):
        task.cancel()
        return (await asyncio.gather(task, return_exceptions=True))[0]
    sender = client.portal.call(begin_request, '/messages', {'message':'准备执行','request_key':'first'})
    pending = [sender]
    try:
        assert preparing.wait(6)
        assert key not in manager.tasks and not service.jobs()
        stopping = client.portal.call(begin_request, '/stop')
        pending.append(stopping)
        assert stop_entered.wait(3)
        time.sleep(.05)
        assert not stopping.done()
        if ending == 'sender_cancelled':
            assert isinstance(client.portal.call(cancel_request, sender), asyncio.CancelledError)
        release_prepare.set()
        if ending in {'stop', 'stop_cancelled'}:
            assert finishing.wait(6)
            owner = manager.tasks[key]
            assert not owner.done() and not calls
            assert service.jobs()[0]['status'] != 'completed'
            if ending == 'stop_cancelled':
                assert isinstance(client.portal.call(cancel_request, stopping), asyncio.CancelledError)
                assert not owner.done()
            release_finish.set()
        responses = client.portal.call(collect, pending)
        if ending != 'sender_cancelled':
            assert responses[0].status_code == (422 if ending == 'prepare_error' else 202)
        if ending != 'stop_cancelled': assert responses[1].status_code == 200
        state = wait(client, path, headers)
        assert state['status'] == ('interrupted' if ending in {'stop','stop_cancelled'} else 'idle')
        assert not calls and not client.get('/api/v1/projects/'+pid+'/tasks').json()
        assert all(job['status'] == 'interrupted' for job in service.jobs())
        assert len(service.jobs()) == (1 if ending in {'stop','stop_cancelled'} else 0)
    finally:
        release_prepare.set(); release_finish.set()
        client.portal.call(collect, pending)
    assert client.post(path+'/messages', json={'message':'现在明确开始','request_key':'next'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    assert len(calls) == 1 and calls[0]['user_message'] == '现在明确开始'
    assert (app.state.services.settings.workspace_root/pid/'results/next.txt').read_text() == 'answer=42'
    jobs = service.jobs()
    assert sum(job['status'] == 'completed' for job in jobs) == 1
    assert not manager.finalizing


@pytest.mark.parametrize('ending', ['native_complete', 'user_stop', 'normal_tool_end', 'pending_supplement'])
def test_cleanup_failure_is_visible_in_job_and_chat_and_can_be_retried(official, tmp_path, monkeypatch, ending):
    from agent_platform.codex_app_server import CodexAppServer
    from agent_platform.conversation_scope import conversation_scope
    from agent_platform.sandbox import SandboxSession, CommandResult
    client, app, service = official
    executable = tmp_path/'protocol_fixture'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
recovering=False
def emit(value):print(json.dumps(value),flush=True)
def write(identity,path,content):
    emit({'id':identity,'method':'item/tool/call','params':{'threadId':'root',
        'turnId':'recovery-turn' if recovering else 'root-turn','tool':'project_file',
        'arguments':{'action':'write','path':path,'content':content}}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method in {'thread/start','thread/resume'}:emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        recovering=json.loads(m['params']['input'][0]['text'])['user_message']=='明确继续'
        emit({'id':m['id'],'result':{'turn':{'id':'recovery-turn' if recovering else 'root-turn'}}})
        write('recovered' if recovering else 'saved','results/recovered.txt' if recovering else 'results/saved.txt','saved result')
    elif m.get('id')=='saved':
        assert m['result']['success']
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'id':'code','method':'item/tool/call','params':{'threadId':'child','turnId':'child-turn',
            'tool':'project_code','arguments':{'code':'print("working")','timeout':30}}})
    elif method=='fixture/complete' or (method=='turn/steer' and ending=='pending_supplement'):
        if method=='fixture/complete':emit({'id':m['id'],'result':{}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':'child-turn','status':'interrupted'}}})
    elif method=='thread/read':emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root','status':{'type':'idle'}}}})
    elif m.get('id')=='code' and ending=='normal_tool_end':
        assert not m['result']['success']
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':'child-turn','status':'completed'}}})
    elif m.get('id')=='recovered':
        assert m['result']['success']
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'recovery-turn','status':'completed'}}})
''')
    executable.chmod(0o700)
    connections = []
    def factory(*args, **kwargs):
        connection = CodexAppServer(*args, **kwargs)
        connections.append(connection)
        return connection
    service.client_factory = factory
    app.state.services.settings.official_agent_egress_enabled = True  # Python protocol fixture only.
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), max_tokens=None))
    _, headers = signup(client, 'CleanupFailureWorker')
    pid = project(client, headers); enable(client, pid)
    path = chat(client, pid, headers)
    manager = app.state.services.local_agents
    sandboxes = app.state.services.sandboxes
    started = threading.Event()
    reject_removal = True
    async def docker(self, argv, **kwargs):
        if argv[1] == 'run': return CommandResult('fixture container','',0)
        if argv[1] == 'exec':
            started.set()
            if ending == 'normal_tool_end': return CommandResult('computed result','',0)
            await asyncio.Event().wait()
        assert argv[1] == 'rm'
        return CommandResult('', 'simulated removal failure' if reject_removal else '', 1 if reject_removal else 0)
    monkeypatch.setattr(SandboxSession, '_host_command', docker)
    assert client.post(path+'/messages',headers=headers,json={'message':'开始处理','request_key':'first'}).status_code == 202
    try:
        assert started.wait(6)
        first_job = service.jobs()[0]['id']
        connection = connections[0]
        if ending == 'native_complete':client.portal.call(connection.request, 'fixture/complete', {})
        elif ending == 'user_stop':assert client.post(path+'/stop',headers=headers).status_code == 200
        elif ending == 'pending_supplement':
            with ThreadPoolExecutor(max_workers=1) as executor:
                sender = executor.submit(client.post,path+'/messages',headers=headers,json={'message':'追加要求'})
                try:
                    assert sender.result(timeout=2).status_code == 422
                finally:
                    if not sender.done():client.portal.call(connection.close)
                    sender.result(timeout=3)
        state = wait(client,path,headers)
        assert state['status'] == 'error' and 'simulated removal failure' in state['error']
        assert service.job(first_job)['status'] == 'error'
        assert service.job(first_job)['error'] == state['error']
        assert len(service.jobs()) == 1  # No automatic replay of unacknowledged input.
        assert connection.process.returncode is not None
        assert len(sandboxes.sessions) == 1
        with conversation_scope(pid,path.rsplit('/',1)[-1]):
            failures = [e for e in manager.load(pid)['events'] if e['kind']=='tool' and e['text']=='project_code']
        assert len(failures) == 1 and failures[0]['status'] == 'failed'
        workspace = app.state.services.settings.workspace_root/pid
        assert (workspace/'results/saved.txt').read_text() == 'saved result'
    finally:
        reject_removal = False
        client.portal.call(sandboxes.close)
    assert not sandboxes.sessions
    assert client.post(path+'/messages',headers=headers,json={'message':'明确继续','request_key':'next'}).status_code == 202
    assert wait(client,path,headers)['status'] == 'idle'
    assert service.job(first_job)['status'] == 'error'
    assert len(service.jobs()) == 2 and sum(job['status']=='completed' for job in service.jobs()) == 1
    assert (workspace/'results/recovered.txt').read_text() == 'saved result'


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


@pytest.mark.parametrize('timing', ['initial', 'followup'])
@pytest.mark.parametrize('during_recovery', ['none', 'supplement', 'stop'])
def test_recovery_preserves_initial_request_and_ordered_supplements_without_duplicates(
        tmp_path, monkeypatch, timing, during_recovery):
    from fastapi.testclient import TestClient
    from agent_platform.api import create_app
    from agent_platform.config import Settings

    settings = Settings(_env_file=None, api_token='admin-boot', data_dir=tmp_path/'data',
                        workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                        scheduler_poll_seconds=3600)
    original = '计算原始总和并保存'
    supplements = ['额外加10', '再加20']
    contexts, actions = [], []
    first_ready, release_first = threading.Event(), threading.Event()
    setup_ready, release_setup = threading.Event(), threading.Event()
    recovering = False
    increments = {'额外加10':10, '再加20':20, '最后加30':30}

    class Session(FakeAgent):
        async def start(self, tools, instructions, thread_id=None):
            if recovering and during_recovery != 'none':
                setup_ready.set()
                while not release_setup.is_set(): await asyncio.sleep(.01)
            return await super().start(tools, instructions, thread_id)

    async def turn(self, message, on_event, on_tool, **kwargs):
        context = json.loads(message)
        contexts.append(context)
        if context['user_message'] == original and original not in actions:
            actions.append(original)
            await on_tool('project_file', {'action':'write', 'path':'results/base.txt', 'content':'45'})
            if timing == 'followup':
                first_ready.set()
                while not release_first.is_set(): await asyncio.sleep(.01)
        if original in actions:
            for text in context.get('latest_messages', []):
                actions.append(text)
            if context.get('latest_messages'):
                total = 45 + sum(increments[text] for text in context['latest_messages'])
                await on_tool('project_file', {'action':'write', 'path':'results/final.txt', 'content':str(total)})
        return {'status':'completed'}

    monkeypatch.setattr(FakeAgent, 'turn', turn)
    def application(reserve=0):
        app = create_app(settings)
        service = app.state.services.official_agent
        service.client_factory = Session
        service.save_config(ServiceConfig(enabled=True, reserve_percent=reserve))
        async def inspect():
            service.rate_limits = {'rateLimits': {'primary': {'usedPercent':10}}}
            return {'error':'', 'dispatch_reason':service.quota_reason()}
        service.inspect = inspect
        return app, service

    app, service = application(100 if timing == 'initial' else 0)
    with TestClient(app) as client:
        _, headers = signup(client, 'OrderedRecoveryWorker')
        pid = project(client, headers)
        enable(client, pid)
        path = chat(client, pid, headers)
        assert client.post(path+'/messages', headers=headers, json={'message':original,'request_key':'original'}).status_code == 202
        if timing == 'followup':
            assert first_ready.wait(5)
            service.save_config(service.config().model_copy(update={'reserve_percent':100}))
        else:
            wait(client, path, headers, ('queued',))
        for index, text in enumerate(supplements):
            body = {'message':text, 'request_key':'supplement-'+str(index)}
            for _ in range(2):
                assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
        release_first.set()
        queued = wait(client, path, headers, ('queued',))
        job_id = queued['request_id']
        assert (service.job(job_id)['started'] is None) == (timing == 'initial')
        target = app.state.services.local_agents.root/pid/'conversations'/path.rsplit('/',1)[-1]/'session.json'
        assert json.loads(target.read_text())['pending_messages'] == supplements
        assert [event['text'] for event in queued['events'] if event['kind']=='user'] == [original, *supplements]
    assert service.job(job_id)['status'] == 'queued'
    assert len(contexts) == (0 if timing == 'initial' else 1)
    recovering = True
    expected_inputs = [*supplements]
    app, service = application()
    with TestClient(app) as client:
        if during_recovery != 'none':
            assert setup_ready.wait(5)
            assert len(contexts) == (0 if timing == 'initial' else 1)
            body = {'message':'最后加30', 'request_key':'during-recovery'}
            for _ in range(2):
                assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
            expected_inputs.append(body['message'])
            assert json.loads(target.read_text())['pending_messages'] == expected_inputs
            assert client.get(path, headers=headers).json()['status'] == 'running'
        if during_recovery == 'stop':
            state = client.post(path+'/stop', headers=headers).json()
            assert state['status'] == 'interrupted' and state['request_id'] == job_id
            assert service.job(job_id)['status'] == 'interrupted'
            assert len(contexts) == (0 if timing == 'initial' else 1)
            assert json.loads(target.read_text())['pending_messages'] == expected_inputs
        else:
            release_setup.set()
            async def recovered():
                await asyncio.wait_for(asyncio.shield(app.state.services.local_agents.tasks[pid+':'+path.rsplit('/',1)[-1]]), 5)
            client.portal.call(recovered)
            state = client.get(path, headers=headers).json()
            assert state['status'] == 'idle' and state['request_id'] == job_id
            assert contexts[-1]['user_message'] == (original if timing == 'initial' else supplements[-1])
            assert contexts[-1]['latest_messages'] == expected_inputs
            assert actions == [original, *expected_inputs]
            for index, text in enumerate(supplements):
                assert client.post(path+'/messages', headers=headers, json={'message':text,'request_key':'supplement-'+str(index)}).status_code == 202
            assert len(contexts) == (1 if timing == 'initial' else 2)
            assert len(service.jobs()) == 1 and service.job(job_id)['status'] == 'completed'
            assert not json.loads(target.read_text()).get('pending_messages')
    workspace = settings.workspace_root/pid/'results'
    if during_recovery == 'stop':
        app, service = application()
        with TestClient(app) as client:
            assert not app.state.services.local_agents.tasks
            assert client.get(path, headers=headers).json()['status'] == 'interrupted'
            assert service.job(job_id)['status'] == 'interrupted'
            assert len(contexts) == (0 if timing == 'initial' else 1)
            assert not (workspace/'final.txt').exists()
            assert (workspace/'base.txt').exists() == (timing == 'followup')
        return
    assert (workspace/'base.txt').read_text() == '45'
    assert (workspace/'final.txt').read_text() == str(45 + sum(increments[text] for text in expected_inputs))


def test_shutdown_during_requeue_access_check_preserves_undelivered_supplement(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_platform.api import create_app
    from agent_platform.config import Settings

    settings = Settings(_env_file=None, api_token='admin-boot', data_dir=tmp_path/'data',
                        workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                        scheduler_poll_seconds=3600)
    first_ready, release_first, first_done, checking = (threading.Event() for _ in range(4))
    contexts = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        contexts.append(json.loads(message))
        first = len(contexts) == 1
        await on_tool('project_file', {'action':'write', 'path':'results/'+('first' if first else 'next')+'.txt', 'content':'saved once'})
        if first:
            first_ready.set()
            while not release_first.is_set(): await asyncio.sleep(.01)
            first_done.set()
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)

    def application():
        app = create_app(settings)
        service = app.state.services.official_agent
        service.client_factory = FakeAgent
        service.save_config(ServiceConfig(enabled=True))
        async def inspect():
            service.rate_limits = {'rateLimits': {'primary': {'usedPercent':10}}}
            return {'error':'', 'dispatch_reason':service.quota_reason()}
        service.inspect = inspect
        return app, service

    app, service = application()
    authorize = service.authorize
    async def checking_access(project_id):
        result = await authorize(project_id)
        if first_done.is_set() and app.state.services.local_agents.load(project_id)['status'] == 'queued':
            checking.set()
            await asyncio.Event().wait()
        return result
    service.authorize = checking_access
    with TestClient(app) as client:
        _, headers = signup(client, 'QueueBoundaryWorker')
        pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
        assert client.post(path+'/messages', headers=headers, json={'message':'first','request_key':'first'}).status_code == 202
        assert first_ready.wait(5)
        assert client.post(path+'/messages', headers=headers, json={'message':'supplement','request_key':'supplement'}).status_code == 202
        release_first.set()
        assert checking.wait(5)
        state = client.get(path, headers=headers).json()
        job_id = state['request_id']
        assert state['status'] == 'queued' and len(contexts) == 1
        assert service.job(job_id)['status'] == 'queued'
    assert service.job(job_id)['status'] == 'queued'
    app, service = application()
    with TestClient(app) as client:
        async def recovered():
            await asyncio.wait_for(asyncio.shield(app.state.services.local_agents.tasks[pid+':'+path.rsplit('/',1)[-1]]), 5)
        client.portal.call(recovered)
        assert client.get(path, headers=headers).json()['status'] == 'idle'
        assert len(contexts) == 2 and contexts[-1]['latest_messages'] == ['supplement']
        assert service.job(job_id)['status'] == 'completed'
    workspace = settings.workspace_root/pid/'results'
    assert (workspace/'first.txt').read_text() == (workspace/'next.txt').read_text() == 'saved once'


@pytest.mark.parametrize('ending', ['shutdown_only', 'stop_before_shutdown', 'stop_during_shutdown', 'denied_before_shutdown',
                                  'cancelled_stop_before_shutdown', 'cancelled_stop_during_shutdown'])
def test_shutdown_does_not_restore_an_explicitly_stopped_or_denied_queued_request(tmp_path, monkeypatch, ending):
    import httpx
    from fastapi.testclient import TestClient
    from agent_platform.api import create_app
    from agent_platform.config import Settings

    settings = Settings(_env_file=None, api_token='admin-boot', data_dir=tmp_path/'data',
                        workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                        scheduler_poll_seconds=3600)
    contexts = []
    cancel_waiter = ending.startswith('cancelled_')
    phase = ending.removeprefix('cancelled_')
    async def turn(self, message, on_event, on_tool, **kwargs):
        contexts.append(json.loads(message))
        name = 'explicit' if contexts[-1]['user_message'] == 'new request only' else 'recovered'
        await on_tool('project_file', {'action':'write', 'path':'results/'+name+'.txt', 'content':'executed once'})
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    def application(reserve=0):
        app = create_app(settings)
        service = app.state.services.official_agent
        service.client_factory = FakeAgent
        service.save_config(ServiceConfig(enabled=True, reserve_percent=reserve))
        async def inspect():
            service.rate_limits = {'rateLimits': {'primary': {'usedPercent':10}}}
            return {'error':'', 'dispatch_reason':service.quota_reason()}
        service.inspect = inspect
        return app, service

    app, service = application(100)
    finishing, release_finish, stop_entered = threading.Event(), threading.Event(), threading.Event()
    original_pause = app.state.services.projects.conversation.pause
    async def pause(*args, **kwargs):
        finishing.set()
        while not release_finish.is_set(): await asyncio.sleep(.01)
        return await original_pause(*args, **kwargs)
    monkeypatch.setattr(app.state.services.projects.conversation, 'pause', pause)
    original_stop = app.state.services.local_agents.stop
    async def stop(project_id):
        stop_entered.set()
        return await original_stop(project_id)
    monkeypatch.setattr(app.state.services.local_agents, 'stop', stop)
    client = TestClient(app)
    client.__enter__()
    shutdown = stopping = None
    try:
        _, headers = signup(client, 'ShutdownIntentWorker')
        pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
        assert client.post(path+'/messages', headers=headers, json={'message':'original','request_key':'original'}).status_code == 202
        state = wait(client, path, headers, ('queued',))
        job_id = state['request_id']
        assert client.post(path+'/messages', headers=headers, json={'message':'supplement','request_key':'supplement'}).status_code == 202
        async def request_stop():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as api:
                return await api.post(path+'/stop', headers=headers)
        async def begin_stop():
            return asyncio.create_task(request_stop())
        async def cancel_stop(task):
            task.cancel()
            return (await asyncio.gather(task, return_exceptions=True))[0]
        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                if phase == 'stop_before_shutdown':
                    if cancel_waiter: stop_task = client.portal.call(begin_stop)
                    # Keep the request and response-body read in one portal task.
                    # A TestClient.post thread may try to read its streaming body
                    # after concurrent __exit__ has already closed that portal.
                    else: stopping = client.portal.start_task_soon(request_stop)
                    assert finishing.wait(5)
                    if cancel_waiter: assert isinstance(client.portal.call(cancel_stop, stop_task), asyncio.CancelledError)
                elif phase == 'denied_before_shutdown':
                    assert client.put('/api/v1/projects/'+pid+'/capabilities', headers=ADMIN, json={'agent_modules_enabled':False}).status_code == 200
                    assert finishing.wait(5)
                shutdown = executor.submit(client.__exit__, None, None, None)
                assert finishing.wait(5)
                for _ in range(200):
                    if service.shutting_down: break
                    time.sleep(.01)
                assert service.shutting_down
                if phase == 'stop_during_shutdown':
                    if cancel_waiter: stop_task = client.portal.call(begin_stop)
                    else: stopping = client.portal.start_task_soon(request_stop)
                    # Wait for the public stop to reach the observed task while
                    # shutdown is still waiting for its finalizer.
                    assert stop_entered.wait(5)
                    if cancel_waiter: assert isinstance(client.portal.call(cancel_stop, stop_task), asyncio.CancelledError)
                assert service.job(job_id)['status'] == 'queued' and not contexts
            finally:
                release_finish.set()
            if stopping:
                response = stopping.result(timeout=5)
                assert response.status_code == 200, response.text
            shutdown.result(timeout=5)
    finally:
        release_finish.set()
        if shutdown is None:
            client.__exit__(None, None, None)
    expected = 'queued' if phase == 'shutdown_only' else 'error' if phase == 'denied_before_shutdown' else 'interrupted'
    assert service.job(job_id)['status'] == expected
    if phase == 'denied_before_shutdown': assert '此项目未获准使用官方智能体' in service.job(job_id)['error']
    assert not contexts
    app, service = application()
    with TestClient(app) as client:
        if phase == 'shutdown_only':
            async def recovered():
                await asyncio.wait_for(asyncio.shield(app.state.services.local_agents.tasks[pid+':'+path.rsplit('/',1)[-1]]), 5)
            client.portal.call(recovered)
            assert service.job(job_id)['status'] == 'completed'
            assert len(contexts) == 1 and contexts[0]['latest_messages'] == ['supplement']
        else:
            assert not app.state.services.local_agents.tasks and not contexts
            assert service.job(job_id)['status'] == expected
            assert not (settings.workspace_root/pid/'results/recovered.txt').exists()
            if cancel_waiter:
                stopped_job = service.job(job_id)
                body = {'message':'new request only', 'request_key':'explicit-after-stop'}
                assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
                completed = wait(client, path, headers)
                assert completed['status'] == 'idle' and completed['request_id'] != job_id
                assert service.job(completed['request_id'])['status'] == 'completed'
                assert service.job(job_id) == stopped_job and len(service.jobs()) == 2
                assert len(contexts) == 1 and contexts[0]['latest_messages'] == ['supplement', body['message']]
                assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
                assert len(contexts) == 1 and len(service.jobs()) == 2
                assert (settings.workspace_root/pid/'results/explicit.txt').read_text() == 'executed once'
                assert not (settings.workspace_root/pid/'results/recovered.txt').exists()


@pytest.mark.parametrize('arrival', ['during_shutdown', 'waiting_for_finalizer', 'during_enqueue_check'])
def test_shutdown_rejects_new_messages_without_orphaning_jobs_and_allows_retry(tmp_path, monkeypatch, arrival):
    from fastapi.testclient import TestClient
    from agent_platform.api import create_app
    from agent_platform.config import Settings

    settings = Settings(_env_file=None, api_token='admin-boot', data_dir=tmp_path/'data',
                        workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                        scheduler_poll_seconds=3600)
    calls = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        text = json.loads(message)['user_message']; calls.append(text)
        await on_tool('project_file', {'action':'write', 'path':'results/'+text+'.txt', 'content':'executed once'})
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    def application(reserve=0):
        app = create_app(settings); service = app.state.services.official_agent
        service.client_factory = FakeAgent
        service.save_config(ServiceConfig(enabled=True, reserve_percent=reserve))
        async def inspect():
            service.rate_limits = {'rateLimits': {'primary': {'usedPercent':10}}}
            return {'error':'', 'dispatch_reason':service.quota_reason()}
        service.inspect = inspect
        return app, service

    app, service = application(100)
    manager = app.state.services.local_agents
    finishing, release_finish, closing, release_close, entered, checked, enqueue_check, release_check = (threading.Event() for _ in range(8))
    original_pause = app.state.services.projects.conversation.pause
    async def pause(*args, **kwargs):
        finishing.set()
        while not release_finish.is_set(): await asyncio.sleep(.01)
        return await original_pause(*args, **kwargs)
    monkeypatch.setattr(app.state.services.projects.conversation, 'pause', pause)
    original_close = service.close
    async def close():
        closing.set()
        while not release_close.is_set(): await asyncio.sleep(.01)
        await original_close()
    service.close = close
    original_message, original_require, original_authorize = manager.message, manager.require_project_model, service.authorize
    async def message(project_id, text, **kwargs):
        if text == 'new': entered.set()
        return await original_message(project_id, text, **kwargs)
    async def require(*args):
        await original_require(*args)
        if entered.is_set(): checked.set()
    async def authorize(project_id):
        result = await original_authorize(project_id)
        if arrival == 'during_enqueue_check' and checked.is_set() and not enqueue_check.is_set():
            enqueue_check.set()
            while not release_check.is_set(): await asyncio.sleep(.01)
        return result
    monkeypatch.setattr(manager, 'message', message)
    monkeypatch.setattr(manager, 'require_project_model', require)
    service.authorize = authorize
    client = TestClient(app); client.__enter__(); shutdown = stopping = None
    try:
        _, headers = signup(client, 'ClosingRequestWorker')
        pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
        old_id = None
        if arrival != 'during_enqueue_check':
            assert client.post(path+'/messages', headers=headers, json={'message':'old','request_key':'old'}).status_code == 202
            old_id = wait(client, path, headers, ('queued',))['request_id']
        body = {'message':'new', 'request_key':'retry-after-restart'}
        with ThreadPoolExecutor(max_workers=3) as executor:
            try:
                if arrival == 'during_shutdown':
                    shutdown = executor.submit(client.__exit__, None, None, None)
                    assert finishing.wait(5) and service.shutting_down
                elif arrival == 'waiting_for_finalizer':
                    stopping = executor.submit(client.post, path+'/stop', headers=headers)
                    assert finishing.wait(5)
                sender = executor.submit(client.post, path+'/messages', headers=headers, json=body)
                assert checked.wait(5)
                if arrival == 'during_enqueue_check': assert enqueue_check.wait(5)
                if shutdown is None: shutdown = executor.submit(client.__exit__, None, None, None)
                for _ in range(200):
                    if service.shutting_down: break
                    time.sleep(.01)
                assert service.shutting_down
                release_finish.set()
                assert closing.wait(5)  # Local execution cleanup has finished.
                release_check.set()
                response = sender.result(timeout=5)
                assert response.status_code == 503, response.text
                assert '消息未接收' in response.json()['detail']
                assert len(service.jobs()) == int(old_id is not None) and not calls
                current = client.get(path, headers=headers).json()
                assert not any(e['kind']=='user' and e['text']=='new' for e in current['events'])
            finally:
                release_finish.set(); release_check.set(); release_close.set()
            if stopping: assert stopping.result(timeout=5).status_code == 200
            shutdown.result(timeout=5)
    finally:
        release_finish.set(); release_check.set(); release_close.set()
        if shutdown is None: client.__exit__(None, None, None)
    app, service = application()
    with TestClient(app) as client:
        if arrival == 'during_shutdown':
            async def recovered():
                await asyncio.wait_for(asyncio.shield(app.state.services.local_agents.tasks[pid+':'+path.rsplit('/',1)[-1]]), 5)
            client.portal.call(recovered)
            assert service.job(old_id)['status'] == 'completed' and calls == ['old']
        elif old_id:
            assert service.job(old_id)['status'] == 'interrupted' and not app.state.services.local_agents.tasks
        old_job = service.job(old_id) if old_id else None
        assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
        completed = wait(client, path, headers)
        assert completed['status'] == 'idle' and service.job(completed['request_id'])['status'] == 'completed'
        assert calls == (['old','new'] if arrival == 'during_shutdown' else ['new'])
        assert client.post(path+'/messages', headers=headers, json=body).status_code == 202
        assert len(service.jobs()) == 1 + int(old_id is not None)
        if old_id: assert service.job(old_id) == old_job
        assert (settings.workspace_root/pid/'results/new.txt').read_text() == 'executed once'


@pytest.mark.parametrize('stop_before_shutdown,fault', [
    *[(False, fault) for fault in ('none', 'record', 'session')],
    *[(True, fault) for fault in ('none', 'record', 'session', 'job')],
])
@pytest.mark.parametrize('timing', ['initial', 'followup'])
def test_shutdown_recovery_respects_explicit_stop_without_replaying_finished_work(
        tmp_path, monkeypatch, fault, timing, stop_before_shutdown):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from agent_platform.api import create_app
    from agent_platform.config import Settings

    settings = Settings(_env_file=None, api_token='admin-boot', data_dir=tmp_path/'data',
                        workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                        scheduler_poll_seconds=3600)
    calls, failures = [], []
    first_ready = threading.Event()
    release_first = threading.Event()
    async def turn(self, message, on_event, on_tool, **kwargs):
        text = json.loads(message)['user_message']
        calls.append(text)
        await on_tool('project_file', {'action':'write', 'path':'results/'+text+'.txt', 'content':'saved once'})
        if text == 'running':
            first_ready.set()
            while not release_first.is_set(): await asyncio.sleep(.01)
        return {'status':'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    def application():
        app = create_app(settings)
        service = app.state.services.official_agent
        service.client_factory = FakeAgent
        service.save_config(ServiceConfig(enabled=True, concurrency=1))
        async def inspect():
            service.rate_limits = {'rateLimits': {'primary': {'usedPercent':10}}}
            return {'error':'', 'dispatch_reason':service.quota_reason()}
        service.inspect = inspect
        return app, service
    app, service = application()
    with TestClient(app) as client:
        _, headers = signup(client, 'RestartWorker')
        pid = project(client, headers); enable(client, pid)
        first = chat(client, pid, headers)
        second = chat(client, pid, headers) if timing == 'initial' else first
        assert client.post(first+'/messages', headers=headers, json={'message':'running','request_key':'running'}).status_code == 202
        assert first_ready.wait(5)
        if timing == 'followup':
            service.save_config(ServiceConfig(enabled=True, concurrency=1, reserve_percent=100))
        assert client.post(second+'/messages', headers=headers, json={'message':'queued','request_key':'queued'}).status_code == 202
        if timing == 'followup': release_first.set()
        state = wait(client, second, headers, ('queued',))
        queued_id, first_id = state['request_id'], client.get(first, headers=headers).json()['request_id']
        assert calls == ['running']
        target = app.state.services.local_agents.root/pid/'conversations'/second.rsplit('/',1)[-1]/'session.json'
        original_replace = Path.replace
        def reject_shutdown_save(source, destination):
            if fault != 'none' and Path(destination) == target and not failures:
                state = json.loads(source.read_text())
                last = state['events'][-1] if state['events'] else {}
                terminal = state['status'] == 'interrupted'
                if last.get('text') == '已停止，可以继续' and (fault == 'session' and terminal or fault == 'record' and not terminal):
                    failures.append(state)
                    raise OSError('queued shutdown save rejected')
            return original_replace(source, destination)
        monkeypatch.setattr(Path, 'replace', reject_shutdown_save)
        original_update = service.update
        def reject_stop_job(job_id, **values):
            if fault == 'job' and job_id == queued_id and values.get('status') == 'interrupted' and not failures:
                failures.append(values)
                raise OSError('queued stop job save rejected')
            return original_update(job_id, **values)
        monkeypatch.setattr(service, 'update', reject_stop_job)
        if stop_before_shutdown:
            response = client.post(second+'/stop', headers=headers)
            assert response.status_code == 200
            assert response.json()['status'] == ('interrupted' if fault == 'none' else 'error')
            assert calls == ['running']
            if timing == 'initial': assert client.get(first, headers=headers).json()['status'] == 'running'
    assert len(failures) == (fault != 'none')
    stopped_status = 'interrupted' if fault == 'none' else 'error'
    queued_status = stopped_status if stop_before_shutdown else 'queued'
    assert service.job(first_id)['status'] == ('interrupted' if timing == 'initial' else queued_status)
    assert service.job(queued_id)['status'] == queued_status
    assert (service.job(queued_id)['ended'] is None) == (not stop_before_shutdown)
    workspace = settings.workspace_root/pid/'results'
    assert (workspace/'running.txt').read_text() == 'saved once'
    assert not (workspace/'queued.txt').exists()
    app, service = application()
    with TestClient(app) as client:
        if stop_before_shutdown:
            assert not app.state.services.local_agents.tasks
            state = client.get(second, headers=headers).json()
            assert state['status'] == stopped_status and state['request_id'] == queued_id
            assert service.job(queued_id)['status'] == stopped_status
            assert calls == ['running'] and not (workspace/'queued.txt').exists()
            assert (workspace/'running.txt').read_text() == 'saved once'
            return
        # The previous shutdown error can still be visible while startup has
        # scheduled recovery. Observe that actual task, not the old snapshot.
        async def recovered():
            task = app.state.services.local_agents.tasks[pid+':'+second.rsplit('/',1)[-1]]
            await asyncio.wait_for(asyncio.shield(task), 5)
        client.portal.call(recovered)
        state = client.get(second, headers=headers).json()
        assert state['status'] == 'idle', state
        assert state['request_id'] == queued_id
        assert service.job(queued_id)['status'] == 'completed'
        assert service.job(first_id)['status'] == ('interrupted' if timing == 'initial' else 'completed')
        assert len(service.jobs()) == (2 if timing == 'initial' else 1)
        assert calls == ['running', 'queued']
        assert (workspace/'running.txt').read_text() == (workspace/'queued.txt').read_text() == 'saved once'


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


@pytest.mark.parametrize('ending', ['missing_executable','start_rejected','protocol_error','stop'])
def test_startup_failure_or_stop_releases_slot_only_after_cleanup(official,ending):
    import errno
    client,app,service=official
    service.save_config(ServiceConfig(enabled=True,concurrency=1,max_tokens=None))
    entered,release_start,closing,release_close=(threading.Event() for _ in range(4))
    instances=[];turns=[]
    class StartingAgent(FakeAgent):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.first=not instances
            instances.append(self)
        async def start(self,tools,instructions,thread_id=None):
            if not self.first:return await super().start(tools,instructions,thread_id)
            entered.set()
            while not release_start.is_set():await asyncio.sleep(.01)
            if ending=='missing_executable':
                raise FileNotFoundError(errno.ENOENT,'No such file or directory','/fixture/missing-codex')
            if ending=='start_rejected':raise CodexError('native startup rejected')
            if ending=='protocol_error':raise CodexError('Codex 协议连接失败：fixture invalid JSON')
            await asyncio.Event().wait()
        async def turn(self,message,on_event,on_tool,**kwargs):
            turns.append(json.loads(message)['user_message'])
            await on_tool('project_file',{'action':'write','path':'results/next.txt','content':'answer=42'})
            return {'status':'completed'}
        async def close(self):
            if self.first:
                closing.set()
                while not release_close.is_set():await asyncio.sleep(.01)
    service.client_factory=StartingAgent
    _,headers=signup(client,'StartupSlotWorker')
    pid=project(client,headers);enable(client,pid)
    first,next_chat=chat(client,pid,headers),chat(client,pid,headers)
    def job(path):return next(row for row in service.jobs() if row['conversation_id']==path.rsplit('/',1)[-1])
    assert client.post(first+'/messages',headers=headers,json={'message':'first','request_key':'first'}).status_code==202
    assert entered.wait(5)
    assert job(first)['status']=='running'
    assert client.post(next_chat+'/messages',headers=headers,json={'message':'next','request_key':'next'}).status_code==202
    wait(client,next_chat,headers,('queued',))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            stopping=executor.submit(client.post,first+'/stop',headers=headers) if ending=='stop' else None
            try:
                if ending!='stop':release_start.set()
                assert closing.wait(5)
                assert job(first)['status']=='running' and job(next_chat)['status']=='queued'
                assert client.get(first,headers=headers).json()['status']=='running'
                assert len(instances)==1 and not turns
                if stopping:assert not stopping.done()
            finally:
                release_close.set()
            if stopping:assert stopping.result(timeout=5).status_code==200
        failed=wait(client,first,headers)
        expected={'missing_executable':'/fixture/missing-codex','start_rejected':'native startup rejected',
                  'protocol_error':'协议连接失败'}
        assert failed['status']==('interrupted' if ending=='stop' else 'error')
        if ending!='stop':assert expected[ending] in failed['error']
        original_job=job(first)
        completed=wait(client,next_chat,headers)
        assert completed['status']=='idle' and job(next_chat)['status']=='completed'
        assert original_job==job(first) and job(first)['status']==failed['status']
        assert job(first)['error']==failed['error']
        assert turns==['next'] and len(instances)==2 and len(service.jobs())==2
        assert (app.state.services.settings.workspace_root/pid/'results/next.txt').read_text()=='answer=42'
        assert not service.active_jobs()
    finally:
        release_start.set();release_close.set()


@pytest.mark.parametrize('revocation', ['member','capability'])
@pytest.mark.parametrize('cleanup_fails', [False,True])
def test_access_revoked_during_startup_preserves_reason_and_releases_slot(
        official,revocation,cleanup_fails):
    from agent_platform.conversation_scope import conversation_scope
    client,app,service=official
    service.save_config(ServiceConfig(enabled=True,concurrency=1,max_tokens=None))
    entered,release_start,closing,release_close=(threading.Event() for _ in range(4))
    connections=[];turns=[]
    class StartingAgent(FakeAgent):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.first=not connections
            self.close_calls=0
            connections.append(self)
        async def start(self,*args,**kwargs):
            if self.first:
                entered.set()
                while not release_start.is_set():await asyncio.sleep(.01)
            return await super().start(*args,**kwargs)
        async def turn(self,message,on_event,on_tool,**kwargs):
            turns.append(json.loads(message)['user_message'])
            await on_tool('project_file',{'action':'write','path':'results/next.txt','content':'answer=42'})
            return {'status':'completed'}
        async def close(self):
            self.close_calls+=1
            if self.first and self.close_calls==1:
                closing.set()
                while not release_close.is_set():await asyncio.sleep(.01)
                if cleanup_fails:raise OSError('startup cleanup failed')
    service.client_factory=StartingAgent
    _,owner=signup(client,'StartupAccessOwner')
    member,worker=signup(client,'StartupAccessWorker')
    first_pid=project(client,owner);next_pid=project(client,owner)
    for pid in (first_pid,next_pid):enable(client,pid)
    base='/api/v1/projects/'+first_pid
    assert client.post(base+'/access-members',headers=owner,json={'name':'StartupAccessWorker'}).status_code==200
    first=chat(client,first_pid,worker);next_chat=chat(client,next_pid,owner)
    workspace=app.state.services.settings.workspace_root/first_pid
    original=workspace/'original.txt';original.write_text('original input')
    def job(path):return next(row for row in service.jobs() if row['conversation_id']==path.rsplit('/',1)[-1])
    def first_state():
        async def read():
            with conversation_scope(first_pid,first.rsplit('/',1)[-1]):
                return app.state.services.local_agents.load(first_pid)
        return client.portal.call(read)
    try:
        assert client.post(first+'/messages',headers=worker,json={'message':'first'}).status_code==202
        assert entered.wait(5)
        assert client.post(next_chat+'/messages',headers=owner,json={'message':'next'}).status_code==202
        wait(client,next_chat,owner,('queued',))
        if revocation=='member':
            response=client.delete(base+'/access-members/'+member['user']['id'],headers=owner)
        else:
            response=client.put(base+'/capabilities',headers=ADMIN,json={'agent_modules_enabled':False})
        assert response.status_code==200,response.text
        release_start.set()
        assert closing.wait(5)
        assert job(first)['status']=='running' and job(next_chat)['status']=='queued'
        assert first_state()['status']=='running'
        assert len(connections)==1 and not turns
        release_close.set()
        for _ in range(300):
            failed=first_state()
            if failed['status'] in {'error','idle','interrupted'}:break
            time.sleep(.01)
        assert failed['status']=='error'
        expected='没有访问权限' if revocation=='member' else '此项目未获准使用官方智能体'
        assert expected in failed['error']
        assert ('startup cleanup failed' in failed['error'])==cleanup_fails
        previous_job=job(first)
        assert wait(client,next_chat,owner)['status']=='idle'
        assert turns==['next'] and job(next_chat)['status']=='completed'
        assert job(first)==previous_job and job(first)['error']==failed['error']
        assert not service.active_jobs() and original.read_text()=='original input'
        assert not (workspace/'results/next.txt').exists()
        assert (app.state.services.settings.workspace_root/next_pid/'results/next.txt').read_text()=='answer=42'
    finally:
        release_start.set();release_close.set()


@pytest.mark.parametrize('ending', ['release','stop'])
def test_first_request_waits_for_slot_before_creating_an_empty_provider_thread(official,tmp_path,ending):
    from agent_platform.codex_app_server import CodexAppServer
    client,app,service=official
    executable=tmp_path/'protocol_fixture'
    trace=tmp_path/'calls.jsonl'
    executable.write_text(f'#!{sys.executable}\ntrace={str(trace)!r}\n'+'''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
def record(method):
    with open(trace,'a') as stream:stream.write(json.dumps({'method':method})+'\\n')
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method=='thread/start':
        record(method);emit({'id':m['id'],'result':{'thread':{'id':'empty-root'}}})
    elif method=='thread/resume':
        record(method);emit({'id':m['id'],'error':{'code':-32000,'message':'no rollout found for thread id empty-root'}})
    elif method=='turn/start':
        record(method);emit({'id':m['id'],'result':{'turn':{'id':'first-turn'}}})
        emit({'id':'write','method':'item/tool/call','params':{'threadId':'empty-root','turnId':'first-turn',
            'tool':'project_file','arguments':{'action':'write','path':'results/first.txt','content':'written once'}}})
    elif m.get('id')=='write':
        assert m['result']['success'];record('tool_result')
        emit({'method':'turn/completed','params':{'threadId':'empty-root','turn':{'id':'first-turn','status':'completed'}}})
    elif method=='fixture/exit':emit({'id':m['id'],'result':{}});sys.exit(0)
''')
    executable.chmod(0o700)
    connections=[]
    def factory(*args,**kwargs):
        connection=CodexAppServer(*args,**kwargs);connections.append(connection);return connection
    service.client_factory=factory
    service.save_config(ServiceConfig(enabled=True,executable=str(executable),max_tokens=None))
    app.state.services.settings.official_agent_egress_enabled=True  # Python protocol fixture only.
    _,headers=signup(client,'FirstQueueWorker')
    pid=project(client,headers);enable(client,pid);path=chat(client,pid,headers)
    acquired=service.acquire;waiting=threading.Event()
    async def observed_acquire(*args,**kwargs):
        waiting.set();return await acquired(*args,**kwargs)
    service.acquire=observed_acquire
    client.portal.call(service.dispatch_lock.acquire)
    try:
        assert client.post(path+'/messages',headers=headers,json={'message':'首次执行','request_key':'first'}).status_code==202
        assert waiting.wait(5)
        created_while_queued=len(connections)
        assert client.get(path,headers=headers).json()['status']=='queued'
        if connections:
            # Reproduce the old ordering: the never-run thread has no rollout
            # once this process exits while its first request is still queued.
            client.portal.call(connections[0].request,'fixture/exit',{})
            async def disconnected():await asyncio.wait_for(asyncio.shield(connections[0].reader),3)
            client.portal.call(disconnected)
        if ending=='stop':assert client.post(path+'/stop',headers=headers).status_code==200
    finally:
        client.portal.call(service.dispatch_lock.release)
    state=wait(client,path,headers)
    calls=[json.loads(line)['method'] for line in trace.read_text().splitlines()] if trace.exists() else []
    if ending=='release':
        assert state['status']=='idle',state['error']
        assert calls==['thread/start','turn/start','tool_result']
        assert (app.state.services.settings.workspace_root/pid/'results/first.txt').read_text()=='written once'
        assert len(connections)==1 and service.jobs()[0]['status']=='completed'
    else:
        assert state['status']=='interrupted' and service.jobs()[0]['status']=='interrupted'
        assert not calls and not connections
        assert not (app.state.services.settings.workspace_root/pid/'results/first.txt').exists()
    assert created_while_queued==0
    assert len(service.jobs())==1


@pytest.mark.parametrize('operation', ['start', 'resume'])
@pytest.mark.parametrize('reply', ['pending', 'received'])
def test_stop_during_native_thread_setup_preserves_input_and_explicit_retry(official, tmp_path, operation, reply):
    from agent_platform.codex_app_server import CodexAppServer
    client, app, service = official
    executable = tmp_path/'thread-setup-fixture'
    trace = tmp_path/'trace.jsonl'
    hold, ready, release = (tmp_path/name for name in ('hold', 'ready', 'release'))
    executable.write_text(f'#!{sys.executable}\nROOT={str(tmp_path)!r}\nREPLY={reply!r}\n' + '''
import json,sys,time
from pathlib import Path
def emit(v):print(json.dumps(v),flush=True)
def record(**v):
    with (Path(ROOT)/'trace.jsonl').open('a') as f:f.write(json.dumps(v)+'\\n')
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method in {'thread/start','thread/resume'}:
        record(method=method,thread_id=m['params'].get('threadId'))
        if (Path(ROOT)/'hold').exists() and REPLY=='pending':
            (Path(ROOT)/'ready').touch()
            while not (Path(ROOT)/'release').exists():time.sleep(.01)
        emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        context=json.loads(m['params']['input'][0]['text']);role=context['user_message']
        record(method=method,context=context)
        emit({'id':m['id'],'result':{'turn':{'id':role}}})
        emit({'id':'write','method':'item/tool/call','params':{'threadId':'root','turnId':role,
            'tool':'project_file','arguments':{'action':'write','path':'results/'+role+'.txt','content':'executed once'}}})
    elif m.get('id')=='write':
        assert m['result']['success']
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':role,'status':'completed'}}})
''')
    executable.chmod(0o700)
    connections = []
    cleaning, release_cleanup = threading.Event(), threading.Event()
    def factory(*args, **kwargs):
        connection = CodexAppServer(*args, **kwargs); connections.append(connection)
        request, close = connection.request, connection.close
        async def observed_request(method, params, **options):
            result = await request(method, params, **options)
            if method == 'thread/'+operation and reply == 'received' and hold.exists():
                ready.touch()
                while not release.exists(): await asyncio.sleep(.01)
            return result
        async def observed_close():
            if hold.exists() and connection.process and connection.process.returncode is None:
                cleaning.set()
                while not release_cleanup.is_set(): await asyncio.sleep(.01)
            await close()
        connection.request, connection.close = observed_request, observed_close
        return connection
    service.client_factory = factory
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), max_tokens=None))
    app.state.services.settings.official_agent_egress_enabled = True  # Python protocol fixture only.
    _, headers = signup(client, 'ThreadSetupWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers)
    before = client.get(path, headers=headers).json(); history_job = None
    if operation == 'resume':
        assert client.post(path+'/messages', headers=headers, json={'message':'history','request_key':'history'}).status_code == 202
        before = wait(client, path, headers); assert before['status'] == 'idle'
        history_job = service.job(before['request_id'])
        client.portal.call(connections[-1].close)
    hold.touch()
    original = {'message':'work', 'request_key':'work'}
    assert client.post(path+'/messages', headers=headers, json=original).status_code == 202
    try:
        for _ in range(300):
            if ready.exists(): break
            time.sleep(.01)
        else: raise AssertionError('Native thread setup did not reach the selected reply boundary')
        blocked = client.get(path, headers=headers).json(); old_id = blocked['request_id']
        assert blocked['thread_id'] == before.get('thread_id')
        supplement = {'message':'supplement', 'request_key':'supplement'}
        for _ in range(2): assert client.post(path+'/messages', headers=headers, json=supplement).status_code == 202
        with ThreadPoolExecutor(max_workers=1) as executor:
            stopping = executor.submit(client.post, path+'/stop', headers=headers)
            try:
                assert cleaning.wait(5) and not stopping.done()
                assert connections[-1].process.returncode is None
                assert client.get(path, headers=headers).json()['status'] == 'running'
                assert service.job(old_id)['status'] == 'running'
            finally: release_cleanup.set()
            assert stopping.result(timeout=5).status_code == 200
        stopped = client.get(path, headers=headers).json()
        assert stopped['status'] == 'interrupted' and connections[-1].process.returncode is not None
        assert stopped['thread_id'] == before.get('thread_id')
        assert sum(e['kind']=='user' and e['text']=='work' for e in stopped['events']) == 1
        assert sum(e['kind']=='user' and e['text']=='supplement' for e in stopped['events']) == 1
        old_job = service.job(old_id)
        assert client.post(path+'/messages', headers=headers, json=original).status_code == 202
        assert service.job(old_id) == old_job and client.get(path, headers=headers).json()['status'] == 'interrupted'
        release.touch(); hold.unlink()
        retry = {'message':'retry', 'request_key':'explicit-retry'}
        assert client.post(path+'/messages', headers=headers, json=retry).status_code == 202
        final = wait(client, path, headers); assert final['status'] == 'idle', final
        assert final['request_id'] != old_id and service.job(old_id) == old_job
        assert client.post(path+'/messages', headers=headers, json=retry).status_code == 202
        contexts = [v['context'] for line in trace.read_text().splitlines() if (v:=json.loads(line))['method']=='turn/start']
        assert [v['user_message'] for v in contexts] == (['history','retry'] if history_job else ['retry'])
        assert contexts[-1]['latest_messages'] == ['supplement','retry']
        assert len(service.jobs()) == 2 + int(history_job is not None)
        if history_job: assert service.job(history_job['id']) == history_job
        workspace = app.state.services.settings.workspace_root/pid
        assert not (workspace/'results/work.txt').exists()
        assert (workspace/'results/retry.txt').read_text() == 'executed once'
        assert {e['id'] for e in before['events']} <= {e['id'] for e in final['events']}
    finally:
        release.touch(); release_cleanup.set()
        if hold.exists(): hold.unlink()


@pytest.mark.parametrize('operation', ['start', 'resume'])
@pytest.mark.parametrize('write_result', ['not_written', 'written'])
@pytest.mark.parametrize('stop_while_cleaning', [False, True])
def test_thread_state_save_failure_preserves_error_input_and_explicit_retry(official, monkeypatch, operation, write_result, stop_while_cleaning):
    from pathlib import Path
    client, app, service = official
    manager = app.state.services.local_agents
    entered, release_start, cleaning, release_cleanup = (threading.Event() for _ in range(4))
    stop_entered = threading.Event()
    original_stop = manager.stop
    async def stop(project_id):
        stop_entered.set()
        return await original_stop(project_id)
    monkeypatch.setattr(manager, 'stop', stop)
    starts, contexts, valid_threads = [], [], set()
    state = {'block': False, 'armed': False, 'failed': False, 'target': None}
    class Agent(FakeAgent):
        async def start(self, tools, instructions, thread_id=None):
            starts.append(thread_id)
            if thread_id and thread_id not in valid_threads:
                raise CodexError(f'no rollout found for thread id {thread_id}')
            if state['block']:
                state['target'] = self
                entered.set()
                while not release_start.is_set(): await asyncio.sleep(.01)
            value = await super().start(tools, instructions, thread_id)
            if state['block']: state['armed'] = True
            return value
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message); contexts.append(context); valid_threads.add(self.thread_id)
            await on_tool('project_file', {'action':'write', 'path':'results/'+context['user_message']+'.txt', 'content':'executed once'})
            return {'status':'completed'}
        async def close(self):
            if self is state['target'] and state['block']:
                cleaning.set()
                while not release_cleanup.is_set(): await asyncio.sleep(.01)
            self.closed = True
    service.client_factory = Agent
    _, headers = signup(client, 'ThreadSaveWorker')
    pid = project(client, headers); enable(client, pid); path = chat(client, pid, headers); cid = path.rsplit('/',1)[-1]
    before = client.get(path, headers=headers).json(); history_job = None
    if operation == 'resume':
        assert client.post(path+'/messages', headers=headers, json={'message':'history','request_key':'history'}).status_code == 202
        before = wait(client, path, headers); assert before['status'] == 'idle'
        history_job = service.job(before['request_id'])
        client.portal.call(manager.clients.pop(pid+':'+cid).close)
    target = manager.root/pid/'conversations'/cid/'session.json'
    original_replace = Path.replace
    def replace(source, destination):
        if Path(destination) == target and state['armed'] and not state['failed']:
            candidate = json.loads(source.read_text())
            assert candidate['thread_id'] == state['target'].thread_id
            state['failed'] = True
            if write_result == 'written': original_replace(source, destination)
            raise OSError('thread state save failed once')
        return original_replace(source, destination)
    monkeypatch.setattr(Path, 'replace', replace)
    state['block'] = True
    original = {'message':'work','request_key':'work'}
    try:
        assert client.post(path+'/messages', headers=headers, json=original).status_code == 202
        assert entered.wait(5)
        old_id = client.get(path, headers=headers).json()['request_id']
        supplement = {'message':'supplement','request_key':'supplement'}
        for _ in range(2): assert client.post(path+'/messages', headers=headers, json=supplement).status_code == 202
        release_start.set(); assert cleaning.wait(5) and state['failed']
        assert client.get(path, headers=headers).json()['status'] == 'running' and service.job(old_id)['status'] == 'running'
        assert [c['user_message'] for c in contexts] == (['history'] if history_job else [])
        failed_thread = state['target'].thread_id
        if stop_while_cleaning:
            with ThreadPoolExecutor(max_workers=1) as executor:
                stopping = executor.submit(client.post, path+'/stop', headers=headers)
                try:
                    assert stop_entered.wait(5) and not stopping.done()
                    assert client.get(path, headers=headers).json()['status'] == 'running'
                    assert service.job(old_id)['status'] == 'running'
                finally: release_cleanup.set()
                response = stopping.result(timeout=5)
                assert response.status_code == 200, response.text
                assert response.json()['status'] == 'error'
                assert response.json()['error'] == 'thread state save failed once'
        else:
            release_cleanup.set()
        failed = wait(client, path, headers)
        assert failed['status'] == 'error' and failed['error'] == 'thread state save failed once'
        assert state['target'].closed
        old_job = service.job(old_id)
        assert old_job['status'] == 'error' and old_job['error'] == failed['error']
        assert failed['thread_id'] == (failed_thread if write_result == 'written' else before['thread_id'])
        assert json.loads(target.read_text())['pending_messages'] == ['supplement']
        assert sum(e['kind']=='user' and e['text']=='work' for e in failed['events']) == 1
        assert sum(e['kind']=='user' and e['text']=='supplement' for e in failed['events']) == 1
        assert client.post(path+'/messages', headers=headers, json=original).status_code == 202
        assert service.job(old_id) == old_job and client.get(path, headers=headers).json()['status'] == 'error'
        state['block'] = False
        retry = {'message':'retry', 'request_key':'explicit-retry'}
        assert client.post(path+'/messages', headers=headers, json=retry).status_code == 202
        final = wait(client, path, headers); assert final['status'] == 'idle', final
        assert final['request_id'] != old_id and service.job(old_id) == old_job
        assert client.post(path+'/messages', headers=headers, json=retry).status_code == 202
        assert [c['user_message'] for c in contexts] == (['history','retry'] if history_job else ['retry'])
        assert contexts[-1]['latest_messages'] == ['supplement','retry']
        assert starts == ([None,before['thread_id'],before['thread_id']] if history_job else
                          [None,failed_thread,None] if write_result == 'written' else [None,None])
        if operation == 'start' and write_result == 'written':
            assert any(e['role']=='user' and e['text']=='work' for e in contexts[-1]['recent_project_messages'])
        assert len(service.jobs()) == 2 + int(history_job is not None)
        if history_job: assert service.job(history_job['id']) == history_job
        workspace = app.state.services.settings.workspace_root/pid
        assert not (workspace/'results/work.txt').exists()
        assert (workspace/'results/retry.txt').read_text() == 'executed once'
        assert not json.loads(target.read_text()).get('pending_messages')
        assert {e['id'] for e in before['events']} <= {e['id'] for e in final['events']}
    finally:
        release_start.set(); release_cleanup.set(); state['block'] = False


@pytest.mark.parametrize('idle_fault,fault_timing', [
    (fault,timing) for fault in ('disconnect','invalid_json','healthy','resume_rejected','closed_input')
    for timing in ('idle','queued')
] + [('disconnect','queue_stop'), ('resume_stopped','queued')])
def test_next_message_reconnects_closed_idle_client_without_replaying_completed_work(
        official, tmp_path, idle_fault, fault_timing):
    from agent_platform.codex_app_server import CodexAppServer
    client, app, service = official
    executable = tmp_path/'protocol_fixture'
    trace = tmp_path/'protocol_calls.jsonl'
    executable.write_text(f'#!{sys.executable}\nidle_fault={idle_fault!r}\ntrace={str(trace)!r}\n' + '''
import json,sys,time
from pathlib import Path
def emit(value):print(json.dumps(value),flush=True)
def record(**value):
    with open(trace,'a') as stream:stream.write(json.dumps(value)+'\\n')
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='account/read':emit({'id':m['id'],'result':{'account':{'type':'chatgpt'}}})
    elif method in {'thread/start','thread/resume'}:
        record(method=method,thread_id=m['params'].get('threadId'))
        if method=='thread/resume' and idle_fault=='resume_stopped':continue
        elif method=='thread/resume' and idle_fault=='resume_rejected':
            emit({'id':m['id'],'error':{'code':-32000,'message':'native resume denied'}})
        else:emit({'id':m['id'],'result':{'thread':{'id':'original-root'}}})
    elif method=='turn/start':
        message=json.loads(m['params']['input'][0]['text'])['user_message']
        phase='first' if message=='保存一次' else 'second'
        record(method=method,message=message)
        emit({'id':m['id'],'result':{'turn':{'id':phase+'-turn'}}})
        emit({'method':'thread/started','params':{'thread':{'id':phase+'-child','parentThreadId':'original-root'}}})
        emit({'id':phase,'method':'item/tool/call','params':{'threadId':phase+'-child',
            'turnId':phase+'-child-turn','tool':'project_file',
            'arguments':{'action':'write','path':'results/'+phase+'.txt','content':phase+' result'}}})
    elif m.get('id') in {'first','second'}:
        phase=m['id'];assert m['result']['success']
        record(method='tool_result',phase=phase)
        emit({'method':'turn/completed','params':{'threadId':phase+'-child','turn':{'id':phase+'-child-turn','status':'completed'}}})
        emit({'method':'turn/completed','params':{'threadId':'original-root','turn':{'id':phase+'-turn','status':'completed'}}})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':m['params']['threadId'],'parentThreadId':'original-root','status':{'type':'idle'}}}})
    elif method=='fixture/idle_fault':
        emit({'id':m['id'],'result':{}})
        if idle_fault in {'disconnect','resume_rejected','resume_stopped'}:sys.exit(0)
        print('invalid json',flush=True)
if idle_fault=='closed_input':
    Path('stdin-eof').write_text('input closed; process remains alive')
    time.sleep(60)
''')
    executable.chmod(0o700)
    connections = []
    def factory(*args, **kwargs):
        connection = CodexAppServer(*args, **kwargs)
        connections.append(connection)
        return connection
    service.client_factory = factory
    service.save_config(ServiceConfig(enabled=True, executable=str(executable), max_tokens=None))
    app.state.services.settings.official_agent_egress_enabled = True  # Python protocol fixture only.
    _, headers = signup(client,'IdleReconnectWorker')
    pid = project(client,headers); enable(client,pid)
    path = chat(client,pid,headers)
    assert client.post(path+'/messages',headers=headers,json={'message':'保存一次','request_key':'first'}).status_code == 202
    before = wait(client,path,headers)
    assert before['status'] == 'idle', before
    first_job = service.jobs()[0]
    assert first_job['status'] == 'completed'
    workspace = app.state.services.settings.workspace_root/pid
    assert (workspace/'results/first.txt').read_text() == 'first result'
    original = connections[0]
    original_process = original.process
    queued = fault_timing != 'idle'
    acquired = service.acquire
    waiting = threading.Event()
    async def observed_acquire(*args, **kwargs):
        waiting.set()
        return await acquired(*args, **kwargs)
    service.acquire = observed_acquire
    if queued:client.portal.call(service.dispatch_lock.acquire)
    try:
        if queued:
            assert client.post(path+'/messages',headers=headers,json={'message':'继续处理','request_key':'second'}).status_code == 202
            assert waiting.wait(5)
        if idle_fault == 'closed_input':
            async def close_input():
                original_process.stdin.close()
                await original_process.stdin.wait_closed()
            client.portal.call(close_input)
            for _ in range(300):
                if (original.runtime_dir/'empty-workspace/stdin-eof').exists():break
                time.sleep(.01)
            else:raise AssertionError('Protocol process did not observe input EOF')
            assert original_process.returncode is None and not original.reader.done() and not original.closing
        elif idle_fault != 'healthy':
            client.portal.call(original.request,'fixture/idle_fault',{})
            async def reader_finished():await asyncio.wait_for(asyncio.shield(original.reader),3)
            client.portal.call(reader_finished)
            assert original.closing
            assert (original_process.returncode is None) == (idle_fault == 'invalid_json')
        assert client.get(path,headers=headers).json()['status'] == ('queued' if queued else 'idle')
        assert service.job(first_job['id']) == first_job
        assert [json.loads(line)['message'] for line in trace.read_text().splitlines()
                if json.loads(line)['method'] == 'turn/start'] == ['保存一次']
        if fault_timing == 'queue_stop':
            assert client.post(path+'/stop',headers=headers).status_code == 200
        elif not queued:
            assert client.post(path+'/messages',headers=headers,json={'message':'继续处理','request_key':'second'}).status_code == 202
    finally:
        if queued:client.portal.call(service.dispatch_lock.release)
    if idle_fault == 'resume_stopped':
        for _ in range(250):
            if any(json.loads(line)['method'] == 'thread/resume' for line in trace.read_text().splitlines()):break
            time.sleep(.02)
        else:raise AssertionError('Original thread resume was not attempted')
        assert client.get(path,headers=headers).json()['status'] == 'running'
        assert client.post(path+'/stop',headers=headers).status_code == 200
    after = wait(client,path,headers)
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    starts = [entry for entry in calls if entry['method'] in {'thread/start','thread/resume'}]
    messages = [entry['message'] for entry in calls if entry['method'] == 'turn/start']
    results = [entry['phase'] for entry in calls if entry['method'] == 'tool_result']
    if fault_timing == 'queue_stop' or idle_fault == 'resume_stopped':
        assert after['status'] == 'interrupted'
        assert messages == ['保存一次'] and results == ['first']
        assert not (workspace/'results/second.txt').exists()
    elif idle_fault == 'resume_rejected':
        assert after['status'] == 'error' and after['error'] == 'native resume denied'
        assert messages == ['保存一次'] and results == ['first']
        assert not (workspace/'results/second.txt').exists()
    else:
        assert after['status'] == 'idle', after
        assert messages == ['保存一次','继续处理'] and results == ['first','second']
        assert (workspace/'results/second.txt').read_text() == 'second result'
        assert client.post(path+'/messages',headers=headers,json={'message':'继续处理','request_key':'second'}).status_code == 202
    assert starts == ([{'method':'thread/start','thread_id':None}] +
        ([] if idle_fault == 'healthy' or fault_timing == 'queue_stop'
         else [{'method':'thread/resume','thread_id':'original-root'}]))
    if idle_fault == 'healthy':assert len(connections) == 1 and original.process is original_process
    else:assert original_process.returncode is not None
    assert after['thread_id'] == before['thread_id'] and after['session_id'] == before['session_id']
    assert {e['id'] for e in before['events']} <= {e['id'] for e in after['events']}
    assert service.job(first_job['id']) == first_job and len(service.jobs()) == 2
    assert (workspace/'results/first.txt').read_text() == 'first result'


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


@pytest.mark.parametrize('await_reminder', [True, False])
def test_delivery_reminder_uses_remaining_request_budget_and_never_adds_turns(official, monkeypatch, await_reminder):
    client, app, service = official
    _, headers = signup(client, 'DeliveryWorker')
    pid = project(client, headers); enable(client, pid)
    service.save_config(ServiceConfig(enabled=True, max_seconds=30, max_tokens=None))
    acquire = service.acquire
    async def acquired(project_id, job_id):
        await acquire(project_id, job_id)
        service.update(job_id, execution_seconds=22.2)
    monkeypatch.setattr(service, 'acquire', acquired)
    observed, reminders = [], []
    async def steer(self, message):
        reminders.append(json.loads(message.split('：', 1)[1]))
    async def turn(self, message, on_event, on_tool, **kwargs):
        context = json.loads(message)
        observed.append(context['request_budget'])
        if await_reminder:
            for _ in range(100):
                if reminders:
                    break
                await asyncio.sleep(.01)
            assert len(reminders) == 1
        result = await on_tool('project_file', {'action': 'list'})
        await on_event('item/completed', {'item': {'type': 'agentMessage', 'text': '交付完成'}})
        return {'status': 'completed'}
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    monkeypatch.setattr(FakeAgent, 'steer', steer)
    path = chat(client, pid, headers)
    assert client.post(path + '/messages', headers=headers, json={'message': '交付已有结果'}).status_code == 202
    assert wait(client, path, headers)['status'] == 'idle'
    time.sleep(.4)
    assert len(observed) == 1 and 7.5 < observed[0]['remaining_seconds'] <= 7.8
    assert observed[0]['remaining_tokens'] is None
    assert len(reminders) == int(await_reminder)
    if reminders:
        assert reminders[0]['delivery_due'] and reminders[0]['remaining_seconds'] <= 7.5
    assert len(service.jobs()) == 1 and service.jobs()[0]['status'] == 'completed'
    assert service.config().max_seconds == 30


def test_near_deadline_still_stops_without_automatic_continuation(official, monkeypatch):
    client, app, service = official
    _, headers = signup(client, 'DeadlineWorker')
    pid = project(client, headers); enable(client, pid)
    service.save_config(ServiceConfig(enabled=True, max_seconds=30))
    acquire = service.acquire
    async def acquired(project_id, job_id):
        await acquire(project_id, job_id)
        service.update(job_id, execution_seconds=29.8)
    monkeypatch.setattr(service, 'acquire', acquired)
    calls = []
    async def turn(self, message, on_event, on_tool, **kwargs):
        calls.append(json.loads(message)['request_budget'])
        await asyncio.sleep(10)
    monkeypatch.setattr(FakeAgent, 'turn', turn)
    path = chat(client, pid, headers)
    assert client.post(path + '/messages', headers=headers, json={'message': '超时检查'}).status_code == 202
    state = wait(client, path, headers)
    assert state['status'] == 'error' and '时限' in state['error']
    assert len(calls) == 1 and calls[0]['delivery_due']
    assert len(service.jobs()) == 1 and service.jobs()[0]['status'] == 'error'
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
    monkeypatch.setattr(service, '_transport', transport)
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
