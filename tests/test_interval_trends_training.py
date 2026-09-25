"""Time-aware native classification and prediction from the reusable sample flow."""
import csv
import io
from datetime import datetime

from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_projects import configured, start  # noqa: F401
from tests.test_example_projects import install


def test_real_interval_samples_time_purge_binding_and_prediction(real_compute):
    (client, app, _, settings), service = real_compute
    pid = install(client, 'interval-trends'); base = '/api/v1/projects/' + pid
    guide = client.get(base + '/example').json()
    workflows = {w['name']: w['id'] for w in guide['workflows']}
    prepared = wait_task(client, base, start(client, base, 'prepare', workflow_id=pid))
    assert prepared['status'] == 'succeeded', prepared['error']
    data = prepared['outputs']['result']
    trained = wait_task(client, base, start(client, base, 'train',
        workflow_id=workflows['趋势分类训练与时间验证'], inputs={'source_path': data['source_path']}))
    assert trained['status'] == 'succeeded', trained['error']
    output = trained['outputs']; candidate = output['training']
    study = client.get(base + '/modeling/studies/' + candidate['study_id']).json()
    with (settings.workspace_root / pid / data['source_path']).open(encoding='utf-8-sig', newline='') as stream:
        samples = list(csv.DictReader(stream))
    split = study['split']
    folds = split['folds'] + [[split['development'], split['holdout']]]
    for train, valid in folds:
        assert train and valid
        cutoff = min(datetime.fromisoformat(samples[i]['origin_time']) for i in valid)
        assert all(datetime.fromisoformat(samples[i]['label_available_time']) < cutoff for i in train)
        assert {samples[i]['origin_time'] for i in train}.isdisjoint(samples[i]['origin_time'] for i in valid)
    assert all(set(trial['feature_columns']) == {'lag_0', 'lag_1', 'lag_2', 'interval'}
               for trial in candidate['trials'] if trial['status'] == 'completed')
    assert len(candidate['trials']) == 2
    assert output['test']['rows'] == len(split['holdout'])
    predict_id = workflows['使用已绑定趋势模型预测']
    missing = wait_task(client, base, start(client, base, 'missing-binding', workflow_id=predict_id,
        inputs={'source_path': data['prediction_path']}))
    assert missing['status'] == 'failed' and 'interval-trend' in missing['error']
    best = study['best']
    bound = client.put(base + '/models/interval-trend', json={'name': '区间趋势模型',
        'study_id': study['id'], 'candidate_id': best['candidate_id'], 'slot': best['slot']})
    assert bound.status_code == 200, bound.text
    predicted = wait_task(client, base, start(client, base, 'predict', workflow_id=predict_id,
        inputs={'source_path': data['prediction_path']}))
    assert predicted['status'] == 'succeeded', predicted['error']
    result = predicted['outputs']['result']
    assert result['rows'] == 3
    registered = client.post(base + '/datasets', json={'source_path': data['prediction_path']}).json()
    independent = client.post(base + '/models/interval-trend/predict',
        json={'dataset_id': registered['id'], 'request_key': 'same-model-independent'})
    assert independent.status_code == 202, independent.text
    same = wait_task(client, base, independent.json())
    assert same['status'] == 'succeeded', same['error']
    assert same['outputs']['preview'] == result['preview']
    assert same['outputs']['model_version'] == result['model_version']
    downloaded = client.get(base + '/' + result['artifact'])
    assert downloaded.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(downloaded.content.decode('utf-8-sig'))))) == 3
    assert client.get(base + '/tasks/' + prepared['id']).json()['outputs'] == prepared['outputs']
    assert client.get(base + '/tasks/' + trained['id']).json()['outputs'] == trained['outputs']
    assert not app.state.services.local_agents.tasks
