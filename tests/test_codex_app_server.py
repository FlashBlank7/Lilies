import asyncio
import sys

import pytest

from agent_platform.codex_app_server import CodexAppServer, CodexError, validate_codex_version


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
    client = CodexAppServer(str(executable), tmp_path/'runtime')
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
        assert p['developerInstructions'] == 'current project instructions'
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
