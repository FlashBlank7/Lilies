"""The same real operations can run as agent tools and editable workflow nodes."""
from pathlib import Path
import json

from tests.test_modeling import modeling, real_compute, wait_task, setup  # noqa: F401
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


def test_semantic_rules_survive_tool_derivation_training_and_workflow_replacement(real_compute):
    config, service = real_compute
    client, app, project, settings = config
    root = settings.workspace_root / project['id']
    path = root / 'requirement-package/data.csv'; path.parent.mkdir(exist_ok=True)
    values = [str(i) for i in range(60)]
    values[3:6] = ['3.5-4.2', '/', '6..2']
    def source(values):
        return 'id,x,pressure,y,unused\n' + '\n'.join(f'{i:03d},{x},{i % 13},{i * 2},unknown' for i, x in enumerate(values))
    path.write_text(source(values))
    original = path.read_bytes()
    mapping = {'id_column': 'id', 'target': 'y', 'invalid_rows': 'exclude',
               'field_rules': {'x': {'pending_tokens': ['/']}, 'pressure': {'dtype': 'number'}, 'unused': {'dtype': 'text', 'meaning': 'pending'}}}
    base = '/api/v1/projects/' + project['id']
    graph(client, project['id'], [node('start', 'start'),
        node('data', 'data_analysis', source_path='requirement-package/data.csv', mapping=mapping,
             analysis={'operation': 'numeric', 'columns': ['x']}),
        node('train', 'model_train', dataset_id=ref('data', 'output', 'dataset_id'),
             evaluation={'folds': 2}, features={'columns': ['x', 'pressure']},
             candidate={'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1},
             budget={'seconds': 180, 'trials': 1, 'trial_seconds': 45}),
        node('end', 'end', outputs={'training': ref('train', 'output'), 'data': ref('data', 'output')})],
        [edge('start', 'data'), edge('data', 'train'), edge('train', 'end')])
    task = wait_task(client, base, start(client, base, 'semantic-training', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    trained = task['outputs']['training']
    derived = client.get(base + '/datasets/' + task['outputs']['data']['dataset_id']).json()
    assert 'state' in derived['files']
    repeated = client.post(base + '/datasets', json={'source_path': derived['files']['source']['original'], 'mapping': derived['mapping']})
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()['files']['state']['sha256'] == derived['files']['state']['sha256']
    split = json.loads((service.path(project['id'], trained['study_id']) / 'output/split.json').read_text())
    assert split['excluded_data_rows'] == 3 and len(split['samples']) == 57
    response = client.put(base + '/models/semantic-model', json={'name': '测试模型', 'study_id': trained['study_id'],
        'candidate_id': trained['id'], 'slot': 0})
    assert response.status_code == 200, response.text
    import io, zipfile
    bundle = client.get(base + f'/modeling/studies/{trained["study_id"]}/candidates/{trained["id"]}/download')
    assert bundle.status_code == 200, bundle.text[:200] if bundle.status_code != 200 else ''
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        example = json.loads(archive.read('predict.example.json'))
        assert example['mapping']['field_rules'] == derived['mapping']['field_rules']
        assert example['mapping']['field_rules']['x']['intervals'] == 'pending'
    graph(client, project['id'], [node('start', 'start', inputs=[{'name': 'source_path', 'type': 'string', 'required': True}]),
        node('data', 'data_analysis', source_path=ref('$inputs', 'source_path'), mapping=mapping),
        node('stats', 'data_analysis', dataset_id=ref('data', 'output', 'dataset_id'), analysis={'operation': 'spearman', 'columns': ['x', 'pressure']}),
        node('predict', 'model_predict', dataset_id=ref('data', 'output', 'dataset_id'), model_ref='semantic-model'),
        node('end', 'end', outputs={'stats': ref('stats', 'output'), 'predict': ref('predict', 'output')})],
        [edge('start', 'data'), edge('data', 'stats'), edge('stats', 'predict'), edge('predict', 'end')])
    revision = client.get('/api/v1/applications/' + project['id'] + '/draft').json()['revision']
    outputs = []
    for number in range(2):
        if number:
            values[7] = 'new-marker'; values[8] = '999'
            (root / 'requirement-package/changed.csv').write_text(source(values))
        result = wait_task(client, base, start(client, base, f'semantic-predict-{number}', workflow_id=project['id'],
            inputs={'source_path': 'requirement-package/changed.csv' if number else 'requirement-package/data.csv'}))
        assert result['status'] == 'succeeded', result
        outputs.append(result['outputs'])
        prediction = result['outputs']['predict']
        assert prediction['rows'] == 60 and prediction['unavailable_rows'] == 3 + number
        state = json.loads((root / prediction['data_state_path']).read_text())
        interval = next(r for r in state['cells'] if r['raw'] == '3.5-4.2')
        assert interval['status'] == 'pending' and interval['source_row'] == 3
        assert result['outputs']['stats']['pairs'][0]['samples'] == 57 - number
    assert outputs[0]['stats']['pairs'][0]['rho'] != outputs[1]['stats']['pairs'][0]['rho']
    assert client.get('/api/v1/applications/' + project['id'] + '/draft').json()['revision'] == revision
    assert path.read_bytes() == original


def test_skill_analysis_workflow_saves_before_data_and_reuses_new_input(real_compute):
    config, service = real_compute
    client, app, project, settings = config
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/agent-tools', json={'name': 'project_skills', 'arguments': {
        'action': 'read', 'skill_id': 'modeling', 'reference': 'analysis-workflow.json'}})
    assert response.status_code == 200, response.text
    example = json.loads(response.json()['content'])
    # No dataset, model or file is required to save an editable draft.
    graph(client, project['id'], example['nodes'], example['edges'])
    saved = client.get('/api/v1/applications/' + project['id'] + '/draft').json()
    root = settings.workspace_root / project['id']
    (root / 'requirement-package').mkdir(exist_ok=True)
    for name, text, count, rho in [('requirement-package/first.csv', 'pressure,height\n1,4\n2,3\n3,2\n100,1\n', 4, -1),
                                    ('requirement-package/second.csv', 'pressure,height\n1,1\n2,2\n3,3\n', 3, 1)]:
        (root / name).write_text(text)
        task = wait_task(client, base, start(client, base, name, workflow_id=project['id'], inputs={
            'source_path': name, 'first_column': 'pressure', 'second_column': 'height'}))
        assert task['status'] == 'succeeded', task
        outputs = task['outputs']
        assert outputs['数据概况']['rows'] == count
        assert outputs['异常标记']['output_rows'] == count
        assert outputs['相关分析']['pairs'][0]['samples'] == count
        assert outputs['相关分析']['pairs'][0]['rho'] == rho
        for artifact in outputs['相关分析']['artifacts'] + outputs['异常标记']['artifacts']:
            assert (root / artifact['file_path']).is_file()
        assert (root / name).read_text() == text
    assert client.get('/api/v1/applications/' + project['id'] + '/draft').json() == saved


def test_referenced_analysis_settings_are_validated_after_resolution(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base, data, _ = register(config)
    graph(client, project['id'], [node('start', 'start', inputs=[{'name': 'analysis', 'type': 'object', 'required': True}]),
        node('stats', 'data_analysis', dataset_id=data['id'], analysis=ref('$inputs', 'analysis')),
        node('end', 'end', outputs={'result': ref('stats', 'output')})], [edge('start', 'stats'), edge('stats', 'end')])
    valid = wait_task(client, base, start(client, base, 'valid-analysis', workflow_id=project['id'], inputs={
        'analysis': {'operation': 'spearman', 'columns': ['x', 'y']}}))
    assert valid['status'] == 'succeeded', valid
    invalid = wait_task(client, base, start(client, base, 'invalid-analysis', workflow_id=project['id'], inputs={
        'analysis': {'operation': 'spearman', 'columns': [42, 'y']}}))
    assert invalid['status'] == 'failed' and 'columns.0' in invalid['error']


def test_analysis_prediction_example_recomputes_every_result_for_new_input(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base = '/api/v1/projects/' + project['id']
    help = client.post(base + '/agent-tools', json={'name': 'project_skills', 'arguments': {
        'action': 'read', 'skill_id': 'modeling', 'reference': 'analysis-prediction-workflow.json'}})
    assert help.status_code == 200, help.text
    example = json.loads(help.json()['content'])
    # The full editable chain can be saved before a model or dataset exists.
    graph(client, project['id'], example['nodes'], example['edges'])
    base, _, study, candidate = setup(client, project, settings, batch_size=1,
                                     features={'columns': ['x', 'category']})
    trained = wait_task(client, base, start(client, base, 'train-template-model'))
    assert trained['status'] == 'succeeded', trained
    assert client.put(base + '/models/quality', json={'name': '质量模型', 'study_id': study['id'],
        'candidate_id': candidate['id'], 'slot': 0}).status_code == 200
    # Once the project setup is known, the customer supplies only a new file.
    for field in example['nodes'][0]['config']['inputs']:
        if field['name'] in {'analysis_columns', 'model_ref'}:
            field.update(required=False, default={'analysis_columns': ['x', 'height'], 'model_ref': 'quality'}[field['name']])
    graph(client, project['id'], example['nodes'], example['edges'])
    before = client.get('/api/v1/applications/' + project['id'] + '/draft').json()
    root = settings.workspace_root / project['id']
    saved = {}
    predictions = []
    for name, text, rho in [('first', 'id,x,height,category\n1,1,4,a\n2,2,3,b\n3,3,2,a\n4,100,1,b\n', -1),
                            ('second', 'id,x,height,category\n1,2,2,a\n2,3,3,b\n3,4,4,a\n4,5,5,b\n', 1)]:
        source = 'requirement-package/' + name + '.csv'
        (root / source).write_text(text)
        task = wait_task(client, base, start(client, base, name, inputs={'source_path': source}))
        assert task['status'] == 'succeeded', task
        output = task['outputs']
        assert output['数据概况']['rows'] == output['异常标记']['output_rows'] == 4
        assert output['异常标记']['affected_rows'] == (1 if name == 'first' else 0)
        assert output['相关分析']['pairs'][0]['rho'] == rho
        assert output['相关分析']['pairs'][0]['samples'] == 4
        paths = [a['file_path'] for kind in ['相关分析', '异常标记'] for a in output[kind]['artifacts']]
        prediction = output['逐条预测']['project_path']
        paths.append(prediction)
        predictions.append((root / prediction).read_text())
        assert not set(paths) & saved.keys()
        for path, content in saved.items():
            assert (root / path).read_bytes() == content
        saved.update({path: (root / path).read_bytes() for path in paths})
        assert (root / source).read_text() == text
    assert predictions[0] != predictions[1]
    assert client.get('/api/v1/applications/' + project['id'] + '/draft').json() == before


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
