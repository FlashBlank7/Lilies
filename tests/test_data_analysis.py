"""The same real operations can run as agent tools and editable workflow nodes."""
from pathlib import Path

from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_projects import configured, graph, node, edge, ref, start  # noqa: F401


def register(config):
    client, app, project, settings = config
    path = settings.workspace_root / project['id'] / 'requirement-package/data.csv'
    path.parent.mkdir(exist_ok=True)
    path.write_text('id,x,y\n001,1,4\n002,2,3\n003,3,2\n004,4,1\n005,100,0\n')
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/datasets', json={'source_path': 'requirement-package/data.csv',
                           'mapping': {'id_column': 'id', 'target': 'y'}})
    assert response.status_code == 201, response.text
    return base, response.json(), path


def test_tool_clean_then_workflow_spearman_and_original_preserved(real_compute):
    config, service = real_compute
    client, app, project, settings = config
    base, dataset, path = register(config)
    original = path.read_bytes()
    result = client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': {
        'action': 'analyze', 'dataset_id': dataset['id'],
        'analysis': {'operation': 'outliers', 'columns': ['x'], 'handling': 'drop'}}})
    assert result.status_code == 200, result.text
    clean = result.json()
    assert clean['dataset_id'] != dataset['id'] and clean['source_dataset_id'] == dataset['id']
    assert clean['output_rows'] == 4 and clean['affected_rows'] == 1
    assert path.read_bytes() == original
    derived = client.get(base + '/datasets/' + clean['dataset_id']).json()
    assert derived['derived_from'] == dataset['id']
    assert derived['mapping'] == dataset['mapping']
    assert all((settings.workspace_root / project['id'] / a['file_path']).is_file() for a in clean['artifacts'])
    graph(client, project['id'], [node('start', 'start'),
        node('stats', 'data_analysis', dataset_id=clean['dataset_id'],
             analysis={'operation': 'spearman', 'columns': ['x', 'y']}),
        node('end', 'end', outputs={'result': ref('stats', 'output')})],
        [edge('start', 'stats'), edge('stats', 'end')])
    task = wait_task(client, base, start(client, base, 'spearman', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    assert task['outputs']['result']['pairs'][0]['rho'] == -1
    assert task['outputs']['result']['pairs'][0]['samples'] == 4
    assert Path(service.path(project['id'], dataset['id']) / dataset['files']['source']['name']).read_bytes() == original


def test_workflow_clean_output_can_feed_next_operation(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base, dataset, _ = register(config)
    graph(client, project['id'], [node('start', 'start'),
        node('clean', 'data_analysis', dataset_id=dataset['id'],
             analysis={'operation': 'outliers', 'columns': ['x'], 'handling': 'drop'}),
        node('stats', 'data_analysis', dataset_id=ref('clean', 'output', 'dataset_id'),
             analysis={'operation': 'spearman', 'columns': ['x', 'y']}),
        node('end', 'end', outputs={'result': ref('stats', 'output')})],
        [edge('start', 'clean'), edge('clean', 'stats'), edge('stats', 'end')])
    task = wait_task(client, base, start(client, base, 'pipeline', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    assert task['outputs']['result']['pairs'][0]['samples'] == 4


def test_incomplete_analysis_draft_saves_and_runtime_reports_missing_columns(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base, dataset, _ = register(config)
    graph(client, project['id'], [node('start', 'start'),
        node('stats', 'data_analysis', dataset_id=dataset['id'], analysis={'operation': 'spearman'}),
        node('end', 'end')], [edge('start', 'stats'), edge('stats', 'end')])
    task = wait_task(client, base, start(client, base, 'incomplete', workflow_id=project['id']))
    assert task['status'] == 'failed', task
    assert '至少 2' in str(task)


def test_fixed_business_bounds_feed_actual_training_without_manual_dataset_ids(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base = '/api/v1/projects/' + project['id']
    path = settings.workspace_root / project['id'] / 'requirement-package/data.csv'
    path.parent.mkdir(exist_ok=True)
    path.write_text('id,x,y\n' + '\n'.join(f'{i:03d},{i if i < 59 else 999},{i * 2}' for i in range(60)))
    graph(client, project['id'], [node('start', 'start'),
        node('clean', 'data_analysis', source_path='requirement-package/data.csv', mapping={'id_column': 'id', 'target': 'y'},
             analysis={'operation': 'outliers', 'columns': ['x'], 'method': 'bounds', 'upper': 100, 'handling': 'drop'}),
        node('train', 'model_train', dataset_id=ref('clean', 'output', 'dataset_id'),
             evaluation={'folds': 2}, candidate={'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1},
             budget={'seconds': 180, 'trials': 1, 'trial_seconds': 45}),
        node('end', 'end', outputs={'result': ref('train', 'output'), 'clean': ref('clean', 'output')})],
        [edge('start', 'clean'), edge('clean', 'train'), edge('train', 'end')])
    task = wait_task(client, base, start(client, base, 'clean-train', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    assert task['outputs']['clean']['output_rows'] == 59
    trained = task['outputs']['result']
    assert trained['trials'][0]['status'] == 'completed'
    assert trained['trials'][0]['metrics']['mae'] < 2
    study = client.get(base + '/modeling/studies/' + trained['study_id']).json()
    assert study['dataset_id'] == task['outputs']['clean']['dataset_id']
    assert 'test_result' not in study


def test_cross_project_dataset_rejected_before_compute(modeling, monkeypatch):
    config, service = modeling
    client, app, project, settings = config
    base, dataset, _ = register(config)
    other = client.post('/api/v1/projects', json={'name': 'other'}).json()['id']
    async def unexpected(*args, **kwargs):
        raise AssertionError('cross-project computation must not start')
    monkeypatch.setattr(service, 'compute', unexpected)
    response = client.post('/api/v1/projects/' + other + '/agent-tools', json={
        'name': 'project_modeling', 'arguments': {'action': 'analyze', 'dataset_id': dataset['id'],
            'analysis': {'operation': 'spearman', 'columns': ['x', 'y']}}})
    assert response.status_code == 404, response.text


def test_numeric_tool_exports_new_layout_and_workflow_can_analyze_it(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base = '/api/v1/projects/' + project['id']
    path = settings.workspace_root / project['id'] / 'requirement-package/mixed.csv'
    path.parent.mkdir(exist_ok=True)
    original = 'Title,,\nid,x,y\nunit,micron,kg\n001,1-3,8\n002,4,6\n003,6,4\n004,/,2\n'
    path.write_text(original)
    dataset = client.post(base + '/datasets', json={'source_path': 'requirement-package/mixed.csv',
        'mapping': {'id_column': 'id', 'header_row': 1, 'skip_after_header': 1}}).json()
    response = client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': {
        'action': 'analyze', 'dataset_id': dataset['id'], 'analysis': {'operation': 'numeric', 'columns': ['x'],
        'numeric': {'missing_tokens': ['/'], 'intervals': 'midpoint'}}}})
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared['rows'] == prepared['output_rows'] == 4
    assert prepared['missing_counts'] == {'x': 1}
    derived = client.get(base + '/datasets/' + prepared['dataset_id']).json()
    assert derived['mapping']['header_row'] == derived['mapping']['skip_after_header'] == 0
    assert derived['mapping']['id_column'] == 'id'
    assert path.read_text() == original
    graph(client, project['id'], [node('start', 'start'),
        node('stats', 'data_analysis', dataset_id=prepared['dataset_id'], analysis={'operation': 'spearman', 'columns': ['x', 'y']}),
        node('end', 'end', outputs={'result': ref('stats', 'output')})], [edge('start', 'stats'), edge('stats', 'end')])
    task = wait_task(client, base, start(client, base, 'numeric-correlation', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    assert task['outputs']['result']['pairs'][0]['rho'] == -1
    assert task['outputs']['result']['pairs'][0]['samples'] == 3


def test_workflow_numeric_features_train_and_predict_new_file_layout(real_compute):
    import io
    import json
    import zipfile
    config, _ = real_compute
    client, app, project, settings = config
    base = '/api/v1/projects/' + project['id']
    path = settings.workspace_root / project['id'] / 'requirement-package/train.csv'
    path.parent.mkdir(exist_ok=True)
    path.write_text('Production table,,\nid,x,y\nunit,micron,kg\n' +
                    '\n'.join(f'{i:03d},{i-1}-{i+1},{i*2}' for i in range(60)))
    dataset = client.post(base + '/datasets', json={'source_path': 'requirement-package/train.csv',
        'mapping': {'target': 'y', 'id_column': 'id', 'header_row': 1, 'skip_after_header': 1}}).json()
    numeric = {'columns': ['x'], 'missing_tokens': ['/'], 'intervals': 'midpoint'}
    graph(client, project['id'], [node('start', 'start'),
        node('features', 'feature_extract', dataset_id=dataset['id'], features={'columns': ['x'], 'numeric': numeric}),
        node('train', 'model_train', dataset_id=ref('features', 'output', 'dataset_id'), features=ref('features', 'output', 'feature_plan'),
             evaluation={'folds': 2}, candidate={'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1},
             budget={'seconds': 180, 'trials': 1, 'trial_seconds': 45}),
        node('end', 'end', outputs={'result': ref('train', 'output')})],
        [edge('start', 'features'), edge('features', 'train'), edge('train', 'end')])
    task = wait_task(client, base, start(client, base, 'numeric-train', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    trained = task['outputs']['result']
    assert trained['trials'][0]['metrics']['mae'] < 2
    candidate_path = base + '/modeling/studies/' + trained['study_id'] + '/candidates/' + trained['id']
    with zipfile.ZipFile(io.BytesIO(client.get(candidate_path + '/download').content)) as archive:
        assert json.loads(archive.read('predict.example.json'))['features']['numeric'] == numeric
    new = client.post(base + '/datasets/upload', files={'file': ('new.csv', b'id,x\n001,9-11\n002,10\n003,/\n', 'text/csv')}).json()
    graph(client, project['id'], [node('start', 'start'),
        node('predict', 'model_predict', dataset_id=new['id'], study_id=trained['study_id'], candidate_id=trained['id']),
        node('end', 'end', outputs={'result': ref('predict', 'output')})], [edge('start', 'predict'), edge('predict', 'end')])
    task = wait_task(client, base, start(client, base, 'numeric-predict', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    result = task['outputs']['result']
    assert result['rows'] == 3
    assert result['preview'][0]['prediction'] == result['preview'][1]['prediction']
    assert result['preview'][0]['id'] == '001'


def test_existing_candidate_without_new_default_fields_still_accepts_same_features(real_compute):
    from tests.test_modeling import setup
    config, service = real_compute
    client, app, project, settings = config
    base, dataset, study, candidate = setup(client, project, settings, batch_size=1)
    candidate['features'].pop('numeric')
    client.portal.call(service.put, project['id'], 'candidate', candidate)
    graph(client, project['id'], [node('start', 'start'),
        node('train', 'model_train', dataset_id=dataset['id'], study_id=study['id'], candidate_id=candidate['id'],
             features={'numeric': {'columns': []}}),
        node('end', 'end', outputs={'result': ref('train', 'output')})], [edge('start', 'train'), edge('train', 'end')])
    task = wait_task(client, base, start(client, base, 'legacy-features', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    assert task['outputs']['result']['trials'][0]['status'] == 'completed'
