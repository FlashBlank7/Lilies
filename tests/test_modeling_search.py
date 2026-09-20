"""AIDE branch selection consumes actual platform results and survives reloads."""
import asyncio
import copy
import json

import pytest

from agent_platform.modeling_models import AideSearch
from agent_platform.modeling_search import choose_step
from tests.test_modeling import modeling, real_compute, setup, wait_task
from tests.test_projects import configured, graph, node, edge, ref, start


def study(metric='mae', **search):
    return {'evaluation': {'metric': metric, 'seed': 42}, 'aide': AideSearch(**search).model_dump()}


def result(ident, score, parent='', metric='mae'):
    return {'id': ident, 'parent_id': parent, 'created_at': ident, 'hypothesis': ident,
            'status': 'completed', 'trials': [{'status': 'completed', 'metrics': {metric: score}}]
            if score is not None else [{'status': 'failed', 'error': 'invalid parameter'}]}


def test_upstream_drafts_then_selects_actual_best_in_both_metric_directions():
    candidates = [result('a', .8), result('b', .2)]
    assert choose_step(study(num_drafts=3), candidates)['stage'] == 'draft'
    selection = choose_step(study(num_drafts=2), candidates)
    assert (selection['stage'], selection['parent_id']) == ('improve', 'b')
    # A reply does not accept model-authored metrics; only completed trials rank.
    candidates[0]['score'] = -100
    candidates.append(result('c', float('nan')))
    assert choose_step(study(num_drafts=2, debug_prob=0), candidates)['parent_id'] == 'b'
    classification = [result('a', .8, metric='macro_f1'), result('b', .2, metric='macro_f1')]
    assert choose_step(study('macro_f1', num_drafts=2), classification)['parent_id'] == 'a'


def test_failed_leaf_repair_depth_and_refresh_do_not_change_branch():
    candidates = [result('a', None), result('b', .4)]
    config = study(num_drafts=2, debug_prob=1, max_debug_depth=0)
    before = copy.deepcopy(candidates)
    selection = choose_step(config, candidates)
    assert (selection['stage'], selection['parent_id']) == ('debug', 'a')
    assert choose_step(config, json.loads(json.dumps(candidates))) == selection
    assert candidates == before
    # Repaired parent is no longer a leaf; child exceeds the debug-depth limit.
    candidates.append(result('c', None, parent='a'))
    selection = choose_step(config, candidates)
    assert (selection['stage'], selection['parent_id']) == ('improve', 'b')


def test_aide_actual_workflow_repairs_and_records_selection(real_compute):
    (client, app, project, settings), service = real_compute
    base, dataset, original, _ = setup(client, project, settings)
    request = {'dataset_id': dataset['id'], 'request_key': 'aide', 'search_strategy': 'aide',
               'aide': {'num_drafts': 1, 'debug_prob': 1},
               'budget': {'seconds': 180, 'trials': 4, 'trial_seconds': 45}}
    response = client.post(base + '/modeling/studies', json=request)
    assert response.status_code == 201, response.text
    s = response.json(); path = base + '/modeling/studies/' + s['id']
    first = client.get(path + '/next-step').json()
    assert first['stage'] == 'draft'
    assert client.get(path + '/next-step').json() == first
    other = client.post('/api/v1/projects', json={'name': 'different project'}).json()['id']
    assert client.get(f'/api/v1/projects/{other}/modeling/studies/{s["id"]}/next-step').status_code == 404
    body = {'request_key': 'bug', 'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1, 'parameters': {'does_not_exist': 1}}
    response = client.post(path + '/candidates', json={**body, 'batch_size': 2})
    assert response.status_code == 422
    c = client.post(path + '/candidates', json=body).json()
    assert c['search_decision']['stage'] == 'draft'
    assert client.get(path + '/next-step').json()['candidate_id'] == c['id']
    assert client.post(path + '/candidates', json={**body, 'request_key': 'premature'}).status_code == 409
    assert client.post(path + '/candidates', json=body).json()['id'] == c['id']

    def run(candidate, key):
        graph(client, project['id'], [node('start', 'start', inputs=[]),
              node('train', 'model_train', study_id=s['id'], candidate_id=candidate['id']),
              node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])
        task = start(client, base, key)
        assert wait_task(client, base, task)['status'] == 'succeeded'
        return client.get(path + '/candidates/' + candidate['id']).json()

    broken = run(c, 'broken')
    assert broken['trials'][0]['status'] == 'failed'
    selection = client.get(path + '/next-step').json()
    assert (selection['stage'], selection['parent_id']) == ('debug', c['id'])
    assert client.post(path + '/candidates', json={**body, 'request_key': 'wrong-parent', 'parameters': {}}).status_code == 409
    fixed = client.post(path + '/candidates', json={**body, 'request_key': 'fixed', 'parent_id': c['id'], 'parameters': {}}).json()
    fixed = run(fixed, 'fixed')
    assert fixed['trials'][0]['status'] == 'completed'
    selection = client.get(path + '/next-step').json()
    assert (selection['stage'], selection['parent_id']) == ('improve', fixed['id'])
    assert selection['parent_score'] == fixed['trials'][0]['metrics']['mae']
    note = client.get(path + f'/candidates/{fixed["id"]}/trials/0/note').json()
    assert note['note']['search_decision']['stage'] == 'debug'
    assert '修复失败方案' in note['markdown'] and '搜索策略' in note['markdown']
    # An actual service restart pauses this study; a budget resume preserves
    # completed trials and yields the same branch without another training run.
    asyncio.run(service.close()); asyncio.run(service.initialize())
    assert client.get(path + '/next-step').json()['stage'] == 'stop'
    assert client.patch(path + '/budget', json=s['budget']).status_code == 200
    assert client.get(path + '/next-step').json() == selection
    assert client.get(path).json()['trials_used'] == 2
    assert client.post(path + '/finish', json={}).status_code == 200
    assert client.get(path + '/next-step').json()['stage'] == 'stop'


def test_study_request_before_strategy_fields_remains_idempotent(modeling):
    (client, app, project, settings), service = modeling
    base, _, s, _ = setup(client, project, settings)
    async def legacy():
        doc = await service.get(project['id'], 'study', s['id'])
        doc['request'].pop('search_strategy'); doc['request'].pop('aide')
        await service.put(project['id'], 'study', doc)
    asyncio.run(legacy())
    assert client.post(base + '/modeling/studies', json=s['request']).json()['id'] == s['id']
