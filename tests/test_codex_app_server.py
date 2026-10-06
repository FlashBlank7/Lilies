import asyncio
import json
import re
import sys

import pytest

from agent_platform.codex_app_server import CodexAppServer, CodexError, validate_codex_version


@pytest.mark.asyncio
async def test_deferred_project_tools_are_namespaced_and_remain_callable(tmp_path):
    from agent_platform.project_agent_tools import project_tool_specs
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json, sys
from pathlib import Path
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        emit({'id':message['id'],'result':{}})
    elif method == 'thread/start':
        tools = message['params']['dynamicTools']
        Path(__file__).with_suffix('.tools.json').write_text(json.dumps(tools))
        assert not any(t.get('deferLoading') for t in tools)
        namespace = next(t for t in tools if t['type'] == 'namespace')
        assert namespace['name'] == 'lilies'
        assert {'project_modeling', 'project_progress', 'project_models',
                'project_knowledge', 'project_search', 'project_web'} <= {t['name'] for t in namespace['tools']}
        assert all(t['deferLoading'] for t in namespace['tools'])
        assert any(t['name'] == 'workflow_run' for t in tools)
        assert any(t['name'] == 'workflow_draft' for t in tools)
        assert message['params']['baseInstructions'].startswith('project tools only\\n')
        assert 'JSON.parse(raw)' in message['params']['baseInstructions']
        assert message['params']['developerInstructions'] == ''
        assert 'model_auto_compact_token_limit' not in message['params']['config']
        emit({'id':message['id'],'result':{'thread':{'id':'t1'}}})
    elif method == 'turn/start':
        emit({'id':message['id'],'result':{'turn':{'id':'u1'}}})
        emit({'id':'modeling','method':'item/tool/call','params':{
            'threadId':'t1','namespace':'lilies','tool':'project_modeling','arguments':{'action':'list'}}})
    elif message.get('id') == 'modeling':
        assert message['result']['success']
        assert json.loads(message['result']['contentItems'][0]['text']) == {'studies':[]}
        emit({'method':'turn/completed','params':{'turn':{'id':'u1','status':'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    calls = []
    async def event(method, params):
        pass
    async def tool(name, arguments):
        calls.append((name, arguments))
        return {'studies': []}
    try:
        specs = project_tool_specs()
        await client.start(specs, 'project tools only')
        advertised = json.loads(executable.with_suffix('.tools.json').read_text())
        flattened = {}
        searchable = {}
        for definition in advertised:
            prefix = definition['description'] if definition.get('type') == 'namespace' else ''
            for tool_spec in definition.get('tools', [definition]):
                flattened[tool_spec['name']] = tool_spec
                searchable[tool_spec['name']] = prefix + tool_spec['description']
        # Namespace prose must not make unrelated tools look like workflow tools.
        hits = {name for name, description in searchable.items()
                if re.search(r'workflow|draft|clone|copy', name + ' ' + description, re.I)}
        assert {'workflow_draft', 'workflow_run', 'project_workflows'} <= hits
        assert not {'project_search', 'project_web', 'project_records'} & hits
        # Deferred discovery preserves every callable name and complete schema.
        assert flattened == {spec['name']: spec for spec in specs}
        assert (await client.turn('查看训练', event, tool, timeout=10))['status'] == 'completed'
        assert calls == [('project_modeling', {'action': 'list'})]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_handshake_scoped_environment_tool_results_and_completion(tmp_path):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialize':
        assert message['params']['capabilities']['experimentalApi']
        emit({'id':message['id'],'result':{}})
    elif method=='thread/start':
        p=message['params']
        assert p['environments']==[] and p['selectedCapabilityRoots']==[]
        assert p['modelProvider']=='openai' and not p['allowProviderModelFallback']
        assert p['config']['features.memories'] is False
        assert p['config']['features.plugins'] is False
        assert p['config']['project_doc_max_bytes']==0
        assert p['config']['model_auto_compact_token_limit']==32768
        emit({'id':message['id'],'result':{'thread':{'id':'t1'},'instructionSources':[]}})
    elif method=='turn/start':
        assert message['params']['environments']==[]
        emit({'id':message['id'],'result':{'turn':{'id':'u1'}}})
        emit({'method':'turn/started','params':{'turn':{'id':'u1'}}})
        emit({'id':'tool-request','method':'item/tool/call','params':{
            'threadId':'t1','turnId':'u1','callId':'c1','tool':'project_file',
            'arguments':{'action':'read','path':'../old-answer.txt'}}})
    elif message.get('id')=='tool-request':
        assert message['result']['success'] is False
        assert message['result']['contentItems'][0]['type']=='inputText'
        emit({'method':'item/completed','params':{'item':{'type':'agentMessage','text':'拒绝跨项目读取'}}})
        emit({'method':'turn/completed','params':{'turn':{'id':'u1','status':'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime', auto_compact_token_limit=32768)
    events = []
    async def event(method, params):
        events.append((method, params))
    async def tool(name, arguments):
        assert name == 'project_file'
        raise ValueError('只能访问本项目')
    try:
        assert await client.start([], 'project tools only') == 't1'
        result = await client.turn('读取项目', event, tool, timeout=10)
        assert result['status'] == 'completed'
        assert any(method == 'item/completed' for method, _ in events)
    finally:
        await client.close()
    assert client.process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('with_tools', [False, True])
async def test_dynamic_tool_result_instructions_follow_the_transport(tmp_path, monkeypatch, resume, with_tools):
    """Start/resume explain the actual text result; graph-only requests stay exact."""
    client = CodexAppServer('unused', tmp_path / 'runtime')
    sent = []

    async def connect():
        client.config = {}

    async def request(method, params):
        sent.append((method, params))
        return {'thread': {'id': 'project-thread'}}

    monkeypatch.setattr(client, 'connect', connect)
    monkeypatch.setattr(client, 'request', request)
    specs = [{'type': 'function', 'name': 'workflow_draft', 'description': 'Read current draft',
              'inputSchema': {'type': 'object', 'properties': {}}}] if with_tools else []
    original_specs = json.loads(json.dumps(specs))
    for _ in range(2):
        await client.start(specs, 'Keep current project instructions', 'project-thread' if resume else None)
    assert specs == original_specs
    assert sent[0] == sent[1]
    method, params = sent[0]
    assert method == ('thread/resume' if resume else 'thread/start')
    instructions = params['baseInstructions']
    if not with_tools:
        assert instructions == 'Keep current project instructions'
        return
    assert instructions.startswith('Keep current project instructions\n')
    assert instructions.count('JSON.parse(raw)') == 1
    assert 'typeof raw === "string"' in instructions
    assert '仍可按任务需要读取最新或更详细的数据' in instructions
    # Compact JSON must preserve Unicode, whitespace inside customer text and
    # the types of nested values across the real tool-response boundary.
    responses = []
    expected = {'revision': 3, 'snapshot': {'workflow': {'nodes': [], 'name': '学习流程'}},
                'text': ' 第一行  \n\t第二行 : , "quoted" ',
                'values': [None, True, False, 2, -1.5, {}, []]}

    async def on_tool(name, arguments):
        assert name == 'workflow_draft' and arguments == {}
        return expected

    async def send(message):
        responses.append(message)

    client.on_tool = on_tool
    monkeypatch.setattr(client, '_send', send)
    await client._server_request({'id': 'read-draft', 'method': 'item/tool/call',
        'params': {'threadId': 'project-thread', 'tool': 'workflow_draft', 'arguments': {}}})
    result = responses[0]['result']
    assert result['success'] is True
    item, = result['contentItems']
    assert item['type'] == 'inputText'
    decoded = json.loads(item['text'])
    assert decoded == expected
    assert [type(value) for value in decoded['values']] == [type(value) for value in expected['values']]
    assert '学习流程' in item['text']
    assert len(item['text'].encode()) < len(json.dumps(expected, ensure_ascii=False).encode())


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['known_usage', 'unknown_usage', 'late_usage', 'invalid',
                                      'missing', 'user_stop', 'ack_only', 'disconnect', 'native_error',
                                      'corrected', 'uncorrected'])
async def test_generation_receiver_stops_before_tool_reply_and_requires_native_completion(tmp_path, scenario):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nscenario={scenario!r}\n' + '''
import json, sys
from pathlib import Path
log = Path(__file__).with_suffix('.requests.jsonl')
def emit(value):
    print(json.dumps(value), flush=True)
def usage():
    emit({'method':'thread/tokenUsage/updated','params':{'tokenUsage':{'total':{
        'inputTokens':100,'outputTokens':20,'totalTokens':120}}}})
def complete(status):
    emit({'method':'turn/completed','params':{'turn':{'id':'u1','status':status}}})
for line in sys.stdin:
    message=json.loads(line); method=message.get('method')
    with log.open('a') as out:
        out.write(json.dumps(message)+'\\n')
    if method=='initialize':
        emit({'id':message['id'],'result':{}})
    elif method=='thread/start':
        emit({'id':message['id'],'result':{'thread':{'id':'t1'}}})
    elif method=='turn/start':
        emit({'id':message['id'],'result':{'turn':{'id':'u1'}}})
        if scenario=='known_usage': usage()
        if scenario=='missing':
            complete('completed')
        else:
            emit({'id':'receiver','method':'item/tool/call','params':{
                'threadId':'t1','turnId':'u1','tool':'return_workflow',
                'arguments':{'workflow':{'nodes':[], 'edges':[]}}}})
    elif message.get('id')=='receiver':
        # A successful tool reply would let code mode sample a confirmation.
        if scenario in ('corrected','uncorrected'):
            assert message['result']['success'] is True
            assert json.loads(message['result']['contentItems'][0]['text'])['structure_check']['valid'] is False
            if scenario=='corrected':
                emit({'id':'receiver2','method':'item/tool/call','params':{
                    'threadId':'t1','turnId':'u1','tool':'return_workflow',
                    'arguments':{'workflow':{'nodes':[], 'edges':[]}}}})
            else: complete('completed')
        else:
            assert scenario=='invalid' and message['result']['success'] is False
            complete('completed')
    elif message.get('id')=='receiver2':
        raise AssertionError('Corrected graph must not trigger a confirmation sample')
    elif method=='turn/interrupt':
        emit({'id':message['id'],'result':{}})
        if scenario=='disconnect': sys.exit(0)
        if scenario=='late_usage': usage()
        if scenario!='ack_only': complete('failed' if scenario=='native_error' else 'interrupted')
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    called, usages = [], []
    receiving = asyncio.Event()

    async def event(method, params):
        if method == 'thread/tokenUsage/updated':
            usages.append(params['tokenUsage']['total'])

    async def receive(name, arguments):
        called.append((name, arguments))
        receiving.set()
        if scenario == 'invalid':
            raise ValueError('Incomplete workflow')
        if scenario == 'user_stop':
            await asyncio.Event().wait()
        if scenario in {'corrected', 'uncorrected'}:
            return {'received': True, 'structure_check': {'valid': len(called) > 1, 'errors': ['missing end']}}
        return {'received': True}

    try:
        await client.start([], 'generation only')
        turn = asyncio.create_task(client.turn('generate', event, receive, finish_on_tool='return_workflow',
            tool_result_ready=lambda name, result: result.get('structure_check', {}).get('valid', True)))
        if scenario == 'user_stop':
            await asyncio.wait_for(receiving.wait(), 2)
            await client.interrupt()
        if scenario == 'ack_only':
            await asyncio.wait_for(receiving.wait(), 2)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(turn, .4)
        elif scenario == 'disconnect':
            with pytest.raises(CodexError):
                await asyncio.wait_for(turn, 2)
        else:
            result = await asyncio.wait_for(turn, 2)
            assert result.get('result_received', False) == (scenario in {'known_usage', 'unknown_usage', 'late_usage', 'corrected'})
            assert result['status'] == ('completed' if scenario in {'missing', 'invalid', 'uncorrected'} else
                                        'failed' if scenario == 'native_error' else 'interrupted')
        assert len(called) == (0 if scenario == 'missing' else 2 if scenario == 'corrected' else 1)
        assert usages == ([{'inputTokens': 100, 'outputTokens': 20, 'totalTokens': 120}]
                          if scenario in {'known_usage', 'late_usage'} else [])
    finally:
        await client.close()
    requests = [json.loads(line) for line in executable.with_suffix('.requests.jsonl').read_text().splitlines()]
    assert sum(message.get('method') == 'turn/start' for message in requests) == 1
    assert [message['id'] for message in requests if message.get('id') == 'receiver'] == (
        ['receiver'] if scenario in {'invalid', 'corrected', 'uncorrected'} else [])
    assert not any(message.get('id') == 'receiver2' for message in requests)


@pytest.mark.asyncio
async def test_resume_long_session_without_rehydrating_history(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        emit({'id': message['id'], 'result': {}})
    elif method == 'thread/resume':
        p = message['params']
        assert p['threadId'] == 'long-existing-thread'
        assert p['baseInstructions'] == 'current project instructions'
        assert p['developerInstructions'] == ''
        turns = [] if p.get('excludeTurns') else [{'text': 'x' * (5 * 1024 * 1024)}]
        emit({'id': message['id'], 'result': {'thread': {'id': p['threadId'], 'turns': turns}}})
    elif method == 'turn/start':
        assert message['params']['threadId'] == 'long-existing-thread'
        assert message['params']['environments'] == []
        emit({'id': message['id'], 'result': {'turn': {'id': 'resumed-turn'}}})
        emit({'method': 'turn/completed', 'params': {'turn': {'id': 'resumed-turn', 'status': 'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')

    async def event(method, params):
        pass

    async def tool(name, arguments):
        pytest.fail('No tool call expected')

    try:
        assert await client.start([], 'current project instructions', 'long-existing-thread') == 'long-existing-thread'
        assert (await client.turn('继续', event, tool, timeout=10))['status'] == 'completed'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restored_session_uses_local_rollout_and_relinks_current_login(tmp_path, monkeypatch):
    login = tmp_path / 'current-login'
    login.mkdir()
    (login / 'auth.json').write_text('test-login')
    monkeypatch.setenv('CODEX_HOME', str(login))
    runtime = tmp_path / 'restored-runtime'
    home = runtime / 'codex-home'
    sessions = home / 'sessions/2026/09/16'
    sessions.mkdir(parents=True)
    rollout = sessions / 'rollout-2026-09-16-restored-thread.jsonl'
    rollout.write_text('{}\n')
    (home / 'auth.json').symlink_to(tmp_path / 'missing-old-login')
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json, sys, os
from pathlib import Path
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'thread/resume':
        p = message['params']
        assert p['threadId'] == 'restored-thread' and p['excludeTurns']
        assert Path(p['path']).is_relative_to(Path(os.environ['CODEX_HOME']) / 'sessions')
        assert Path(p['path']).read_text() == '{}\\n'
        assert (Path(os.environ['CODEX_HOME']) / 'auth.json').read_text() == 'test-login'
        print(json.dumps({'id': message['id'], 'result': {'thread': {'id': p['threadId']}}}), flush=True)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), runtime)
    try:
        assert await client.start([], 'only restored project', 'restored-thread') == 'restored-thread'
        assert (home / 'auth.json').resolve() == (login / 'auth.json').resolve()
        assert rollout.read_text() == '{}\n'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disconnect_fails_pending_request_instead_of_hanging(tmp_path):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nimport sys\nsys.stdin.readline()\n')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime')
    try:
        with pytest.raises(CodexError, match='断开'):
            await client.start([], 'project only')
    finally:
        await client.close()


def test_reject_old_cli_without_environment_isolation():
    with pytest.raises(ValueError):
        validate_codex_version('codex-cli 0.99.0')
    validate_codex_version('codex-cli 0.153.4')


@pytest.mark.asyncio
@pytest.mark.parametrize('activity', ['messages', 'tool', 'idle'])
async def test_turn_idle_timeout_does_not_cut_off_progress_or_running_tools(tmp_path, activity):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nactivity={activity!r}\n' + '''
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
def complete():
    emit({'method':'turn/completed','params':{'turn':{'id':'u1','status':'completed'}}})
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialize':
        emit({'id':message['id'],'result':{}})
    elif method=='thread/start':
        emit({'id':message['id'],'result':{'thread':{'id':'t1'}}})
    elif method=='turn/start':
        emit({'id':message['id'],'result':{'turn':{'id':'u1'}}})
        if activity=='messages':
            for _ in range(5):
                time.sleep(.15)
                emit({'method':'item/agentMessage/delta','params':{'threadId':'t1','delta':'working'}})
            complete()
        elif activity=='tool':
            emit({'id':'tool-request','method':'item/tool/call','params':{
                'threadId':'t1','turnId':'u1','tool':'workflow_run','arguments':{}}})
    elif message.get('id')=='tool-request':
        assert message['result']['success'] is True
        complete()
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    called = []

    async def event(method, params):
        pass

    async def tool(name, arguments):
        called.append(name)
        await asyncio.sleep(.75)  # A tool may take longer than the idle limit.
        return {'status': 'succeeded'}

    try:
        await client.start([], 'project tools only')
        if activity == 'idle':
            with pytest.raises(CodexError, match='长时间无响应.*继续'):
                await client.turn('继续', event, tool, timeout=.4)
            assert client.thread_id == 't1'  # Reconnect can resume the same thread.
        else:
            started = asyncio.get_running_loop().time()
            result = await client.turn('继续', event, tool, timeout=.4)
            assert result['status'] == 'completed'
            assert asyncio.get_running_loop().time() - started > .4
            assert called == (['workflow_run'] if activity == 'tool' else [])
    finally:
        await client.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('account', [None, {'type': 'apiKey'}, {'type': 'chatgpt'}])
async def test_subscription_transport_excludes_api_key_and_personal_home(tmp_path, monkeypatch, account):
    import os
    private = tmp_path / 'account' / 'auth.json'
    private.parent.mkdir(); private.write_text('service-auth')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-inherit')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://must-not-inherit.invalid')
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\naccount = {account!r}\n' + '''
import json, sys, os
from pathlib import Path
assert 'OPENAI_API_KEY' not in os.environ and 'OPENAI_BASE_URL' not in os.environ
assert (Path(os.environ['CODEX_HOME'])/'auth.json').read_text() == 'service-auth'
for line in sys.stdin:
    msg=json.loads(line); method=msg.get('method')
    if method=='initialize':result={}
    elif method=='account/read':result={'account':account}
    elif method=='thread/start':
        assert account == {'type':'chatgpt'}, 'Invalid login must not create a thread'
        p=msg['params'];assert p['model']=='gpt-5.6-luna'
        assert p['config']['forced_login_method']=='chatgpt'
        result={'thread':{'id':'isolated'}}
    elif method=='turn/start':raise AssertionError('Egress disabled')
    else:continue
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
''')
    executable.chmod(0o700)
    client=CodexAppServer(str(executable),tmp_path/'session',model='gpt-5.6-luna',thinking='max',
                          auth_file=private,subscription_only=True,allow_model_calls=False)
    try:
        if account == {'type': 'chatgpt'}:
            assert await client.start([], 'project only') == 'isolated'
        else:
            from agent_platform.codex_app_server import CodexAuthenticationError
            with pytest.raises(CodexAuthenticationError, match='未检测到有效登录' if account is None else 'API Key'):
                await client.start([], 'project only')
            assert client.thread_id is None
        with pytest.raises(CodexError,match='模型出口已关闭'):
            await client.turn('hello',None,None)
    finally:
        await client.close()
