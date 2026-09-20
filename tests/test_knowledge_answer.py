"""Execute the same editable definition delivered to the page, with raw API transport doubles."""
import json

import httpx

from tests.test_project_knowledge import knowledge, setup_index  # noqa: F401
from tests.test_projects import configured, graph, start, settled  # noqa: F401


def definition(client, base, **options):
    response = client.post(base + '/knowledge/manual/workflow-definition', json={'mode': 'answer', **options})
    assert response.status_code == 200, response.text
    return response.json()


def test_answer_definition_saves_before_models_or_index_exist(knowledge):
    client, app, project, settings, base, service, calls = knowledge
    client.put(base + '/knowledge/manual', json={'name': '手册'}).raise_for_status()
    workflow = definition(client, base)
    graph(client, project['id'], **workflow)
    assert client.get(base + '/tasks').json() == [] and calls == []
    assert {n['type'] for n in workflow['nodes']} == {'start', 'knowledge_search', 'if_else', 'template_transform', 'llm', 'end'}
    task = settled(client, base, start(client, base, 'unconfigured', inputs={'query': 'question'}))
    assert task['status'] == 'failed' and 'search' in task['error'] and '索引' in task['error']


def test_answer_uses_one_raw_llm_call_and_empty_search_uses_none(knowledge, monkeypatch):
    client, app, project, settings, base, service, calls = knowledge
    indexed = setup_index(knowledge)
    workflow = definition(client, base)
    graph(client, project['id'], **workflow)
    missing = settled(client, base, start(client, base, 'no-chat-model', inputs={'query': 'feline'}))
    assert missing['status'] == 'failed' and '模型' in missing['error']
    empty_unconfigured = settled(client, base, start(client, base, 'empty-no-chat-model', inputs={'query': 'wheels'}))
    assert empty_unconfigured['status'] == 'succeeded' and empty_unconfigured['outputs']['knowledge']['retrieved_count'] == 0
    client.put(base + '/agent-session', json={'provider':'api', 'base_url':'http://127.0.0.1:9001/v1',
        'model':'answer-model', 'api_key':'test-only', 'runtime_enabled':True}).raise_for_status()
    requests = []
    status = 200
    def respond(request):
        assert str(request.url) == 'http://127.0.0.1:9001/v1/chat/completions'
        requests.append(json.loads(request.content))
        return httpx.Response(status, json={'choices':[{'message':{'content':'Cats enjoy sleeping. [1]'}, 'finish_reason':'stop'}],
            'usage':{'prompt_tokens':50,'completion_tokens':8,'total_tokens':58}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**{**kwargs, 'transport':httpx.MockTransport(respond)}))
    task = settled(client, base, start(client, base, 'answer', inputs={'query': 'feline'}))
    assert task['status'] == 'succeeded', task
    assert len(requests) == 1 and 'tools' not in requests[0]
    assert requests[0]['model'] == 'answer-model'
    prompt = requests[0]['messages'][-1]['content']
    assert 'feline' in prompt and '[1]' in prompt and 'A cat enjoys sleeping.' in prompt
    assert task['outputs']['markdown'] == 'Cats enjoy sleeping. [1]'
    assert task['outputs']['knowledge']['version'] == indexed['active_version']
    assert task['outputs']['model_usage']['input_tokens'] == 50
    empty = settled(client, base, start(client, base, 'no-sources', inputs={'query':'wheels'}))
    assert empty['status'] == 'succeeded' and empty['outputs']['knowledge']['retrieved_count'] == 0
    assert '没有检索到' in empty['outputs']['markdown'] and len(requests) == 1
    status = 402
    failed = settled(client, base, start(client, base, 'provider-failed', inputs={'query':'feline'}))
    assert failed['status'] == 'failed' and '402' in failed['error'] and len(requests) == 2
    assert not failed['outputs']  # No invented answer or automatic fallback after model failure.
    assert client.get(base + '/tasks/' + task['id']).json()['outputs'] == task['outputs']


def test_project_knowledge_skill_and_tools_share_existing_index(knowledge):
    client, app, project, settings, base, service, calls = knowledge
    indexed = setup_index(knowledge)
    summaries = client.get(base + '/skills').json()
    assert next(s for s in summaries if s['id'] == 'knowledge')['description']
    assert all('content' not in s for s in summaries)
    skill = client.post(base + '/agent-tools', json={'name':'project_skills','arguments':{'action':'read','skill_id':'knowledge'}})
    assert skill.status_code == 200 and 'project_knowledge' in skill.json()['content']
    listing = client.post(base + '/agent-tools', json={'name':'project_knowledge','arguments':{'action':'list'}})
    assert listing.status_code == 200 and listing.json()[0]['knowledge_ref'] == 'manual'
    search = client.post(base + '/agent-tools', json={'name':'project_knowledge','arguments':{'action':'search','knowledge_ref':'manual','query':'feline'}})
    assert search.status_code == 200 and search.json()['version'] == indexed['active_version']
    assert search.json()['results'][0]['citation'] == '[1]'
