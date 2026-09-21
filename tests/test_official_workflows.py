"""Employees run editable recipes without internal experiment identifiers."""
import asyncio
from copy import deepcopy

import pytest
from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_projects import configured, graph, start  # noqa: F401
from agent_platform.official_workflows import CATALOG, RULE_CODE, REPLAY_INPUT_CODE


def install(client, base, key='tabular-classification'):
    result = client.post(base + '/space/official-workflows/' + key)
    assert result.status_code == 201, result.text
    return result.json()['workflow_id']


def test_install_editable_copies_and_skill_without_resources(configured):
    client, app, project, _ = configured
    base = '/api/v1/projects/' + project['id']
    items = client.get(base + '/space/official-workflows').json()
    for item in items:
        first, second = install(client, base, item['id']), install(client, base, item['id'])
        assert first != second
        draft = client.get('/api/v1/applications/' + first + '/draft').json()['snapshot']['workflow']
        assert len(draft['nodes']) >= 3
        assert client.get(base + '/skills/official-' + first).json()['content'].find(first) >= 0
    assert client.get(base + '/tasks').json() == []
    assert client.post(base + '/space/official-workflows/missing').status_code == 404


def test_rule_decisions_require_actual_model_probabilities(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / 'results/predictions.csv'; p.parent.mkdir()
    p.write_text('prediction,probability_good\ngood,0.95\ngood,0.6\n')
    namespace = {}; exec(RULE_CODE, namespace)
    args = {'threshold': .9, 'prediction': {'project_path': 'results/predictions.csv',
            'model_version': {'candidate_id': 'real-version'}, 'probability_columns': {'good': 'probability_good'}}}
    result = namespace['main'](args)
    assert (result['rows'], result['inherit'], result['measure']) == (2, 1, 1)
    assert (tmp_path / result['file']).exists()
    loader = {}; exec(REPLAY_INPUT_CODE, loader)
    saved = loader['main']({'source_path':result['prediction_input']})
    recomputed = namespace['main']({'prediction':saved,'threshold':.5})
    assert recomputed['inherit']==2 and recomputed['model_version']==result['model_version']
    with pytest.raises(ValueError, match='模型版本'):
        namespace['main']({'threshold': .9, 'prediction': {}})
    with pytest.raises(ValueError, match='阈值'):
        namespace['main']({**args, 'threshold': float('nan')})
    selected = {**args['prediction'], 'acceptance': {'status':'selected','threshold':.8}}
    assert namespace['main']({'prediction': selected})['inherit'] == 1
    unavailable = {**selected, 'acceptance': {'status':'unavailable','threshold':None}}
    assert namespace['main']({'prediction': unavailable})['measure'] == 2
    p.write_text('changed')
    with pytest.raises(ValueError, match='内容改变'):
        loader['main']({'source_path':result['prediction_input']})


def test_file_snapshot_retries_and_new_runs_are_independent(modeling, monkeypatch):
    (client, app, project, settings), service = modeling
    pid = project['id']
    source = settings.workspace_root / pid / 'requirement-package/table.csv'
    source.parent.mkdir(exist_ok=True); source.write_text('x,y\n1,2\n')
    async def profile(project_id, dataset_id):
        return {'rows': 1}
    monkeypatch.setattr(service, 'profile', profile)
    args = {'source_path': 'requirement-package/table.csv', 'mapping': {'target': 'y'}}
    async def run():
        old = await service.run_block({'project_id': pid, 'task_id': 'first'}, 'data_analysis', args, 'r1', 'profile')
        source.write_text('x,y\n3,4\n')
        retry = await service.run_block({'project_id': pid, 'task_id': 'first'}, 'data_analysis', args, 'r2', 'profile')
        new = await service.run_block({'project_id': pid, 'task_id': 'second'}, 'data_analysis', args, 'r3', 'profile')
        assert retry['dataset_id'] == old['dataset_id'] != new['dataset_id']
        assert (service.path(pid, old['dataset_id']) / 'source.csv').read_text() == 'x,y\n1,2\n'
        assert (service.path(pid, new['dataset_id']) / 'source.csv').read_text() == 'x,y\n3,4\n'
    asyncio.run(run())


def test_real_file_training_holdout_prediction_and_changed_input(real_compute):
    (client, app, project, settings), service = real_compute
    pid = project['id']; base = '/api/v1/projects/' + pid
    wid = install(client, base)
    recipe = deepcopy(CATALOG['tabular-classification']['workflow'])
    next(n for n in recipe['nodes'] if n['id']=='train')['config']['evaluation'].update(acceptance_accuracy=.8, acceptance_min_samples=5)
    graph(client, wid, **recipe)
    data = 'x,category,target\n' + '\n'.join(f'{i},{"a" if i%2 else "b"},{int(i%10 >= 5)}' for i in range(90)) + '\n91,a,'
    upload = client.post(base + '/materials', files={'file': ('train.csv', data.encode(), 'text/csv')}).json()
    first = wait_task(client, base, start(client, base, 'official-1', workflow_id=wid, inputs={'source_path': upload['path'], 'target': 'target', 'group_column': ''}))
    assert first['status'] == 'succeeded', first.get('error')
    result = first['outputs']; candidate = result['training']
    prepared = result['features']
    assert prepared['stage'] == 'before_fold_preprocessing'
    assert prepared['source_rows'] == 91 and prepared['rows'] == 90
    assert prepared['excluded'] == [{'reason': '目标标签缺失', 'rows': 1}]
    assert len(prepared['preview']) == 12
    assert {a['file_path'].rsplit('/', 1)[1] for a in prepared['artifacts']} == {'features.csv', 'samples.csv', 'feature-summary.json'}
    import csv, io
    tables = {}
    for artifact in prepared['artifacts']:
        response = client.get('/api/v1/applications/' + pid + '/workspace/files/' + artifact['file_path'])
        assert response.status_code == 200
        if artifact['file_path'].endswith('.csv'):
            tables[artifact['file_path'].rsplit('/', 1)[1]] = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(tables['features.csv']) == len(tables['samples.csv']) == 90
    assert 'target' not in tables['features.csv'][0] and 'target' in tables['samples.csv'][0]
    assert len(candidate['trials']) == 3
    assert result['test']['rows'] == 18
    assert result['test']['acceptance']['selection']['validation_rows'] == 72
    assert result['test']['acceptance']['test']['rows'] == 18
    assert all('macro_f1' in t['metrics'] for t in candidate['trials'])
    # A new draft can change its report without launching another training job.
    recipe['nodes'][-1]['config']['outputs']['note'] = 'Updated report'
    graph(client, wid, **recipe)
    reused = wait_task(client, base, start(client, base, 'report-only', workflow_id=wid,
        reuse_task_id=first['id'], inputs=first['inputs']))
    assert reused['status'] == 'succeeded', reused.get('error')
    assert reused['outputs']['training']['id'] == candidate['id']
    assert reused['outputs']['note'] == 'Updated report'
    assert {'start', 'profile', 'features', 'train', 'test'} <= set(reused['runs'][0]['reuse']['nodes'])
    assert 'end' not in reused['runs'][0]['reuse']['nodes']
    next(n for n in recipe['nodes'] if n['id']=='train')['config']['evaluation']['acceptance_accuracy'] = .85
    graph(client, wid, **recipe)
    retrained = wait_task(client, base, start(client, base, 'changed-training', workflow_id=wid,
        reuse_task_id=reused['id'], inputs=first['inputs']))
    assert retrained['status'] == 'succeeded', retrained.get('error')
    assert retrained['outputs']['training']['id'] != candidate['id']
    assert set(retrained['runs'][0]['reuse']['nodes']) == {'start', 'profile', 'features'}
    trial = next(t for t in candidate['trials'] if t['status'] == 'completed')
    bind = client.put(base + '/models/quality', json={'name': '质量', 'study_id': candidate['study_id'], 'candidate_id': candidate['id'], 'slot': trial['slot']})
    assert bind.status_code == 200, bind.text
    predict_id = install(client, base, 'batch-prediction')
    recipe = deepcopy(CATALOG['batch-prediction']['workflow']); recipe['nodes'][1]['config']['model_ref'] = 'quality'
    graph(client, predict_id, **recipe)
    fresh = client.post(base + '/materials', files={'file': ('new.csv', b'x,category\n2,never-seen\n8,a\n', 'text/csv')}).json()
    prediction = wait_task(client, base, start(client, base, 'predict', workflow_id=predict_id, inputs={'source_path': fresh['path']}))
    assert prediction['status'] == 'succeeded', prediction.get('error')
    output = prediction['outputs']['result']
    assert output['rows'] == 2 and output['probability_columns']
    assert output['acceptance']['rows'] == 2
    assert all(row['decision'] in {'accept_prediction','review'} for row in output['preview'])
    assert client.get(base + '/' + output['artifact']).status_code == 200
    assert (settings.workspace_root / pid / output['project_path']).is_file()
    registered = client.post(base + '/datasets', json={'source_path': fresh['path']}).json()
    independent = client.post(base + '/models/quality/predict', json={'dataset_id': registered['id'], 'request_key': 'independent'})
    assert independent.status_code == 202, independent.text
    independent = wait_task(client, base, independent.json())
    assert independent['status'] == 'succeeded', independent['error']
    assert independent['outputs']['preview'] == output['preview']
    assert independent['outputs']['model_version'] == output['model_version']
    rules_id = install(client, base, 'model-rules-prediction')
    rules = deepcopy(CATALOG['model-rules-prediction']['workflow'])
    next(n for n in rules['nodes'] if n['id']=='predict')['config']['model_ref'] = 'quality'
    graph(client, rules_id, **rules)
    decided = wait_task(client, base, start(client, base, 'saved-threshold', workflow_id=rules_id, inputs={'source_path':fresh['path']}))
    assert decided['status'] == 'succeeded', decided.get('error')
    assert decided['outputs']['result']['rows'] == 2
    replay_id = install(client, base, 'prediction-rules-replay')
    replayed = wait_task(client, base, start(client, base, 'rule-replay', workflow_id=replay_id,
        inputs={'source_path':decided['outputs']['result']['prediction_input'],'threshold':1.0}))
    assert replayed['status'] == 'succeeded', replayed.get('error')
    assert replayed['outputs']['result']['model_version'] == decided['outputs']['result']['model_version']
    assert replayed['outputs']['result']['threshold_source'] == 'manual'
    assert replayed['outputs']['result']['inherit'] <= decided['outputs']['result']['inherit']
    # Same filename but changed content is a new immutable input and experiment.
    changed_data = 'x,category,target\n' + '\n'.join(f'{i},{"a" if i%2 else "b"},{int(i%10 < 5)}' for i in range(90))
    changed = client.post(base + '/materials', files={'file': ('train.csv', changed_data.encode(), 'text/csv')}).json()
    second = wait_task(client, base, start(client, base, 'official-2', workflow_id=wid, inputs={'source_path': changed['path'], 'target': 'missing', 'group_column': ''}))
    assert second['status'] == 'failed'
    assert 'missing' in second['error'] or '目标' in second['error']
    old = client.get(base + '/tasks/' + first['id']).json()
    assert old['outputs']['training']['id'] == candidate['id']
    repaired = wait_task(client, base, start(client, base, 'official-repaired', workflow_id=wid,
        inputs={'source_path': changed['path'], 'target': 'target', 'group_column': ''}))
    assert repaired['status'] == 'succeeded', repaired.get('error')
    assert repaired['outputs']['training']['id'] != candidate['id']
    assert repaired['outputs']['data']['dataset_id'] != result['data']['dataset_id']


@pytest.mark.parametrize('template', ['tabular-regression', 'process-regression'])
def test_real_regression_recipes(real_compute, template):
    (client, app, project, settings), service = real_compute
    base = '/api/v1/projects/' + project['id']
    workflow = install(client, base, template)
    process = template == 'process-regression'
    data = ('batch,t,temperature\n' + '\n'.join(f'{i},2026-01-01T00:00:0{j},{i+j/10}' for i in range(24) for j in range(3))
            if process else 'x,y\n' + '\n'.join(f'{i},{i*2+1}' for i in range(60)))
    uploaded = client.post(base + '/materials', files={'file': ('data.csv', data.encode(), 'text/csv')}).json()
    inputs = {'source_path': uploaded['path'], 'target': 'y', 'group_column': 'batch' if process else ''}
    if process:
        labels = 'batch,at,y\n' + '\n'.join(f'{i},2026-01-01T00:00:03,{i*2+1}' for i in range(24))
        uploaded_labels = client.post(base + '/materials', files={'file': ('labels.csv', labels.encode(), 'text/csv')}).json()
        inputs.update(labels_path=uploaded_labels['path'], id_column='batch', time_column='t', prediction_time_column='at')
    task = wait_task(client, base, start(client, base, template, workflow_id=workflow, inputs=inputs))
    assert task['status'] == 'succeeded', task.get('error')
    assert task['outputs']['test']['rows'] > 0
    import csv, io
    feature_result = task['outputs']['features']
    path = next(a['file_path'] for a in feature_result['artifacts'] if a['file_path'].endswith('/features.csv'))
    response = client.get('/api/v1/applications/' + project['id'] + '/workspace/files/' + path)
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows) == (24 if process else 60)
    assert 'y' not in rows[0]
    if process:
        assert all(float(row['temperature__mean']) == pytest.approx(i + .1) for i, row in enumerate(rows))
    else:
        assert [float(row['x']) for row in rows] == list(range(60))
    assert any(trial['metrics']['mae'] < trial['baseline']['mae'] for trial in task['outputs']['training']['trials'] if trial['status'] == 'completed')
