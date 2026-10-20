import asyncio
import base64
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from agent_platform.connected_model import ConnectedModel
from agent_platform.model_connections import ModelConnections, project_model_role
from agent_platform.model_images import image_blocks
from agent_platform.tools.core import BashTool
from tests.test_projects import configured, edge, graph, node, ref, settled, start

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6n1sAAAAASUVORK5CYII=')


@pytest.mark.parametrize('status,expected_calls,expected_status', [
    (401, 1, 'failed'), (402, 1, 'failed'), (429, 2, 'succeeded'), (503, 2, 'succeeded'),
])
def test_model_retry_recovers_transient_errors_but_stops_for_account_errors(configured, monkeypatch, status, expected_calls, expected_status):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, text='upstream echoed private-key')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'recovered'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(ModelConnections, 'provider', lambda self, project_id, role='main': ConnectedModel(
        self.load(project_id, role), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond)))
    assert client.put(base + '/agent-session', json={
        'provider': 'api', 'base_url': 'https://model.test/v1', 'model': 'test-model',
        'api_key': 'private-key', 'runtime_enabled': True}).status_code == 200
    model = node('model', 'llm', prompt='read this input')
    model['retry'] = {'enabled': True, 'max_attempts': 3, 'delay_seconds': 0}
    graph(client, pid, [node('start', 'start', inputs=[]), model,
        node('end', 'end', outputs={'answer': ref('model', 'text')})], [edge('start', 'model'), edge('model', 'end')])
    task = settled(client, base, start(client, base, 'retry', mode='workflow', workflow_id=pid))
    assert task['status'] == expected_status, task
    assert len(requests) == expected_calls
    assert 'private-key' not in task['error']
    if status == 402:
        assert '账户' in task['error']
    if expected_status == 'succeeded':
        assert task['outputs']['answer'] == 'recovered'


def test_reuse_connection_without_exposing_key_or_starting_an_agent(configured):
    client, app, source, _ = configured
    source_base = '/api/v1/projects/' + source['id']
    configured_key = {'provider': 'api', 'model': 'main-model', 'base_url': 'https://model.test/v1',
                      'api_key': 'private-source-key', 'runtime_enabled': True}
    assert client.put(source_base + '/agent-session', json=configured_key).status_code == 200
    target = client.post('/api/v1/projects', json={'name': 'manual project'}).json()['id']
    base = '/api/v1/projects/' + target
    copied = client.post(base + '/model-connection/copy', json={'source_project_id': source['id']})
    assert copied.status_code == 200, copied.text
    assert copied.json()['has_api_key'] and 'private-source-key' not in copied.text
    assert copied.json()['status'] == 'idle' and copied.json()['thread_id'] is None
    assert app.state.services.local_agents.connections.load(target).api_key.get_secret_value() == 'private-source-key'
    before = client.get(base + '/agent-session').json()
    vision = client.put(base + '/vision-model', json={**configured_key, 'model': 'vision-model', 'api_key': 'private-vision-key'})
    assert vision.status_code == 200 and 'private-vision-key' not in vision.text
    assert client.get(base + '/agent-session').json()['session_id'] == before['session_id']
    assert client.get(base + '/agent-session').json()['model'] == 'main-model'
    assert client.get(base + '/vision-model').json()['model'] == 'vision-model'
    assert client.put(base + '/vision-model', json={'provider': 'codex'}).status_code == 422
    copied_vision = client.post(base + '/model-connection/copy', json={
        'source_project_id': source['id'], 'source_role': 'main', 'role': 'vision'})
    assert copied_vision.status_code == 200, copied_vision.text
    assert copied_vision.json()['has_api_key'] and 'private-source-key' not in copied_vision.text
    assert app.state.services.local_agents.connections.load(target, 'vision').api_key.get_secret_value() == 'private-source-key'
    assert client.put(base + '/vision-model', json={
        'provider': 'api', 'model': 'separate-vision-model', 'base_url': 'https://model.test/v1',
        'runtime_enabled': True}).status_code == 200
    assert client.get(base + '/agent-session').json()['session_id'] == before['session_id']
    assert client.get(base + '/agent-session').json()['model'] == 'main-model'
    assert client.get(base + '/vision-model').json()['model'] == 'separate-vision-model'
    assert app.state.services.local_agents.connections.load(target, 'vision').api_key.get_secret_value() == 'private-source-key'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_visual_node_uses_separate_connection_and_actual_workspace_image(configured, monkeypatch, protocol):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    seen = []
    def respond(request):
        body = json.loads(request.content); seen.append(body)
        assert body['model'] == 'visual-model'
        content = body['messages'][-1]['content']
        if protocol == 'openai':
            assert content[1]['type'] == 'image_url'
            assert base64.b64decode(content[1]['image_url']['url'].split(',', 1)[1]) == PNG
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"label":"diagram"}'}, 'finish_reason': 'stop'}]})
        assert content[1]['source']['media_type'] == 'image/png'
        assert base64.b64decode(content[1]['source']['data']) == PNG
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': '{"label":"diagram"}'}], 'stop_reason': 'end_turn'})
    monkeypatch.setattr(ModelConnections, 'provider', lambda self, project_id, role='main': ConnectedModel(self.load(project_id, role), self.root / 'test', egress_enabled=True, transport=httpx.MockTransport(respond), supports_images=role == 'vision'))
    assert client.put(base + '/vision-model', json={'provider': 'api', 'protocol': protocol,
        'base_url': 'https://vision.test/v1', 'model': 'visual-model', 'api_key': 'private-visual-key', 'runtime_enabled': True}).status_code == 200
    image = settings.workspace_root / pid / 'results' / 'page.png'
    image.parent.mkdir(); image.write_bytes(PNG)
    graph(client, pid, [node('start', 'start', inputs=[{'name': 'images', 'type': 'array'}]),
        node('read', 'llm', model_role='vision', prompt='read page', images=ref('$inputs', 'images'), structured_output={'type': 'object'}),
        node('end', 'end', outputs={'result': ref('read', 'structured')})], [edge('start', 'read'), edge('read', 'end')])
    result = settled(client, base, start(client, base, 'visual', inputs={'images': [{'file_path': 'results/page.png'}]}))
    assert result['status'] == 'succeeded', result
    assert result['outputs'] == {'result': {'label': 'diagram'}}
    assert len(seen) == 1 and project_model_role.get() == 'main'
    missing = settled(client, base, start(client, base, 'missing', inputs={'images': [{'file_path': 'results/missing.png'}]}))
    assert missing['status'] == 'failed' and '不存在' in missing['error']
    assert len(seen) == 1


def test_images_do_not_follow_external_files_and_reject_non_images(tmp_path):
    workspace = tmp_path / 'project'; workspace.mkdir()
    outside = tmp_path / 'secret.png'; outside.write_bytes(PNG)
    (workspace / 'link.png').symlink_to(outside)
    (workspace / 'fake.png').write_text('not an image')
    for path in ['../secret.png', str(outside), 'link.png', 'fake.png']:
        with pytest.raises(ValueError):
            image_blocks(workspace, [{'file_path': path}])


@pytest.mark.asyncio
async def test_bash_passes_json_on_stdin_without_evaluating_shell_text(tmp_path):
    payload = {'name': "中文 ' \" $(touch unexpected) `touch other`\nnext"}
    class Sandbox:
        async def run(self, argv, *, stdin=None, timeout=None):
            result = await asyncio.to_thread(subprocess.run, argv, input=stdin, text=True,
                capture_output=True, cwd=tmp_path, timeout=timeout)
            return SimpleNamespace(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)
    result = await BashTool().execute({'command': "python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False))'", 'stdin': payload}, SimpleNamespace(sandbox=Sandbox()))
    assert not result.is_error
    assert result.structured_output['json'] == payload
    assert result.structured_output['exit_code'] == 0
    assert '[exit_code=0]' in result.content
    assert not list(tmp_path.iterdir())
