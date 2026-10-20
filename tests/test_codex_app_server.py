import asyncio
import json
import re
import sys

import pytest

from agent_platform.codex_app_server import CodexAppServer, CodexError, validate_codex_version


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['spawn', 'reading'])
@pytest.mark.parametrize('cancel_again', [False, True])
async def test_cancelled_version_probe_waits_for_its_process_and_pipe_cleanup(tmp_path, monkeypatch, stage, cancel_again):
    from agent_platform.codex_app_server import inspect_executable

    executable = tmp_path / 'slow-version'
    executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n')
    executable.chmod(0o700)
    spawned, reading, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_spawn, release_cleanup = asyncio.Event(), asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec
    process = None
    original_communicate = None
    async def spawn(*args, **kwargs):
        nonlocal process, original_communicate
        process = await original_spawn(*args, **kwargs)
        original_communicate = process.communicate
        calls = 0
        async def communicate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == (1 if stage == 'spawn' else 2):
                cleaning.set()
                await release_cleanup.wait()
            else:
                reading.set()
            return await original_communicate(*args, **kwargs)
        process.communicate = communicate
        spawned.set()
        if stage == 'spawn': await release_spawn.wait()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    probe = asyncio.create_task(inspect_executable(str(executable)))
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        if stage == 'reading': await asyncio.wait_for(reading.wait(), 5)
        probe.cancel()
        await asyncio.sleep(0)
        assert not probe.done(), 'Cancellation returned while the owned process was still live'
        release_spawn.set()
        await asyncio.wait_for(cleaning.wait(), 5)
        if cancel_again:
            probe.cancel()
            await asyncio.sleep(0)
            assert not probe.done(), 'Repeated cancellation bypassed process/pipe cleanup'
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError): await probe
        assert process.returncode is not None
        assert process.stdout.at_eof() and process.stderr.at_eof()
    finally:
        release_spawn.set(); release_cleanup.set()
        if not probe.done(): probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)
        if process:
            if process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
            await original_communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'empty', 'exit_error', 'timeout', 'spawn_error'])
async def test_version_probe_preserves_its_result_or_error_after_process_cleanup(tmp_path, monkeypatch, outcome):
    from agent_platform.codex_app_server import inspect_executable

    executable = tmp_path / 'version'
    body = {'success': "print('codex-cli 0.162.0')", 'empty': 'pass',
            'exit_error': "print('unusable');raise SystemExit(2)", 'timeout': 'import time;time.sleep(60)'}
    executable.write_text(f'#!{sys.executable}\n'+body[outcome] if outcome != 'spawn_error' else 'invalid executable format')
    executable.chmod(0o700)
    original_spawn, original_wait = asyncio.create_subprocess_exec, asyncio.wait_for
    processes = []
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process
    async def wait(awaitable, timeout):
        return await original_wait(awaitable, .05 if outcome == 'timeout' and timeout == 10 else timeout)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(asyncio, 'wait_for', wait)
    if outcome == 'success':
        assert await inspect_executable(str(executable)) == {'path': str(executable), 'version': 'codex-cli 0.162.0'}
    elif outcome == 'spawn_error':
        with pytest.raises(OSError): await inspect_executable(str(executable))
    else:
        with pytest.raises(ValueError, match='读取 Agent 版本超时' if outcome == 'timeout' else '无法读取 Agent 版本'):
            await inspect_executable(str(executable))
    for process in processes:
        assert process.returncode is not None
        assert process.stdout.at_eof() and process.stderr.at_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['spawn', 'handshake'])
@pytest.mark.parametrize('cancel_again', [False, True])
async def test_cancelled_connection_keeps_process_ownership_until_cleanup_and_can_retry(tmp_path, monkeypatch, stage, cancel_again):
    executable = tmp_path / 'connecting-agent'
    executable.write_text(f'#!{sys.executable}\nROOT={str(tmp_path)!r}\n' + '''
import json,sys,time
from pathlib import Path
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method') == 'skills/list':
        print(json.dumps({'id':message['id'],'result':{'data':[]}}),flush=True)
        continue
    if message.get('method')=='initialize':
        while not (Path(ROOT)/'allow-connect').exists():time.sleep(.01)
        print(json.dumps({'id':message['id'],'result':{}}),flush=True)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime', auth_file=tmp_path/'unused-auth')
    spawned, handshake, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_spawn, release_cleanup = asyncio.Event(), asyncio.Event()
    original_spawn, original_request = asyncio.create_subprocess_exec, client.request
    processes = []
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        if len(processes) == 1:
            original_wait = process.wait
            async def wait():
                cleaning.set()
                await release_cleanup.wait()
                return await original_wait()
            process.wait = wait
            spawned.set()
            if stage == 'spawn': await release_spawn.wait()
        return process
    async def request(method, params, **kwargs):
        if method == 'initialize': handshake.set()
        return await original_request(method, params, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    client.request = request
    async def connect():
        try:
            await client.connect()
        finally:
            await client.close()  # Same fallback used by the platform owner.
    connecting = asyncio.create_task(connect())
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        if stage == 'handshake': await asyncio.wait_for(handshake.wait(), 5)
        connecting.cancel()
        release_spawn.set()
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not connecting.done()
        if cancel_again:
            connecting.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not connecting.done(), 'A second cancellation bypassed startup cleanup'
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError): await connecting
        assert processes[0].returncode is not None
        assert processes[0].stdout.at_eof() and processes[0].stderr.at_eof()
        assert all(task is None or task.done() for task in (client.reader, client.stderr_reader))
        assert not client.pending
        (tmp_path/'allow-connect').touch()
        await client.connect()
        assert len(processes) == 2 and client.process is processes[1]
        assert client.process.returncode is None and not client.closing
    finally:
        release_spawn.set(); release_cleanup.set()
        if not connecting.done(): connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        await client.close()
        for process in processes:
            if process.returncode is None:
                try: process.kill()
                except ProcessLookupError: pass
            await process.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['rejected', 'timeout'])
async def test_failed_handshake_closes_process_preserves_error_and_allows_explicit_retry(tmp_path, failure):
    executable = tmp_path/'handshake-agent'
    executable.write_text(f'#!{sys.executable}\nROOT={str(tmp_path)!r}\nFAILURE={failure!r}\n' + '''
import json,sys
from pathlib import Path
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method') == 'skills/list':
        print(json.dumps({'id':message['id'],'result':{'data':[]}}),flush=True)
        continue
    if message.get('method')=='initialize':
        if (Path(ROOT)/'allow-connect').exists():
            print(json.dumps({'id':message['id'],'result':{}}),flush=True)
        elif FAILURE=='rejected':
            print(json.dumps({'id':message['id'],'error':{'message':'fixture handshake rejected'}}),flush=True)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime', auth_file=tmp_path/'unused-auth')
    original_request = client.request
    async def request(method, params, **kwargs):
        if method == 'initialize' and failure == 'timeout' and not (tmp_path/'allow-connect').exists():
            kwargs['timeout'] = .05
        return await original_request(method, params, **kwargs)
    client.request = request
    try:
        with pytest.raises(CodexError, match='fixture handshake rejected' if failure == 'rejected' else 'initialize 长时间无响应'):
            await client.connect()
        original_process = client.process
        assert original_process.returncode is not None and client.closing
        assert client.reader.done() and client.stderr_reader.done() and not client.pending
        (tmp_path/'allow-connect').touch()
        await client.connect()
        assert client.process is not original_process and client.process.returncode is None and not client.closing
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('resume', [False, True])
async def test_native_skills_disabled_without_hiding_project_skills(tmp_path, monkeypatch, resume):
    personal = tmp_path / 'personal'
    personal.mkdir()
    (personal / 'auth.json').write_text('local-login')
    config = personal / 'config.toml'
    config.write_text('[skills]\nmax_context_tokens = 8000\n')
    monkeypatch.setenv('CODEX_HOME', str(personal))
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
native = [str(home / 'skills/.system' / name / 'SKILL.md')
          for name in ['imagegen', 'new-bundled-skill']]
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        emit({'id': message['id'], 'result': {}})
    elif method == 'skills/list':
        assert message['params'] == {'cwds': [str(home.parent / 'empty-workspace')], 'forceReload': True}
        emit({'id': message['id'], 'result': {'data': [{'skills': [
            {'path': path, 'enabled': True} for path in native] + [{'path': native[0]}]}]}})
    elif method in ('thread/start', 'thread/resume'):
        params = message['params']
        assert params['config']['skills.config'] == [{'path': path, 'enabled': False} for path in native]
        if method == 'thread/start':
            assert params['dynamicTools'][0]['name'] == 'project_skills'
        else:
            assert params['threadId'] == 'project-thread' and params['excludeTurns']
        emit({'id': message['id'], 'result': {'thread': {'id': 'project-thread'}}})
    elif method == 'turn/start':
        emit({'id': message['id'], 'result': {'turn': {'id': 'turn'}}})
        emit({'id': 'skill-read', 'method': 'item/tool/call', 'params': {
            'threadId': 'project-thread', 'turnId': 'turn', 'tool': 'project_skills',
            'arguments': {'action': 'read', 'name': 'quality-analysis'}}})
    elif message.get('id') == 'skill-read':
        assert message['result']['success']
        value = json.loads(message['result']['contentItems'][0]['text'])
        assert value == {'body': 'Use the project quality workflow.'}
        emit({'method': 'turn/completed', 'params': {'threadId': 'project-thread', 'turn': {'id': 'turn', 'status': 'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    calls = []

    async def tool(name, arguments):
        calls.append((name, arguments))
        return {'body': 'Use the project quality workflow.'}

    async def event(method, params):
        pass

    try:
        await client.start([{'type': 'function', 'name': 'project_skills',
                            'description': 'Read project skills', 'inputSchema': {'type': 'object'}}],
                           'Project tools only', 'project-thread' if resume else None)
        assert (await client.turn('Read the quality skill', event, tool, timeout=10))['status'] == 'completed'
        assert calls == [('project_skills', {'action': 'read', 'name': 'quality-analysis'})]
        assert config.read_text() == '[skills]\nmax_context_tokens = 8000\n'
        assert not (tmp_path / 'runtime/codex-home/config.toml').exists()
    finally:
        await client.close()


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
    elif method == 'skills/list':
        emit({'id':message['id'],'result':{'data':[]}})
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
        assert 'fork_turns' in message['params']['developerInstructions']
        assert 'model_auto_compact_token_limit' not in message['params']['config']
        emit({'id':message['id'],'result':{'thread':{'id':'t1'}}})
    elif method == 'turn/start':
        emit({'id':message['id'],'result':{'turn':{'id':'u1'}}})
        emit({'id':'modeling','method':'item/tool/call','params':{
            'threadId':'t1','namespace':'lilies','tool':'project_modeling','arguments':{'action':'list'}}})
    elif message.get('id') == 'modeling':
        assert message['result']['success']
        assert json.loads(message['result']['contentItems'][0]['text']) == {'studies':[]}
        emit({'method':'turn/completed','params':{'threadId':'t1','turn':{'id':'u1','status':'completed'}}})
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
    elif method=='skills/list':
        emit({'id':message['id'],'result':{'data':[]}})
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
        emit({'method':'turn/started','params':{'threadId':'t1','turn':{'id':'u1'}}})
        emit({'id':'tool-request','method':'item/tool/call','params':{
            'threadId':'t1','turnId':'u1','callId':'c1','tool':'project_file',
            'arguments':{'action':'read','path':'../old-answer.txt'}}})
    elif message.get('id')=='tool-request':
        assert message['result']['success'] is False
        assert message['result']['contentItems'][0]['type']=='inputText'
        emit({'method':'item/completed','params':{'threadId':'t1','turnId':'u1','item':{'type':'agentMessage','text':'拒绝跨项目读取'}}})
        emit({'method':'turn/completed','params':{'threadId':'t1','turn':{'id':'u1','status':'completed'}}})
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
    client.accepting_tools = True
    client.turn_id = 'active-turn'
    monkeypatch.setattr(client, '_send', send)
    await client._server_request({'id': 'read-draft', 'method': 'item/tool/call',
        'params': {'threadId': 'project-thread', 'turnId': 'active-turn', 'tool': 'workflow_draft', 'arguments': {}}})
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
    emit({'method':'thread/tokenUsage/updated','params':{'threadId':'t1','turnId':'u1','tokenUsage':{'total':{
        'inputTokens':100,'outputTokens':20,'totalTokens':120}}}})
def complete(status):
    emit({'method':'turn/completed','params':{'threadId':'t1','turn':{'id':'u1','status':status}}})
for line in sys.stdin:
    message=json.loads(line); method=message.get('method')
    with log.open('a') as out:
        out.write(json.dumps(message)+'\\n')
    if method=='initialize':
        emit({'id':message['id'],'result':{}})
    elif method=='skills/list':
        emit({'id':message['id'],'result':{'data':[]}})
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
    elif method == 'skills/list':
        emit({'id':message['id'],'result':{'data':[]}})
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
        emit({'method': 'turn/completed', 'params': {'threadId': 'long-existing-thread', 'turn': {'id': 'resumed-turn', 'status': 'completed'}}})
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
    elif message.get('method') == 'skills/list':
        print(json.dumps({'id': message['id'], 'result': {'data': []}}), flush=True)
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
    emit({'method':'turn/completed','params':{'threadId':'t1','turn':{'id':'u1','status':'completed'}}})
for line in sys.stdin:
    message=json.loads(line)
    method=message.get('method')
    if method=='initialize':
        emit({'id':message['id'],'result':{}})
    elif method=='skills/list':
        emit({'id':message['id'],'result':{'data':[]}})
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
    elif method=='skills/list':result={'data':[]}
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


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_status', ['completed', 'failed', 'interrupted'])
async def test_children_execute_project_tools_without_finishing_or_steering_parent(tmp_path, terminal_status):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nterminal_status={terminal_status!r}\n' + '''
import json, sys
def emit(value): print(json.dumps(value), flush=True)
def note(method, thread, **params): emit({'method':method,'params':{'threadId':thread,**params}})
def call(identity, thread):
    emit({'id':identity,'method':'item/tool/call','params':{
        'threadId':thread,'turnId':'child-turn','tool':'project_file',
        'arguments':{'action':'write','path':identity+'.txt','content':identity},
        'parentThreadId':'root'}})
for line in sys.stdin:
    m=json.loads(line); method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'parent-turn'}}})
        note('turn/started','root',turn={'id':'parent-turn'})
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'method':'thread/started','params':{'thread':{'id':'grandchild','parentThreadId':'child'}}})
        note('turn/started','child',turn={'id':'child-turn'})
        call('child-result','child')
    elif m.get('id')=='child-result':
        assert m['result']['success'] is True
        call('nested-result','grandchild')
    elif m.get('id')=='nested-result':
        assert m['result']['success'] is True
        call('forbidden','unrelated')
    elif method=='thread/read':
        identity=m['params']['threadId'];assert not m['params']['includeTurns']
        if identity=='unrelated':
            emit({'id':m['id'],'result':{'thread':{'id':'unrelated','parentThreadId':None,
                 'forkedFromId':'root','sessionId':'same-session','cwd':'same-cwd'}}})
        else:
            emit({'id':m['id'],'result':{'thread':{'id':identity,'status':{'type':'idle'},
                'parentThreadId':{'child':'root','grandchild':'child'}[identity]}}})
    elif m.get('id')=='forbidden':
        assert m['result']['success'] is False
        note('item/completed','child',turnId='child-turn',item={'type':'agentMessage','text':'child-only-final'})
        note('thread/tokenUsage/updated','child',turnId='child-turn',tokenUsage={'total':{'totalTokens':9999999}})
        note('turn/completed','child',turn={'id':'child-turn','status':'failed'})
        note('turn/completed','unrelated',turn={'id':'parent-turn','status':'completed'})
        note('turn/completed','root',turn={'id':'previous-turn','status':'completed'})
        call('parent-still-working','root')
    elif method=='turn/steer':
        assert m['params']['threadId']=='root' and m['params']['expectedTurnId']=='parent-turn'
        emit({'id':m['id'],'result':{'turnId':'parent-turn'}})
    elif method=='turn/interrupt':
        assert m['params']=={'threadId':'root','turnId':'parent-turn'}
        emit({'id':m['id'],'result':{}})
    elif m.get('id')=='parent-still-working':
        assert m['result']['success'] is True
        note('item/completed','root',turnId='parent-turn',item={'type':'agentMessage','text':'parent-final'})
        note('turn/completed','root',turn={'id':'parent-turn','status':terminal_status})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    reached, release = asyncio.Event(), asyncio.Event()
    calls, events = [], []

    async def event(method, params):
        events.append((method, params))

    async def tool(name, arguments):
        calls.append(arguments['path'])
        if arguments['path'] == 'parent-still-working.txt':
            reached.set()
            await release.wait()
        (tmp_path / arguments['path']).write_text(arguments['content'])
        return {'written': arguments['path']}

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('delegate', event, tool, timeout=10))
        await asyncio.wait_for(reached.wait(), 5)
        assert not task.done() and not client.finished.done()
        assert client.turn_id == 'parent-turn'
        assert (tmp_path / 'child-result.txt').read_text() == 'child-result'
        assert (tmp_path / 'nested-result.txt').read_text() == 'nested-result'
        assert not (tmp_path / 'forbidden.txt').exists()
        await client.steer('keep working')
        if terminal_status == 'interrupted':
            await client.interrupt()
        release.set()
        assert (await asyncio.wait_for(task, 5))['status'] == terminal_status
        messages = [p['item']['text'] for m, p in events if m == 'item/completed']
        assert messages == ['parent-final']
        assert [p['usageDelta'] for m, p in events if m == 'thread/tokenUsage/updated'] == [9999999]
        assert [p['turn']['id'] for m, p in events if m == 'turn/completed'] == ['parent-turn']
        assert calls == ['child-result.txt', 'nested-result.txt', 'parent-still-working.txt']
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_resumed_descendants_resolve_server_ancestry_without_start_notifications(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/resume': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'current'}}})
        emit({'id':'nested-call','method':'item/tool/call','params':{
            'threadId':'nested','tool':'project_file','arguments':{'action':'read','path':'shared.txt'}}})
    elif method=='thread/read':
        identity=m['params']['threadId'];assert not m['params']['includeTurns']
        parent={'nested':'child','child':'root'}[identity]
        emit({'id':m['id'],'result':{'thread':{'id':identity,'status':{'type':'idle'},'source':{
            'subAgent':{'thread_spawn':{'parent_thread_id':parent,'depth':2}}}}}})
    elif m.get('id')=='nested-call':
        assert m['result']['success']
        assert json.loads(m['result']['contentItems'][0]['text'])=={'text':'project data'}
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'current','status':'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    (tmp_path / 'shared.txt').write_text('project data')
    async def event(method, params): pass
    async def tool(name, arguments): return {'text': (tmp_path / arguments['path']).read_text()}
    try:
        await client.start([], 'project only', 'root')
        assert (await client.turn('continue delegated work', event, tool, timeout=10))['status'] == 'completed'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_parent_completion_before_start_response_is_not_lost(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        for identity in ['old','current']:
            emit({'method':'turn/started','params':{'threadId':'root','turn':{'id':identity}}})
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':identity,'status':'completed'}}})
        emit({'id':m['id'],'result':{'turn':{'id':'current'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    events = []
    async def event(method, params): events.append((method, params))
    try:
        await client.start([], 'project only')
        result = await client.turn('work', event, None, timeout=2)
        assert result['id'] == 'current'
        assert [p['turn']['id'] for _, p in events] == ['current', 'current']
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['resolved', 'completed', 'failed', 'interrupted'])
async def test_cancelled_child_tool_cannot_write_while_sibling_and_parent_continue(tmp_path, ending):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
def note(method, **params): emit({'method':method,'params':params})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'parent-turn'}}})
        for thread,identity in [('child',41),('sibling','sibling-call')]:
            note('thread/started',thread={'id':thread,'parentThreadId':'root'})
            emit({'id':identity,'method':'item/tool/call','params':{
                'threadId':thread,'turnId':thread+'-turn','tool':'project_file',
                'arguments':{'action':'write','path':thread+'.txt','content':thread}}})
    elif method=='turn/steer':
        if m['params']['input'][0]['text']=='ignore unrelated cleanup':
            note('serverRequest/resolved',threadId='sibling',requestId=41)
            note('turn/completed',threadId='child',turn={'id':'old-child-turn','status':'interrupted'})
        elif ending=='resolved':
            note('serverRequest/resolved',threadId='child',requestId=41)
        else:
            note('turn/completed',threadId='child',turn={'id':'child-turn','status':ending})
        emit({'id':m['id'],'result':{'turnId':'parent-turn'}})
    elif m.get('id')=='sibling-call':
        assert m['result']['success']
        note('turn/completed',threadId='sibling',turn={'id':'sibling-turn','status':'completed'})
        note('turn/completed',threadId='root',turn={'id':'parent-turn','status':'completed'})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':m['params']['threadId'],
            'parentThreadId':'root','status':{'type':'idle'}}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    started = {name: asyncio.Event() for name in ['child', 'sibling']}
    release = asyncio.Event()
    settled = {name: asyncio.Event() for name in started}
    cancelled, writes = [], []

    async def event(method, params): pass

    async def tool(name, arguments):
        identity = arguments['content']
        started[identity].set()
        try:
            await release.wait()
            (tmp_path / arguments['path']).write_text(identity)
            writes.append(identity)
            return {'written': arguments['path']}
        except asyncio.CancelledError:
            cancelled.append(identity)
            raise
        finally:
            settled[identity].set()

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('delegate', event, tool, timeout=10))
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started.values())), 5)
        await client.steer('ignore unrelated cleanup')
        assert not any(e.is_set() for e in settled.values())
        await client.steer('cancel child')
        assert not task.done() and client.turn_id == 'parent-turn'
        release.set()
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in settled.values())), 5)
        assert (await asyncio.wait_for(task, 5))['status'] == 'completed'
        assert not (tmp_path / 'child.txt').exists()
        assert (tmp_path / 'sibling.txt').read_text() == 'sibling'
        assert cancelled == ['child'] and writes == ['sibling']
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['failed', 'interrupted'])
async def test_parent_waits_for_terminated_tool_cleanup_before_finishing(tmp_path, ending):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'parent-turn'}}})
        emit({'id':'pending-write','method':'item/tool/call','params':{
            'threadId':'root','turnId':'parent-turn','tool':'project_file','arguments':{}}})
    elif method=='turn/steer':
        emit({'method':'turn/completed','params':{'threadId':'root',
            'turn':{'id':'parent-turn','status':ending}}})
        emit({'id':m['id'],'result':{'turnId':'parent-turn'}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    completion_observed_cleanup = []

    async def event(method, params):
        if method == 'turn/completed':
            completion_observed_cleanup.append(cleaned.is_set())

    async def tool(name, arguments):
        started.set()
        try:
            await release.wait()
            (tmp_path / 'late-write.txt').write_text('must not happen')
        finally:
            await asyncio.sleep(.01)  # Actual asynchronous tool cleanup.
            cleaned.set()

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('write', event, tool, timeout=10))
        await asyncio.wait_for(started.wait(), 5)
        await client.steer('terminate turn')
        assert (await asyncio.wait_for(task, 5))['status'] == ending
        release.set()
        await asyncio.wait_for(cleaned.wait(), 5)
        assert completion_observed_cleanup == [True]
        assert not (tmp_path / 'late-write.txt').exists()
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['failed', 'interrupted'])
async def test_parent_abort_stops_descendants_before_waiting_for_slow_root_cleanup(tmp_path, ending):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        for identity,parent in [('child','root'),('grandchild','child')]:
            emit({'method':'thread/started','params':{'thread':{'id':identity,'parentThreadId':parent}}})
        for identity in ['root','child','grandchild']:
            emit({'id':identity+'-write','method':'item/tool/call','params':{
                'threadId':identity,'turnId':identity+'-turn','tool':'project_file',
                'arguments':{'identity':identity}}})
    elif method=='turn/steer':
        emit({'id':m['id'],'result':{'turnId':'root-turn'}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{
            'id':'root-turn','status':ending,'error':{'message':'original parent failure'} if ending=='failed' else None}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime')
    started = {name:asyncio.Event() for name in ('root','child','grandchild')}
    cleaned = {name:asyncio.Event() for name in started}
    root_cleanup, release_cleanup, release_work = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []
    (tmp_path/'saved-result.txt').write_text('previous result')
    async def event(method, params):
        if method == 'turn/completed':
            observed.append((params['turn'], all(flag.is_set() for flag in cleaned.values())))
    async def tool(name, arguments):
        identity = arguments['identity']
        started[identity].set()
        try:
            await release_work.wait()
            (tmp_path/(identity+'-late.txt')).write_text('must not write after abort')
        finally:
            if identity == 'root':
                root_cleanup.set()
                await release_cleanup.wait()
            await asyncio.sleep(.01)
            cleaned[identity].set()
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run with descendants', event, tool, timeout=10))
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())), 5)
        await client.steer('abort root')
        await asyncio.wait_for(root_cleanup.wait(), 5)
        assert not task.done() and not observed
        # A slow root cleanup must not leave either descendant running. They
        # receive no separate terminal notification in this replay.
        release_work.set()
        await asyncio.wait_for(asyncio.gather(cleaned['child'].wait(), cleaned['grandchild'].wait()), 5)
        assert not (tmp_path/'child-late.txt').exists()
        assert not (tmp_path/'grandchild-late.txt').exists()
        assert not task.done() and not observed
        release_cleanup.set()
        result = await asyncio.wait_for(task, 5)
        assert result['status'] == ending
        if ending == 'failed': assert result['error']['message'] == 'original parent failure'
        assert observed == [(result, True)]
        assert client.process.returncode is not None and not client.requests
        assert (tmp_path/'saved-result.txt').read_text() == 'previous result'
        assert not (tmp_path/'root-late.txt').exists()
    finally:
        release_work.set(); release_cleanup.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending,stop_phase,cleanup_fails', [
    ('failed', 'before_result', False), ('failed', 'before_result', True),
    ('failed', 'completion_ready', False), ('failed', 'completion_ready', True),
    ('completed', 'before_result', False), ('interrupted', 'before_result', False),
])
async def test_stop_after_native_failure_preserves_failure_and_waits_for_cleanup(
        tmp_path, ending, stop_phase, cleanup_fails):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        if m['params']['input'][0]['text']=='recover':
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
            continue
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'id':'child-work','method':'item/tool/call','params':{
            'threadId':'child','turnId':'child-turn','tool':'project_code','arguments':{}}})
    elif method=='turn/steer':
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{
            'id':'root-turn','status':ending,'error':{'message':'original native failure'} if ending=='failed' else None}}})
        emit({'id':m['id'],'result':{'turnId':'root-turn'}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime')
    started, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_result = asyncio.Event()
    cleaned, cleanup_interruptions, observed = [], [], []
    original_request = client.request

    async def delayed_start_return(method, params, timeout=30):
        result = await original_request(method, params, timeout)
        if method == 'turn/start' and stop_phase == 'before_result':
            # The reader has received the result, but the turn owner has not
            # resumed yet. Make that scheduling window deterministic.
            await release_result.wait()
        return result
    client.request = delayed_start_return

    async def event(method, params):
        if method == 'turn/completed':
            observed.append((params['turn'], bool(cleaned)))

    async def tool(name, arguments):
        (tmp_path/'saved.txt').write_text('previous result')
        started.set()
        try:
            await asyncio.Event().wait()
            (tmp_path/'late.txt').write_text('must not happen')
        finally:
            cleaning.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interruptions.append(True)
                raise
            cleaned.append(True)
            if cleanup_fails:
                raise OSError('child cleanup failed')

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run', event, tool, timeout=10))
        await asyncio.wait_for(started.wait(), 5)
        await client.steer('finish root')
        assert client.finished.done() and client.finished.result()['status'] == ending
        if stop_phase == 'completion_ready':
            await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        await asyncio.sleep(.03)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done() and not cleaned and not cleanup_interruptions and not observed
        release_cleanup.set()
        if ending == 'failed':
            try:
                result = await asyncio.wait_for(task, 5)
            except CodexError as cause:
                error = str(cause)
            else:
                assert result['status'] == 'failed' and observed == [(result, True)]
                error = result['error']['message']
            assert 'original native failure' in error
            assert ('child cleanup failed' in error) == cleanup_fails
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        assert cleaned and not cleanup_interruptions
        assert not client.requests and not client.pending and client.process.returncode is not None
        assert (tmp_path/'saved.txt').read_text() == 'previous result' and not (tmp_path/'late.txt').exists()
        release_result.set()
        await client.start([], 'project only')
        assert (await client.turn('recover', event, tool, timeout=5))['status'] == 'completed'
    finally:
        release_cleanup.set(); release_result.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('child_notice', ['resolved', 'completed'])
@pytest.mark.parametrize('ending', ['completed', 'failed', 'interrupted'])
async def test_slow_child_cleanup_does_not_block_later_protocol_messages(tmp_path, child_notice, ending):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nchild_notice={child_notice!r}\nending={ending!r}\n' + '''
import json,sys
active={'child':True,'sibling':True}
def emit(value): print(json.dumps(value),flush=True)
def note(method,**params): emit({'method':method,'params':params})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        for identity in active:
            note('thread/started',thread={'id':identity,'parentThreadId':'root'})
            emit({'id':identity+'-call','method':'item/tool/call','params':{
                'threadId':identity,'turnId':identity+'-turn','tool':'project_file','arguments':{'identity':identity}}})
    elif method=='turn/steer':
        active['child']=False
        if child_notice=='resolved':note('serverRequest/resolved',threadId='child',requestId='child-call')
        else:note('turn/completed',threadId='child',turn={'id':'child-turn','status':'interrupted'})
        note('turn/completed',threadId='root',turn={'id':'root-turn','status':ending,
            'error':{'message':'original parent error'} if ending=='failed' else None})
        emit({'id':m['id'],'result':{'turnId':'root-turn'}})
    elif m.get('id')=='sibling-call':
        active['sibling']=False
        note('turn/completed',threadId='sibling',turn={'id':'sibling-turn','status':'completed'})
    elif method=='thread/read':
        identity=m['params']['threadId']
        emit({'id':m['id'],'result':{'thread':{'id':identity,'parentThreadId':'root',
            'status':{'type':'active' if active[identity] else 'idle'}}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime')
    started = {identity:asyncio.Event() for identity in ('child','sibling')}
    cleaned = {identity:asyncio.Event() for identity in started}
    slow_cleanup, release_cleanup, release_sibling = [asyncio.Event() for _ in range(3)]
    observed, interrupted_cleanup = [], []
    async def event(method, params):
        if method == 'turn/completed':
            observed.append((params['turn'], all(flag.is_set() for flag in cleaned.values())))
    async def tool(name, args):
        identity = args['identity']
        started[identity].set()
        try:
            if identity == 'child': await asyncio.Event().wait()
            else:
                await release_sibling.wait()
                (tmp_path/'sibling.txt').write_text('sibling result')
            return {'ok':True}
        finally:
            if identity == 'child':
                slow_cleanup.set()
                try: await release_cleanup.wait()
                except asyncio.CancelledError:
                    interrupted_cleanup.append(identity)
                    raise
            cleaned[identity].set()
    task = steering = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run children', event, tool, timeout=10))
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())), 5)
        steering = asyncio.create_task(client.steer('cancel child then finish root'))
        await asyncio.wait_for(slow_cleanup.wait(), 5)
        release_sibling.set()
        await asyncio.wait_for(cleaned['sibling'].wait(), 5)
        assert (tmp_path/'sibling.txt').exists() is (ending == 'completed')
        assert await asyncio.wait_for(asyncio.shield(steering), .5) is True
        assert not task.done() and not observed and not cleaned['child'].is_set()
        release_cleanup.set()
        result = await asyncio.wait_for(task, 5)
        assert result['status'] == ending
        if ending == 'failed': assert result['error']['message'] == 'original parent error'
        assert observed == [(result, True)] and not interrupted_cleanup
        if ending == 'completed': assert (tmp_path/'sibling.txt').read_text() == 'sibling result'
    finally:
        release_cleanup.set(); release_sibling.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if steering: await asyncio.gather(steering, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('root_completed', [False, True])
async def test_child_cleanup_failure_stops_owned_sibling_but_not_other_conversation(tmp_path, root_completed):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nroot_completed={root_completed!r}\n' + '''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
def note(method,**params):emit({'method':method,'params':params})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        identities=['bad','sibling'] if m['params']['input'][0]['text']=='failing' else ['independent']
        for identity in identities:
            note('thread/started',thread={'id':identity,'parentThreadId':'root'})
            emit({'id':identity,'method':'item/tool/call','params':{'threadId':identity,
                'turnId':identity+'-turn','tool':'project_file','arguments':{'identity':identity}}})
    elif method=='fixture/fail':
        emit({'id':m['id'],'result':{}})
        if root_completed:note('turn/completed',threadId='root',turn={'id':'root-turn','status':'completed'})
        note('turn/completed',threadId='bad',turn={'id':'bad-turn','status':'interrupted'})
    elif method=='thread/read':
        identity=m['params']['threadId']
        emit({'id':m['id'],'result':{'thread':{'id':identity,'parentThreadId':'root','status':{'type':'idle'}}}})
    elif m.get('id')=='independent':
        note('turn/completed',threadId='root',turn={'id':'root-turn','status':'completed'})
''')
    executable.chmod(0o700)
    clients = [CodexAppServer(str(executable),tmp_path/identity) for identity in ('failing','independent')]
    started = {identity:asyncio.Event() for identity in ('bad','sibling','independent')}
    cleaned = {identity:asyncio.Event() for identity in started}
    release = {identity:asyncio.Event() for identity in started}
    notices = []
    async def event(method, params):
        if method == 'turn/completed':notices.append(params['turn']['status'])
    async def tool(name, args):
        identity = args['identity']
        started[identity].set()
        try:
            await release[identity].wait()
            (tmp_path/(identity+'.txt')).write_text('result')
            return {'ok':True}
        finally:
            cleaned[identity].set()
            if identity == 'bad':raise OSError('owned cleanup failed')
    tasks = []
    try:
        for client, message in zip(clients, ('failing','independent')):
            await client.start([], 'project only')
            tasks.append(asyncio.create_task(client.turn(message,event,tool,timeout=10)))
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())),5)
        await clients[0].request('fixture/fail',{})
        await asyncio.wait_for(cleaned['sibling'].wait(),1)
        with pytest.raises(CodexError,match='owned cleanup failed'):
            await asyncio.wait_for(asyncio.shield(tasks[0]),5)
        assert not tasks[1].done() and not cleaned['independent'].is_set()
        assert not notices and not (tmp_path/'sibling.txt').exists()
        release['sibling'].set();release['independent'].set()
        assert (await asyncio.wait_for(tasks[1],5))['status']=='completed'
        assert (tmp_path/'independent.txt').read_text()=='result'
        assert not (tmp_path/'sibling.txt').exists() and notices==['completed']
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for client in clients:await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cleanup_failure', 'disconnect', 'start_rejected', 'cancel', 'ordinary_error',
                                  'write_error', 'write_reset', 'cancel_while_writing'])
async def test_tools_before_start_ack_settle_once_and_can_explicitly_reconnect(tmp_path, ending):
    import gc
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
def note(method,**params):emit({'method':method,'params':params})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method in {'thread/start','thread/resume'}:emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        if m['params']['input'][0]['text']=='recover':
            emit({'id':m['id'],'result':{'turn':{'id':'recovery-turn'}}})
            note('turn/completed',threadId='root',turn={'id':'recovery-turn','status':'completed'})
        else:
            pending_start=m['id']
            note('turn/started',threadId='root',turn={'id':'root-turn'})
            for identity in ('child','sibling'):
                note('thread/started',thread={'id':identity,'parentThreadId':'root'})
                emit({'id':identity,'method':'item/tool/call','params':{'threadId':identity,
                    'turnId':identity+'-turn','tool':'project_file','arguments':{'identity':identity}}})
    elif method=='fixture/fail':
        emit({'id':m['id'],'result':{}})
        if ending in {'disconnect','write_error','write_reset','cancel_while_writing'}:sys.exit(0)
        elif ending=='start_rejected':emit({'id':pending_start,'error':{'message':'native start rejected','code':-32000}})
        else:note('turn/completed',threadId='child',turn={'id':'child-turn','status':'interrupted'})
    elif m.get('id')=='child' and ending=='ordinary_error':
        assert not m['result']['success']
        emit({'id':pending_start,'result':{'turn':{'id':'root-turn'}}})
        note('turn/completed',threadId='child',turn={'id':'child-turn','status':'completed'})
    elif m.get('id')=='sibling':
        assert m['result']['success']
        note('turn/completed',threadId='sibling',turn={'id':'sibling-turn','status':'completed'})
        note('turn/completed',threadId='root',turn={'id':'root-turn','status':'completed'})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':m['params']['threadId'],'parentThreadId':'root','status':{'type':'idle'}}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable),tmp_path/'runtime')
    started = {identity:asyncio.Event() for identity in ('child','sibling')}
    cleaned = {identity:asyncio.Event() for identity in started}
    release = {identity:asyncio.Event() for identity in started}
    release_write = asyncio.Event()
    original_send = client._send
    async def send(message):
        await original_send(message)
        if (ending in {'write_error','write_reset','cancel_while_writing'} and message.get('method')=='turn/start'
                and message['params']['input'][0]['text']!='recover'):
            # Hold the write coroutine after delivery, as a delayed drain can
            # overlap reader failure. The real fixture process still disconnects.
            await release_write.wait()
            if ending=='write_reset':raise ConnectionResetError('Connection lost')
            raise BrokenPipeError()
    client._send = send
    notices, background_errors = [], []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context:background_errors.append(
        (context.get('message'),str(context.get('exception')))))
    async def event(method, params):
        if method=='turn/completed':notices.append(params['turn']['status'])
    async def tool(name, args):
        identity = args['identity']
        (tmp_path/(identity+'-saved.txt')).write_text('previous result')
        started[identity].set()
        try:
            await release[identity].wait()
            if identity=='child':raise ValueError('invalid business input')
            (tmp_path/'sibling-finished.txt').write_text('sibling result')
            return {'ok':True}
        finally:
            cleaned[identity].set()
            if identity=='child' and ending=='cleanup_failure':raise OSError('owned cleanup failed')
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run',event,tool,timeout=10))
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())),5)
        assert not task.done() and not notices
        if ending=='ordinary_error':
            release['child'].set()
            await asyncio.wait_for(cleaned['child'].wait(),5)
            assert not task.done() and not cleaned['sibling'].is_set()
            release['sibling'].set()
            assert (await asyncio.wait_for(task,5))['status']=='completed'
            assert (tmp_path/'sibling-finished.txt').read_text()=='sibling result'
            assert notices==['completed']
        else:
            if ending=='cancel':task.cancel()
            else:await client.request('fixture/fail',{})
            if ending in {'write_error','write_reset','cancel_while_writing'}:
                await asyncio.wait_for(asyncio.shield(client.reader),2)
                if ending=='cancel_while_writing':task.cancel()
                else:release_write.set()
            # A stop after an observed disconnect preserves that earlier error.
            with pytest.raises(asyncio.CancelledError if ending == 'cancel' else CodexError) as caught:
                await asyncio.wait_for(asyncio.shield(task),2)
            expected={'cleanup_failure':'owned cleanup failed','disconnect':'断开','start_rejected':'native start rejected',
                      'write_error':'断开','write_reset':'断开','cancel_while_writing':'断开'}
            if ending in expected:assert expected[ending] in str(caught.value)
            del caught
            task = None
            assert not notices and not (tmp_path/'sibling-finished.txt').exists()
            assert client.process.returncode is not None
        assert all(flag.is_set() for flag in cleaned.values()) and not client.requests and not client.pending
        assert all((tmp_path/(identity+'-saved.txt')).read_text()=='previous result' for identity in started)
        await client.close()
        await client.start([], 'project only',client.thread_id)
        assert (await client.turn('recover',event,tool,timeout=5))['status']=='completed'
        await asyncio.sleep(0)
        gc.collect()
        assert not background_errors
    finally:
        if task and not task.done():task.cancel()
        if task:await asyncio.gather(task,return_exceptions=True)
        await client.close()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['status', 'ancestry'])
@pytest.mark.parametrize('failure', ['cancel_cleanup', 'normal_cleanup', 'ordinary_error'])
async def test_cleanup_failure_aborts_pending_metadata_but_business_error_does_not(tmp_path, query, failure):
    from agent_platform.sandbox import SandboxCleanupError
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nquery={query!r}\n' + '''
import json,sys
pending=[];released=False
def emit(value):print(json.dumps(value),flush=True)
def note(method,**params):emit({'method':method,'params':params})
def metadata(m):
    emit({'id':m['id'],'result':{'thread':{'id':m['params']['threadId'],
        'parentThreadId':'root','status':{'type':'idle'}}}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method in {'thread/start','thread/resume'}:emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        recovery=m['params']['input'][0]['text']=='recover'
        turn='recovery-turn' if recovery else 'root-turn'
        emit({'id':m['id'],'result':{'turn':{'id':turn}}})
        if recovery:note('turn/completed',threadId='root',turn={'id':turn,'status':'completed'})
        else:
            for identity in ('child','sibling'):
                note('thread/started',thread={'id':identity,'parentThreadId':'root'})
                emit({'id':identity,'method':'item/tool/call','params':{'threadId':identity,
                    'turnId':identity+'-turn','tool':'project_file','arguments':{'identity':identity}}})
    elif method=='fixture/end':
        emit({'id':m['id'],'result':{}})
        if query=='ancestry':note('turn/started',threadId='unknown',turn={'id':'unknown-turn'})
        note('turn/completed',threadId='root',turn={'id':'root-turn','status':'completed'})
    elif method=='thread/read':
        if not released and m['params']['threadId']==('sibling' if query=='status' else 'unknown'):
            pending.append(m);note('fixture/read_pending')
        else:metadata(m)
    elif method=='fixture/fail':
        emit({'id':m['id'],'result':{}})
        note('turn/completed',threadId='child',turn={'id':'child-turn','status':'interrupted'})
    elif method=='fixture/release':
        released=True
        for request in pending:metadata(request)
        emit({'id':m['id'],'result':{}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable),tmp_path/'runtime')
    started = {identity:asyncio.Event() for identity in ('child','sibling')}
    cleaned = {identity:asyncio.Event() for identity in started}
    release = {identity:asyncio.Event() for identity in started}
    queried = asyncio.Event()
    notices = []
    async def event(method, params):
        if method=='fixture/read_pending':queried.set()
        if method=='turn/completed':notices.append(params['turn']['status'])
    async def tool(name, args):
        identity = args['identity']
        started[identity].set()
        try:
            await release[identity].wait()
            if identity=='child' and failure=='ordinary_error':raise ValueError('invalid business input')
            if identity=='sibling':(tmp_path/'sibling.txt').write_text('sibling result')
            return {'ok':True}
        finally:
            cleaned[identity].set()
            if identity=='child' and failure!='ordinary_error':
                if failure=='normal_cleanup':raise SandboxCleanupError('owned cleanup failed')
                raise OSError('owned cleanup failed')
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run',event,tool,timeout=10))
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())),5)
        await client.request('fixture/end',{})
        await asyncio.wait_for(queried.wait(),5)
        if failure=='cancel_cleanup':await client.request('fixture/fail',{})
        else:release['child'].set()
        await asyncio.wait_for(cleaned['child'].wait(),5)
        if failure=='ordinary_error':
            await asyncio.wait({task},timeout=.05)
            assert not task.done() and not cleaned['sibling'].is_set() and not notices
            await client.request('fixture/release',{})
            release['sibling'].set()
            assert (await asyncio.wait_for(task,5))['status']=='completed'
            assert (tmp_path/'sibling.txt').read_text()=='sibling result'
            assert notices==['completed']
        else:
            with pytest.raises(CodexError,match='owned cleanup failed'):
                await asyncio.wait_for(asyncio.shield(task),1)
            assert cleaned['sibling'].is_set() and not (tmp_path/'sibling.txt').exists()
            assert not notices and not client.pending and not client.requests
            assert client.process.returncode is not None
            await client.start([], 'project only',client.thread_id)
            assert (await client.turn('recover',event,tool,timeout=5))['status']=='completed'
    finally:
        if task and not task.done():task.cancel()
        if task:await asyncio.gather(task,return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['completed', 'failed', 'interrupted', 'cancel', 'disconnect', 'active', 'ordinary_error'])
async def test_cancelled_tool_cleanup_error_fails_turn_and_allows_explicit_reconnect(tmp_path, ending):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nending={ending!r}\n' + '''
import json,sys
root_turn=child_turn=''
def emit(value): print(json.dumps(value),flush=True)
def call(identity):
    emit({'id':identity,'method':'item/tool/call','params':{
        'threadId':'child','turnId':child_turn,'tool':'project_file','arguments':{'identity':identity}}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method in {'thread/start','thread/resume'}:emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        recovering=m['params']['input'][0]['text']=='recover'
        root_turn='root-recovery' if recovering else 'root-turn'
        child_turn='child-recovery' if recovering else 'child-turn'
        emit({'id':m['id'],'result':{'turn':{'id':root_turn}}})
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        call('recovered' if recovering else 'pending')
    elif method=='turn/steer':
        emit({'id':m['id'],'result':{'turnId':'root-turn'}})
        if ending=='disconnect':sys.exit(0)
        if ending!='active':
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':root_turn,'status':ending,
                'error':{'message':'original root error'} if ending=='failed' else None}}})
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':child_turn,'status':'interrupted'}}})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root','status':{'type':'idle'}}}})
    elif m.get('id')=='pending' and ending=='ordinary_error':
        assert not m['result']['success'];call('recovered')
    elif m.get('id')=='recovered':
        assert m['result']['success']
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':root_turn,'status':'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable),tmp_path/'runtime')
    started, cleaned = asyncio.Event(), asyncio.Event()
    notices = []
    (tmp_path/'saved.txt').write_text('previous result')
    async def event(method, params):
        if method == 'turn/completed': notices.append(params['turn']['status'])
    async def tool(name, args):
        if args['identity'] == 'recovered':
            (tmp_path/'recovered.txt').write_text('recovered result')
            return {'ok':True}
        started.set()
        if ending == 'ordinary_error': raise ValueError('invalid business input')
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
            raise OSError('fixture removal failed')
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('start',event,tool,timeout=10))
        await asyncio.wait_for(started.wait(),5)
        if ending == 'ordinary_error':
            assert (await asyncio.wait_for(task,5))['status'] == 'completed'
            assert notices == ['completed'] and (tmp_path/'recovered.txt').is_file()
            return
        if ending == 'cancel': task.cancel()
        else: await client.steer('terminate')
        with pytest.raises(CodexError, match='fixture removal failed') as caught:
            await asyncio.wait_for(task,5)
        if ending == 'failed': assert 'original root error' in str(caught.value)
        assert cleaned.is_set() and not notices
        assert client.process.returncode is not None and not client.requests
        assert (tmp_path/'saved.txt').read_text() == 'previous result'
        await client.start([], 'project only', client.thread_id)
        assert (await client.turn('recover',event,tool,timeout=5))['status'] == 'completed'
        assert (tmp_path/'recovered.txt').read_text() == 'recovered result'
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['disconnect', 'invalid_json'])
@pytest.mark.parametrize('parent_finished', [False, True])
async def test_transport_failure_stops_child_tool_before_returning_error(tmp_path, fault, parent_finished):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nfault={fault!r}\nparent_finished={parent_finished!r}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'id':'pending-write','method':'item/tool/call','params':{
            'threadId':'child','turnId':'child-turn','tool':'project_file','arguments':{}}})
        if parent_finished:
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root',
            'status':{'type':'active','activeFlags':[]}}}})
    elif method=='fixture/fault':
        emit({'id':m['id'],'result':{}})
        if fault=='disconnect': sys.exit(0)
        print('invalid json',flush=True)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    (tmp_path / 'saved.txt').write_text('previous result')

    async def event(method, params): pass

    async def tool(name, arguments):
        started.set()
        try:
            await release.wait()
            (tmp_path / 'late.txt').write_text('must not happen')
        finally:
            await asyncio.sleep(.01)
            cleaned.set()

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('delegate', event, tool, timeout=10))
        await asyncio.wait_for(started.wait(), 5)
        await client.request('fixture/fault', {})
        with pytest.raises(CodexError, match='断开|协议连接失败'):
            await asyncio.wait_for(task, 5)
        cleaned_before_error = cleaned.is_set()
        release.set()
        await asyncio.wait_for(cleaned.wait(), 5)
        assert not (tmp_path / 'late.txt').exists()
        assert cleaned_before_error
        assert (tmp_path / 'saved.txt').read_text() == 'previous result'
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('root_completed', [False, True])
@pytest.mark.parametrize('fault,cleanup_fails', [
    ('disconnect', False), ('disconnect', True),
    ('invalid_json', False), ('invalid_json', True), ('stop_first', False),
])
async def test_stop_preserves_prior_transport_failure_but_not_stop_induced_disconnect(
        tmp_path, root_completed, fault, cleanup_fails):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nroot_completed={root_completed!r}\nfault={fault!r}\n' + '''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        if m['params']['input'][0]['text']=='recover':
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
            continue
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'id':'child-work','method':'item/tool/call','params':{
            'threadId':'child','turnId':'child-turn','tool':'project_code','arguments':{}}})
        if root_completed:
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
    elif method=='fixture/fault':
        emit({'id':m['id'],'result':{}})
        if fault in {'disconnect','stop_first'}:sys.exit(0)
        print('invalid json',flush=True)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable),tmp_path/'runtime')
    started, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_result, close_started, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned, cleanup_interruptions, completions = [], [], []
    original_request, original_close = client.request, client.close
    async def delayed_start_return(method, params, timeout=30):
        result = await original_request(method,params,timeout)
        if method == 'turn/start' and params['input'][0]['text'] != 'recover':
            await release_result.wait()
        return result
    async def delayed_close():
        if fault == 'stop_first':
            close_started.set()
            await release_close.wait()
        await original_close()
    client.request, client.close = delayed_start_return, delayed_close
    async def event(method, params):
        if method == 'turn/completed':completions.append(params['turn']['status'])
    async def tool(name, arguments):
        (tmp_path/'saved.txt').write_text('previous result')
        started.set()
        try:
            await asyncio.Event().wait()
            (tmp_path/'late.txt').write_text('must not happen')
        finally:
            cleaning.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interruptions.append(True)
                raise
            cleaned.append(True)
            if cleanup_fails:raise OSError('owned cleanup failed')
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run',event,tool,timeout=10))
        await asyncio.wait_for(started.wait(),5)
        if fault == 'stop_first':
            task.cancel()
            await asyncio.wait_for(close_started.wait(),5)
        await client.request('fixture/fault',{})
        await asyncio.wait_for(cleaning.wait(),5)
        assert client.closing and not task.done()
        if fault != 'stop_first':task.cancel()
        await asyncio.sleep(.03)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done() and not cleaned and not cleanup_interruptions and not completions
        release_cleanup.set(); release_close.set()
        if fault == 'stop_first':
            with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,5)
        else:
            expected = '协议连接失败' if fault == 'invalid_json' else '连接已断开'
            with pytest.raises(CodexError,match=expected) as caught:await asyncio.wait_for(task,5)
            assert ('owned cleanup failed' in str(caught.value)) == cleanup_fails
        assert cleaned and not cleanup_interruptions
        assert not client.requests and not client.pending and client.process.returncode is not None
        assert not (tmp_path/'late.txt').exists() and (tmp_path/'saved.txt').read_text() == 'previous result'
        release_result.set()
        await client.start([], 'project only')
        assert (await client.turn('recover',event,tool,timeout=5))['status'] == 'completed'
    finally:
        release_result.set(); release_cleanup.set(); release_close.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['late_call', 'already_cancelling'])
async def test_close_rejects_late_tools_and_finishes_existing_cleanup(tmp_path, race):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method=='thread/start': emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        emit({'id':'slow','method':'item/tool/call','params':{
            'threadId':'root','turnId':'root-turn','tool':'project_file','arguments':{'path':'slow'}}})
    elif method=='turn/steer':
        if m['params']['input'][0]['text']=='cancel':
            emit({'method':'serverRequest/resolved','params':{'threadId':'root','requestId':'slow'}})
            emit({'id':m['id'],'result':{'turnId':'root-turn'}})
        else:
            pending_steer=m['id']
            emit({'id':'late','method':'item/tool/call','params':{
                'threadId':'root','turnId':'root-turn','tool':'project_file','arguments':{'path':'late'}}})
    elif m.get('id')=='late':
        emit({'id':pending_steer,'result':{'turnId':'root-turn'}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    started, cleanup_started, release_cleanup, cleaned = [asyncio.Event() for _ in range(4)]

    async def event(method, params): pass

    async def tool(name, arguments):
        if arguments['path'] == 'late':
            (tmp_path / 'late.txt').write_text('must not happen')
            return {}
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            (tmp_path / 'cleanup.txt').write_text('finished')
            cleaned.set()

    task = closing = cancelling = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('work', event, tool, timeout=10))
        await asyncio.wait_for(started.wait(), 5)
        if race == 'already_cancelling':
            cancelling = asyncio.create_task(client.steer('cancel'))
            await asyncio.wait_for(cleanup_started.wait(), 5)
        closing = asyncio.create_task(client.close())
        await asyncio.wait_for(cleanup_started.wait(), 5)
        if race == 'late_call':
            await client.steer('late tool arrives during cleanup')
        else:
            # Closing must wait for a tool already cleaning up, not cancel it again.
            await asyncio.wait({closing}, timeout=.05)
        release_cleanup.set()
        await asyncio.wait_for(closing, 5)
        await asyncio.gather(*(t for t in [task, cancelling] if t), return_exceptions=True)
        assert not (tmp_path / 'late.txt').exists()
        assert cleaned.is_set() and (tmp_path / 'cleanup.txt').read_text() == 'finished'
    finally:
        release_cleanup.set()
        for pending in [closing, cancelling, task]:
            if pending and not pending.done():
                pending.cancel()
        await asyncio.gather(*(t for t in [closing, cancelling, task] if t), return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('root_completed,ending', [
    (False,'timeout'),(True,'timeout'),(False,'cancel'),(True,'cancel'),(False,'release'),(True,'release'),
    (False,'timeout_cleanup'),(True,'timeout_cleanup'),(True,'timeout_cleanup_idle'),
    (False,'timeout_stop_cleanup'),(True,'timeout_stop_cleanup'),
    (False,'timeout_stop_cleanup_failure'),(True,'timeout_stop_cleanup_failure'),
])
async def test_blocked_tool_result_stops_run_or_drains_without_losing_saved_work(tmp_path, monkeypatch, root_completed, ending):
    import agent_platform.codex_app_server as transport
    monkeypatch.setattr(transport,'TOOL_RESPONSE_TIMEOUT_SECONDS',.5,raising=False)
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\nroot_completed={root_completed!r}\nending={ending!r}\n' + '''
import json,sys,time
from pathlib import Path
active={}
read_count=0
def emit(value):print(json.dumps(value),flush=True)
def note(method,**params):emit({'method':method,'params':params})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method in {'thread/start','thread/resume'}:emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        recovery=m['params']['input'][0]['text']=='recover'
        turn='recovery-turn' if recovery else 'root-turn'
        emit({'id':m['id'],'result':{'turn':{'id':turn}}})
        if recovery:note('turn/completed',threadId='root',turn={'id':turn,'status':'completed'})
        else:
            for identity in ('child','sibling'):
                active[identity]=True
                note('thread/started',thread={'id':identity,'parentThreadId':'root'})
                emit({'id':identity,'method':'item/tool/call','params':{'threadId':identity,
                    'turnId':identity+'-turn','tool':'project_code','arguments':{'identity':identity}}})
            if ending!='timeout_cleanup_idle':
                if root_completed:note('turn/completed',threadId='root',turn={'id':turn,'status':'completed'})
                while not Path('read-results').exists():time.sleep(.01)
    elif method=='fixture/complete':
        emit({'id':m['id'],'result':{}})
        note('turn/completed',threadId='root',turn={'id':'root-turn','status':'completed'})
    elif m.get('id') in active:
        identity=m['id'];active[identity]=False
        assert m['result']['success']
        if identity=='child':
            result=json.loads(m['result']['contentItems'][0]['text'])
            assert result['sum']==45 and len(result['payload'])==2*1024*1024
            note('fixture/received')
        note('turn/completed',threadId=identity,turn={'id':identity+'-turn','status':'completed'})
        if not any(active.values()) and not root_completed:
            note('turn/completed',threadId='root',turn={'id':'root-turn','status':'completed'})
    elif method=='thread/read':
        identity=m['params']['threadId']
        emit({'id':m['id'],'result':{'thread':{'id':identity,'parentThreadId':'root',
            'status':{'type':'active' if active[identity] else 'idle'}}}})
        read_count+=1
        if ending=='timeout_cleanup_idle' and read_count==2:
            Path('status-ready').touch()
            while not Path('read-results').exists():time.sleep(.01)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable),tmp_path/'runtime')
    started = {identity:asyncio.Event() for identity in ('child','sibling')}
    cleaned = {identity:asyncio.Event() for identity in started}
    release, received = asyncio.Event(), asyncio.Event()
    cleaning, release_cleanup = asyncio.Event(), asyncio.Event()
    interrupted_cleanup = []
    notices = []
    async def event(method, params):
        if method=='fixture/received':received.set()
        if method=='turn/completed':notices.append(params['turn']['status'])
    async def tool(name, args):
        identity = args['identity']
        started[identity].set()
        try:
            if identity=='child':
                if ending=='timeout_cleanup_idle':
                    while not (client.runtime_dir/'empty-workspace/status-ready').exists():await asyncio.sleep(.005)
                (tmp_path/'computed.json').write_text(json.dumps({'sum':sum(range(10))}))
                return {'sum':45,'payload':'x'*(2*1024*1024)}
            await release.wait()
            (tmp_path/'sibling.txt').write_text('sibling result')
            return {'ok':True}
        finally:
            if identity=='sibling' and ending.startswith('timeout_stop'):
                cleaning.set()
                try:await release_cleanup.wait()
                except asyncio.CancelledError:
                    interrupted_cleanup.append(True)
                    raise
            cleaned[identity].set()
            if identity=='sibling' and (ending.startswith('timeout_cleanup') or ending=='timeout_stop_cleanup_failure'):
                raise OSError('sibling cleanup failed')
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('run',event,tool,timeout=10))
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())),5)
        if ending=='timeout_cleanup_idle':await client.request('fixture/complete',{})
        for _ in range(50):
            if client.process.stdin.transport.get_write_buffer_size()>64*1024:break
            await asyncio.sleep(.005)
        else:raise AssertionError('Actual tool reply never backed up')
        assert not task.done() and not notices
        if ending=='release':
            (client.runtime_dir/'empty-workspace/read-results').touch()
            await asyncio.wait_for(received.wait(),2)
            assert not task.done() and not cleaned['sibling'].is_set()
            release.set()
            assert (await asyncio.wait_for(task,5))['status']=='completed'
            assert notices==['completed'] and (tmp_path/'sibling.txt').read_text()=='sibling result'
        else:
            if ending=='cancel':task.cancel()
            if ending.startswith('timeout_stop'):
                await asyncio.wait_for(cleaning.wait(),3)
                for _ in range(2):
                    task.cancel()
                    await asyncio.wait({task},timeout=.03)
                    assert not task.done() and not cleaned['sibling'].is_set() and not interrupted_cleanup
                release_cleanup.set()
            with pytest.raises(asyncio.CancelledError if ending=='cancel' else CodexError) as caught:
                await asyncio.wait_for(asyncio.shield(task),3)
            if ending.startswith('timeout_cleanup'):
                assert '工具结果未能发送' in str(caught.value) and 'sibling cleanup failed' in str(caught.value)
            if ending.startswith('timeout_stop'):
                assert '工具结果未能发送' in str(caught.value) and not interrupted_cleanup
                assert ('sibling cleanup failed' in str(caught.value)) is ending.endswith('failure')
            assert not notices and not (tmp_path/'sibling.txt').exists()
            assert client.process.returncode is not None
        assert all(flag.is_set() for flag in cleaned.values()) and not client.requests and not client.pending
        assert json.loads((tmp_path/'computed.json').read_text())=={'sum':45}
        await client.close()
        await client.start([], 'project only',client.thread_id)
        assert (await client.turn('recover',event,tool,timeout=5))['status']=='completed'
    finally:
        release_cleanup.set()
        if task and not task.done():task.cancel()
        if task:await asyncio.gather(task,return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('delay', ['never_reads', 'write_and_reply', 'temporary_backpressure'])
async def test_request_deadline_includes_actual_pipe_backpressure(tmp_path, delay):
    executable = tmp_path/'codex'
    executable.write_text(f'#!{sys.executable}\ndelay={delay!r}\n' + '''
import json,sys,time
from pathlib import Path
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method in {'thread/start','thread/resume'}:
        emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
        if method=='thread/start':
            while not Path('read-input').exists():time.sleep(.01)
    elif method=='turn/start':
        recovery=m['params']['input'][0]['text']=='recover'
        if not recovery:
            Path('received-size').write_text(str(len(m['params']['input'][0]['text'])))
            time.sleep(.3 if delay=='write_and_reply' else .02)
        turn='recovery-turn' if recovery else 'root-turn'
        emit({'id':m['id'],'result':{'turn':{'id':turn}}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':turn,'status':'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable),tmp_path/'runtime')
    original_request = client.request
    async def request(method, params, **kwargs):
        if method=='turn/start':kwargs['timeout']=.5
        return await original_request(method,params,**kwargs)
    client.request = request  # Shorten only the existing request deadline; real pipe I/O remains intact.
    notices = []
    async def event(method, params):
        if method=='turn/completed':notices.append(params['turn']['status'])
    saved = tmp_path/'saved.txt'
    saved.write_text('previous result')
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('x'*(2*1024*1024),event,None,timeout=10))
        for _ in range(50):
            if client.process.stdin.transport.get_write_buffer_size()>64*1024:break
            await asyncio.sleep(.005)
        else:raise AssertionError('Actual process pipe never backed up')
        assert not task.done() and not notices
        if delay!='never_reads':
            await asyncio.sleep(.3 if delay=='write_and_reply' else .02)
            (client.runtime_dir/'empty-workspace/read-input').touch()
        if delay=='temporary_backpressure':
            assert (await asyncio.wait_for(asyncio.shield(task),3))['status']=='completed'
            assert notices==['completed']
        else:
            with pytest.raises(CodexError,match='turn/start 长时间无响应'):
                await asyncio.wait_for(asyncio.shield(task),3)
            assert not notices and client.process.returncode is not None
        assert not client.pending and not client.requests and saved.read_text()=='previous result'
        if delay!='never_reads':
            assert int((client.runtime_dir/'empty-workspace/received-size').read_text())==2*1024*1024
        await client.close()
        await client.start([], 'project only',client.thread_id)
        assert (await client.turn('recover',event,None,timeout=5))['status']=='completed'
    finally:
        if task and not task.done():task.cancel()
        if task:await asyncio.gather(task,return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['idle_timeout', 'deadline', 'cancel'])
async def test_aborted_turn_closes_transport_and_tools_then_can_resume(tmp_path, stop):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\nstop={stop!r}\n' + '''
import json,sys,time
resumed=False
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize': emit({'id':m['id'],'result':{}})
    elif method in ['thread/start','thread/resume']:
        resumed=method=='thread/resume'
        if resumed: assert m['params']['threadId']=='root'
        emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        identity='resumed-turn' if resumed else 'root-turn'
        emit({'id':m['id'],'result':{'turn':{'id':identity}}})
        if resumed:
            emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':identity,'status':'completed'}}})
        else:
            if stop=='idle_timeout': time.sleep(.2)
            emit({'id':'late','method':'item/tool/call','params':{
                'threadId':'root','turnId':identity,'tool':'project_file','arguments':{}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    (tmp_path / 'saved.txt').write_text('previous result')

    async def event(method, params): pass

    async def tool(name, arguments):
        started.set()
        try:
            await release.wait()
            (tmp_path / 'late.txt').write_text('must not happen')
        finally:
            await asyncio.sleep(.01)
            cleaned.set()

    async def run():
        if stop == 'deadline':
            async with asyncio.timeout(.15):
                return await client.turn('work', event, tool, timeout=.05)
        return await client.turn('work', event, tool, timeout=.05)

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(run())
        if stop != 'idle_timeout':
            await asyncio.wait_for(started.wait(), 5)
        if stop == 'cancel':
            task.cancel()
        expected = {'idle_timeout': CodexError, 'deadline': TimeoutError, 'cancel': asyncio.CancelledError}[stop]
        with pytest.raises(expected):
            await asyncio.wait_for(task, 5)
        release.set()
        await asyncio.sleep(.3)  # Include a late server request after the idle timeout.
        assert not (tmp_path / 'late.txt').exists()
        assert stop == 'idle_timeout' or cleaned.is_set()
        assert client.process.returncode is not None
        assert client.thread_id == 'root' and (tmp_path / 'saved.txt').read_text() == 'previous result'
        assert await client.start([], 'continue project', client.thread_id) == 'root'
        assert (await client.turn('continue', event, tool, timeout=2))['id'] == 'resumed-turn'
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', [False, True])
async def test_parent_completion_waits_for_child_result_and_followup_tool(tmp_path, stop):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
active=True
def emit(value): print(json.dumps(value),flush=True)
def call(identity):
    emit({'id':identity,'method':'item/tool/call','params':{'threadId':'child','turnId':'child-turn',
        'tool':'project_file','arguments':{'action':'write','path':identity+'.txt','content':identity}}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'method':'turn/started','params':{'threadId':'child','turn':{'id':'child-turn'}}})
        call('first')
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
    elif method=='thread/read':
        assert m['params']=={'threadId':'child','includeTurns':False}
        emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root',
            'status':{'type':'active','activeFlags':[]} if active else {'type':'idle'}}}})
    elif m.get('id')=='first':
        assert m['result']['success'];call('second')
    elif m.get('id')=='second':
        assert m['result']['success'];active=False
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':'child-turn','status':'completed'}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    started, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def event(method, params):
        if method == 'turn/completed':
            observed.append((tmp_path / 'second.txt').exists())

    async def tool(name, arguments):
        if arguments['path'] == 'first.txt':
            started.set()
            await release.wait()
        (tmp_path / arguments['path']).write_text(arguments['content'])
        return {'written': arguments['path']}

    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('delegate', event, tool, timeout=2))
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait({task}, timeout=.05)
        assert not task.done() and not observed
        assert client.turn_id is None  # Root cannot be steered while children drain.
        if stop:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not (tmp_path / 'first.txt').exists() and not observed
            assert client.process.returncode is not None
            return
        release.set()
        assert (await asyncio.wait_for(task, 5))['status'] == 'completed'
        assert (tmp_path / 'first.txt').read_text() == 'first'
        assert (tmp_path / 'second.txt').read_text() == 'second'
        assert observed == [True]
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_child_status_reads_do_not_keep_stalled_work_alive(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        emit({'method':'turn/started','params':{'threadId':'resumed-child','turn':{'id':'child-turn'}}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':'resumed-child','parentThreadId':'root',
            'status':{'type':'active','activeFlags':[]}}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    async def event(method, params): pass
    try:
        await client.start([], 'project only')
        with pytest.raises(CodexError, match='子任务长时间无响应'):
            await asyncio.wait_for(client.turn('work', event, None, timeout=.1), 2)
        assert client.process.returncode is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_finished_turn_tools_are_rejected_between_and_during_followup_turns(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
number=0
def emit(value):print(json.dumps(value),flush=True)
def call(identity,thread,turn):
    emit({'id':identity,'method':'item/tool/call','params':{'threadId':thread,'turnId':turn,
        'tool':'project_file','arguments':{'action':'write','path':identity+'.txt','content':identity}}})
def end(thread,turn):
    emit({'method':'turn/completed','params':{'threadId':thread,'turn':{'id':turn,'status':'completed'}}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        number+=1
        emit({'id':m['id'],'result':{'turn':{'id':'root-'+str(number)}}})
        if number==1:
            emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
            end('child','child-old');end('root','root-1')
        else:call('old-child-in-new-turn','child','child-old')
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root','status':{'type':'idle'}}}})
    elif method=='test/emit-late-call':
        probe=m['id'];call('old-child-between-turns','child','child-old')
    elif m.get('id')=='old-child-between-turns':
        assert not m['result']['success'];call('old-root-between-turns','root','root-1')
    elif m.get('id')=='old-root-between-turns':
        assert not m['result']['success'];emit({'id':probe,'result':{}})
    elif m.get('id')=='old-child-in-new-turn':
        assert not m['result']['success'];call('old-root-in-new-turn','root','root-1')
    elif m.get('id')=='old-root-in-new-turn':
        assert not m['result']['success'];call('current-result','root','root-2')
    elif m.get('id')=='current-result':
        assert m['result']['success'];end('root','root-2')
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    writes = []
    async def event(method, params): pass
    async def tool(name, arguments):
        (tmp_path / arguments['path']).write_text(arguments['content'])
        writes.append(arguments['path'])
        return {}
    try:
        await client.start([], 'project only')
        assert (await client.turn('first', event, tool, timeout=2))['status']=='completed'
        await client.request('test/emit-late-call', {})
        assert (await client.turn('followup', event, tool, timeout=2))['status']=='completed'
        assert writes==['current-result.txt']
        assert (tmp_path / 'current-result.txt').read_text()=='current-result'
        assert not list(tmp_path.glob('old-*.txt'))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_protocol_failure_wakes_parent_waiting_for_child_without_tools(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        emit({'method':'thread/started','params':{'thread':{'id':'child','parentThreadId':'root'}}})
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
    elif method=='thread/read':
        emit({'id':m['id'],'result':{'thread':{'id':'child','parentThreadId':'root',
            'status':{'type':'active','activeFlags':[]}}}})
        emit({'method':'fixture/ready','params':{}})
    elif method=='fixture/fault':
        emit({'id':m['id'],'result':{}})
        print('invalid json',flush=True)
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    ready = asyncio.Event()
    async def event(method, params):
        if method == 'fixture/ready': ready.set()
    task = None
    try:
        await client.start([], 'project only')
        task = asyncio.create_task(client.turn('work', event, None, timeout=10))
        await asyncio.wait_for(ready.wait(), 2)
        await client.request('fixture/fault', {})
        with pytest.raises(CodexError, match='连接'):
            await asyncio.wait_for(task, 2)
        assert client.process.returncode is not None
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reconnect', [False, True])
async def test_scoped_usage_deltas_ignore_restore_replay_duplicates_and_other_threads(tmp_path, reconnect):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
from pathlib import Path
number=int(Path('number').read_text()) if Path('number').exists() else 0
def emit(value):print(json.dumps(value),flush=True)
def note(method,**params):emit({'method':method,'params':params})
def usage(thread,turn,total):
    note('thread/tokenUsage/updated',threadId=thread,turnId=turn,tokenUsage={'total':{'totalTokens':total}})
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':
        emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
        usage('root','old-root',1000)
    elif method=='thread/resume':
        emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
        usage('root','root-1',1600)
        usage('child','child-1',8300)
    elif method=='turn/start':
        number+=1;turn='root-'+str(number)
        Path('number').write_text(str(number))
        if number==1:usage('root',turn,1500)
        emit({'id':m['id'],'result':{'turn':{'id':turn}}})
        if number==1:
            usage('root',turn,1500)
            note('thread/started',thread={'id':'child','parentThreadId':'root'})
            usage('child','old-child',8000)
            note('turn/started',threadId='child',turn={'id':'child-1'})
            for total in [8100,8100,8050,8300]:usage('child','child-1',total)
            note('turn/started',threadId='grandchild',turn={'id':'grandchild-1'})
            usage('grandchild','grandchild-1',50)
            note('turn/started',threadId='unrelated',turn={'id':'unrelated-1'})
            usage('unrelated','unrelated-1',9999999)
            usage('root',turn,1600)
        else:
            usage('child','child-1',8300)
            note('turn/started',threadId='child',turn={'id':'child-2'})
            usage('child','child-2',8600)
            usage('root',turn,1700)
        note('turn/completed',threadId='root',turn={'id':turn,'status':'completed'})
    elif method=='thread/read':
        identity=m['params']['threadId']
        emit({'id':m['id'],'result':{'thread':{'id':identity,
            'parentThreadId':{'child':'root','grandchild':'child','unrelated':None}[identity],
            'status':{'type':'idle'}}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path / 'runtime')
    events = []
    async def event(method, params):
        if method == 'thread/tokenUsage/updated':events.append(params)
    try:
        await client.start([], 'project only')
        await client.turn('first', event, None, timeout=2)
        assert sum(e.get('usageDelta',0) for e in events)==950
        assert {e['threadId'] for e in events}=={'root','child','grandchild'}
        assert not any(e['turnId'] in {'old-root','old-child'} for e in events)
        events.clear()
        if reconnect:
            await client.close()
            client = CodexAppServer(str(executable), tmp_path / 'runtime')
            await client.start([], 'project only', thread_id='root')
        await client.turn('continue', event, None, timeout=2)
        assert sum(e.get('usageDelta',0) for e in events)==400
        assert {e['turnId'] for e in events}=={'root-2','child-2'}
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'disconnect', 'cleanup_failure'])
async def test_completion_waiting_for_steer_ack_stops_owned_tools_on_abort(tmp_path, ending):
    executable = tmp_path / 'codex'
    executable.write_text(f'#!{sys.executable}\n' + '''
import json,sys
def emit(value):print(json.dumps(value),flush=True)
for line in sys.stdin:
    m=json.loads(line);method=m.get('method')
    if m.get('method') == 'skills/list':
        print(json.dumps({'id':m['id'],'result':{'data':[]}}),flush=True)
        continue
    if method=='initialize':emit({'id':m['id'],'result':{}})
    elif method=='thread/start':emit({'id':m['id'],'result':{'thread':{'id':'root'}}})
    elif method=='turn/start':
        emit({'id':m['id'],'result':{'turn':{'id':'root-turn'}}})
        for identity in ('child','sibling'):
            emit({'method':'thread/started','params':{'thread':{'id':identity,'parentThreadId':'root'}}})
            emit({'id':identity,'method':'item/tool/call','params':{'threadId':identity,
                'turnId':identity+'-turn','tool':'project_file','arguments':{'identity':identity}}})
        emit({'method':'fixture/ready','params':{}})
    elif method=='turn/steer':
        emit({'method':'turn/completed','params':{'threadId':'root','turn':{'id':'root-turn','status':'completed'}}})
        emit({'method':'fixture/pending','params':{}})
    elif method=='fixture/disconnect':
        emit({'id':m['id'],'result':{}});sys.exit(0)
    elif method=='fixture/cleanup':
        emit({'id':m['id'],'result':{}})
        emit({'method':'turn/completed','params':{'threadId':'child','turn':{'id':'child-turn','status':'interrupted'}}})
    elif method=='thread/read':
        identity=m['params']['threadId']
        emit({'id':m['id'],'result':{'thread':{'id':identity,'parentThreadId':'root','status':{'type':'idle'}}}})
''')
    executable.chmod(0o700)
    client = CodexAppServer(str(executable), tmp_path/'runtime')
    ready, pending = asyncio.Event(), asyncio.Event()
    started = {identity:asyncio.Event() for identity in ('child','sibling')}
    cleaned = {identity:asyncio.Event() for identity in started}
    completions = []
    async def event(method, params):
        if method=='fixture/ready':ready.set()
        if method=='fixture/pending':pending.set()
        if method=='turn/completed':completions.append(params)
    async def tool(name, args):
        identity = args['identity']
        started[identity].set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned[identity].set()
            if identity=='child' and ending=='cleanup_failure':raise OSError('owned cleanup failed')
    turn = steer = None
    try:
        await client.start([], 'project only')
        turn = asyncio.create_task(client.turn('work', event, tool, timeout=2))
        await asyncio.wait_for(ready.wait(), 2)
        await asyncio.wait_for(asyncio.gather(*(flag.wait() for flag in started.values())), 2)
        steer = asyncio.create_task(client.steer('supplement'))
        await asyncio.wait_for(pending.wait(), 2)
        await asyncio.wait({turn}, timeout=.03)
        assert not turn.done() and not completions
        if ending=='cancel':turn.cancel()
        else:await client.request('fixture/cleanup' if ending=='cleanup_failure' else 'fixture/disconnect', {})
        with pytest.raises(asyncio.CancelledError if ending=='cancel' else CodexError) as caught:
            await asyncio.wait_for(asyncio.shield(turn), 2)
        if ending=='cleanup_failure':assert 'owned cleanup failed' in str(caught.value)
        with pytest.raises(CodexError):
            await asyncio.wait_for(steer, 2)
        assert not completions and not client.steering
        assert all(flag.is_set() for flag in cleaned.values()) and not client.requests
        assert client.process.returncode is not None
    finally:
        for task in (turn, steer):
            if task and not task.done():task.cancel()
        await asyncio.gather(*(t for t in (turn, steer) if t), return_exceptions=True)
        await client.close()
