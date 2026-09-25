"""Search persists real provider responses without inference or cross-project access."""
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from agent_platform import project_search as search
from tests.test_projects import configured  # noqa: F401
from tests.test_users import platform, signup, project  # noqa: F401


@pytest.fixture
def provider(monkeypatch):
    requests = []
    state = {'status': 200, 'body': {'results': [
        {'title': '<b>Splitting data</b>', 'url': 'https://docs.test/splits',
         'content': 'Keep groups <b>separate</b>.<script>hidden</script>', 'engines': ['example']},
        {'title': 'Duplicate', 'url': 'https://docs.test/splits#part'},
        {'title': 'Unsafe link', 'url': 'javascript:alert(1)'},
        {'title': 'Login', 'url': 'https://user:secret@docs.test/'},
        {'title': 'Oversized link', 'url': 'https://docs.test/' + 'x' * 4000},
        {'title': 'Another paper', 'url': 'https://papers.test/article', 'content': 'A second result.'}],
        'unresponsive_engines': [['slow engine', 'timeout']]}}
    def respond(request):
        requests.append(request)
        if state.get('error'):
            raise state['error']
        return httpx.Response(state['status'], content=state.get('raw') or json.dumps(state['body']).encode())
    monkeypatch.setattr(search, 'Client', lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kw))
    return state, requests


def lookup(client, pid, headers=None, **arguments):
    return client.post('/api/v1/projects/' + pid + '/agent-tools', headers=headers,
        json={'name': 'project_search', 'arguments': {'query': 'grouped cross validation', **arguments}})


def test_search_saves_bounded_sources_and_raw_response_without_running(configured, provider):
    client, _, p, settings = configured
    settings.searxng_url = 'http://search:8080'
    state, requests = provider
    response = lookup(client, p['id'])
    assert response.status_code == 200, response.text
    out = response.json()
    assert len(out['results']) == 2 and out['partial']
    assert out['results'][0]['snippet'] == 'Keep groups separate.'
    assert out['results'][0]['title'] == 'Splitting data'
    assert 'hidden' not in out['results'][0]['snippet']
    assert out['engine_errors'] and '未读取原文' in out['note']
    assert requests[0].method == 'POST' and str(requests[0].url) == 'http://search:8080/search'
    params = parse_qs(requests[0].content.decode())
    assert params['q'] == ['grouped cross validation'] and params['format'] == ['json']
    assert 'Authorization' not in requests[0].headers and 'Cookie' not in requests[0].headers
    root = settings.workspace_root / p['id']
    raw = (root / out['original_path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == out['response_sha256']
    assert json.loads(raw) == state['body']
    markdown = (root / out['source_path']).read_text()
    assert '[查看原文](https://docs.test/splits)' in markdown and '部分引擎未响应' in markdown
    assert client.get('/api/v1/projects/' + p['id'] + '/tasks').json() == []
    changed = lookup(client, p['id'], query='time series leakage', category='science', max_results=1).json()
    assert changed['source_path'] != out['source_path'] and len(changed['results']) == 1
    assert (root / out['source_path']).read_text() == markdown
    assert parse_qs(requests[-1].content.decode())['categories'] == ['science']


@pytest.mark.parametrize('change, message', [
    ({'status': 403}, '启用json'), ({'status': 302}, 'HTTP 302'),
    ({'status': 429}, 'HTTP 429'), ({'raw': b'<html>upstream error</html>'}, '有效JSON'),
    ({'body': {'results': {}}}, '结果列表'),
    ({'raw': b'x' * (search.MAX_BYTES + 1)}, '超过2 MB'),
    ({'error': httpx.ReadTimeout('secret endpoint credentials must not surface')}, '连接失败或超时'),
])
def test_failed_search_no_fake_results_or_partial_files(configured, provider, change, message):
    client, _, p, settings = configured
    settings.searxng_url = 'http://search:8080'
    provider[0].update(change)
    response = lookup(client, p['id'])
    assert response.status_code == 422 and message in response.text, response.text
    assert 'secret endpoint credentials' not in response.text
    assert not list((settings.workspace_root / p['id'] / 'results').glob('source-search-*'))


def test_missing_config_network_policy_and_unsupported_query_do_not_send(configured, provider):
    client, app, p, settings = configured
    settings.searxng_url = ''
    assert 'SEARXNG_URL' in lookup(client, p['id']).text
    settings.searxng_url = 'http://name:private@search:8080'
    assert '配置无效' in lookup(client, p['id']).text
    settings.searxng_url = 'http://search:8080'
    app.state.services.harness.network_egress_policy = 'none'
    assert '网络配置不允许' in lookup(client, p['id']).text
    app.state.services.harness.network_egress_policy = 'allowlist'
    app.state.services.harness.network_egress_allowlist = ['other.test']
    assert '网络配置不允许' in lookup(client, p['id']).text
    assert lookup(client, p['id'], query='  ').status_code == 422
    assert lookup(client, p['id'], url='http://outside.test').status_code == 422
    assert provider[1] == []


@pytest.mark.parametrize('errors', [[], [['all engines', 'timeout']]])
def test_empty_result_preserves_actual_failure_scope(configured, provider, errors):
    client, _, p, settings = configured
    settings.searxng_url = 'http://search:8080'
    provider[0]['body'] = {'results': [], 'unresponsive_engines': errors}
    out = lookup(client, p['id']).json()
    assert out['results'] == [] and out['partial'] == bool(errors)
    assert '不代表不存在' in out['note']


def test_employee_search_and_download_isolated(platform, provider):
    client, app = platform
    app.state.services.settings.searxng_url = 'http://search:8080'
    _, a = signup(client, '搜索员工甲')
    _, b = signup(client, '搜索员工乙')
    pid = project(client, a)
    response = lookup(client, pid, a)
    assert response.status_code == 200, response.text
    out = response.json()
    for artifact in out['artifacts']:
        url = '/api/v1/applications/' + pid + '/workspace/files/' + artifact['file_path']
        assert client.get(url, headers=a).status_code == 200
        assert client.get(url, headers=b).status_code == 404
    assert lookup(client, pid, b).status_code == 404
    assert len(provider[1]) == 1


def test_failed_save_cleans_partial_snapshot(configured, provider, monkeypatch):
    from pathlib import Path
    client, _, p, settings = configured
    settings.searxng_url = 'http://search:8080'
    original = Path.write_text
    def fail(path, *args, **kwargs):
        if path.name == 'sources.md':
            raise OSError('disk full')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail)
    response = lookup(client, p['id'])
    assert response.status_code == 422 and 'disk full' in response.text, response.text
    assert not list((settings.workspace_root / p['id'] / 'results').glob('source-search-*'))
