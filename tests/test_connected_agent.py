import sys

import pytest

from agent_platform.connected_agent import ConnectedAgent
from agent_platform.model_connections import ModelConnection
from tests.test_model_connections import complete


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,thinking', [('claude', 'high'), ('claude', 'off'), ('kimi', 'enabled'), ('kimi', 'off')])
async def test_explicit_agent_adapter_preserves_local_session_protocol(tmp_path, provider, thinking):
    executable = tmp_path / provider
    executable.write_text(f'#!{sys.executable}\nprovider={provider!r}\nthinking={thinking!r}\n' + '''
import json, os, pathlib, sys
args = sys.argv[1:]
assert json.loads(args[args.index('--mcp-config') + 1]) == {'mcpServers': {}}
data = json.loads(sys.stdin.read())
if provider == 'claude':
    assert args[args.index('--tools') + 1] == ''
    assert '--strict-mcp-config' in args and '--disable-slash-commands' in args
    assert args[args.index('--setting-sources') + 1] == ''
    assert pathlib.Path(args[args.index('--system-prompt-file') + 1]).read_text() == 'test system'
    assert json.loads(args[args.index('--settings') + 1])['disableAllHooks']
    assert data[0]['content'][0]['text'] == 'original input'
    if thinking == 'off':
        assert os.environ['MAX_THINKING_TOKENS'] == '0'
    else:
        assert args[args.index('--effort') + 1] == thinking
    print(json.dumps({'result': 'local result'}))
else:
    assert '--print' in args
    assert ('--no-thinking' if thinking == 'off' else '--thinking') in args
    assert 'tools: []' in pathlib.Path(args[args.index('--agent-file') + 1]).read_text()
    assert data['system'] == 'test system'
    assert data['conversation'][0]['content'][0]['text'] == 'original input'
    print(json.dumps({'role': 'assistant', 'content': [{'type': 'text', 'text': 'local result'}]}))
''')
    executable.chmod(0o700)
    agent = ConnectedAgent(ModelConnection(provider=provider, executable=str(executable), thinking=thinking), tmp_path / 'sessions')
    result = await complete(agent)
    assert result.blocks[0].text == 'local result'
