"""Configurable preprocessing from editable nodes through downloadable models."""
import csv
import io
import json
import os
import subprocess
import zipfile

import pytest

from tests.test_modeling import modeling, real_compute, setup, wait_task  # noqa: F401
from tests.test_projects import configured, edge, graph, node, ref, start  # noqa: F401


def test_preprocessing_workflow_download_runs_independently(real_compute, tmp_path):
    (client, app, project, settings), service = real_compute
    pid = project['id']; base = '/api/v1/projects/' + pid
    package = settings.workspace_root / pid / 'requirement-package'
    package.mkdir(exist_ok=True)
    (package / 'train.csv').write_text('x,y\n' + '\n'.join(f'{"" if i == 3 else 10000 if i == 39 else i},{2*i}' for i in range(40)))
    (package / 'new.csv').write_text('x\n2\n""\n100000\n')
    plan = {'columns': ['x'], 'preprocessing': {'numeric_missing': 'mean', 'scaling': 'robust',
            'outliers': 'iqr_clip', 'outlier_columns': ['x'], 'iqr_factor': 1.5}}
    graph(client, pid, [node('start', 'start'),
        node('data', 'data_analysis', source_path='requirement-package/train.csv', mapping={'target': 'y'}),
        node('features', 'feature_extract', dataset_id=ref('data', 'output', 'dataset_id'), features=plan),
        node('train', 'model_train', dataset_id=ref('features', 'output', 'dataset_id'), features=ref('features', 'output', 'feature_plan'),
             evaluation={'folds': 2, 'holdout_fraction': 0}, candidate={'engine': 'sklearn', 'models': ['linear'], 'batch_size': 1}),
        node('predict', 'model_predict', source_path='requirement-package/new.csv', study_id=ref('train', 'output', 'study_id'), candidate_id=ref('train', 'output', 'id')),
        node('end', 'end', outputs={'prediction': ref('predict', 'output'), 'training': ref('train', 'output'), 'features': ref('features', 'output')})],
        [edge('start', 'data'), edge('data', 'features'), edge('features', 'train'), edge('train', 'predict'), edge('predict', 'end')])
    task = wait_task(client, base, start(client, base, 'preprocessing', workflow_id=pid))
    assert task['status'] == 'succeeded', task
    outputs = task['outputs']; trained = outputs['training']; trial = trained['trials'][0]
    details = trial['preprocessing']['development']
    assert details['rows'] == 40 and details['outlier_bounds']['x']['upper'] < 10000
    assert trial['preprocessing']['settings']['numeric_missing'] == 'mean'
    assert outputs['prediction']['rows'] == 3
    # Feature extraction remains unfitted, including the missing and extreme row.
    artifact = next(a for a in outputs['features']['artifacts'] if a['file_path'].endswith('features.csv'))
    values = list(csv.DictReader(io.StringIO((settings.workspace_root / pid / artifact['file_path']).read_text(encoding='utf-8-sig'))))
    assert values[3]['x'] == '' and float(values[39]['x']) == 10000
    path = base + f'/modeling/studies/{trained["study_id"]}/candidates/{trained["id"]}'
    note = client.get(path + '/trials/0/note').json()
    assert note['note']['trial']['preprocessing'] == trial['preprocessing']
    assert 'outlier_bounds' in note['markdown']
    response = client.get(path + '/download')
    assert response.status_code == 200
    bundle = tmp_path / 'standalone'; bundle.mkdir()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {'modeling_preprocessing.py', 'model/preprocessing.json', 'model/model.joblib'} <= set(archive.namelist())
        archive.extractall(bundle)
    (bundle / 'input').mkdir(); (bundle / 'output').mkdir()
    (bundle / 'input/input.csv').write_bytes((package / 'new.csv').read_bytes())
    if os.environ.get('MODEL_TEST_DOCKER'):
        command = ['docker', 'run', '--rm', '--network', 'none', '--cpus', '4', '--memory', '4g', '--entrypoint', 'python',
                   '-v', f'{bundle}:/bundle:rw', '-w', '/bundle', trained['image'], 'worker.py', 'predict.example.json']
    else:
        command = [os.environ['MODEL_TEST_PYTHON'], 'worker.py', 'predict.example.json']
    result = subprocess.run(command, cwd=bundle, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    predicted = list(csv.DictReader((bundle / 'output/predictions.csv').open()))
    assert [float(row['prediction']) for row in predicted] == pytest.approx([row['prediction'] for row in outputs['prediction']['preview']])
    saved = json.loads((bundle / 'model/preprocessing.json').read_text())
    assert saved['development'] == details and len(saved['folds']) == 2


def test_old_environment_rejects_new_options_but_default_candidate_remains_usable(modeling, monkeypatch, tmp_path):
    (client, app, project, settings), service = modeling
    base, _, study, _ = setup(client, project, settings)
    old = tmp_path / 'old-environment'; old.mkdir()
    (old / 'worker.py').write_text('# old worker without configurable preprocessing')
    async def snapshot(image):
        return old
    monkeypatch.setattr(service, 'snapshot_environment', snapshot)
    path = base + '/modeling/studies/' + study['id'] + '/candidates'
    rejected = client.post(path, json={'request_key': 'new-options', 'features': {'preprocessing': {'scaling': 'none'}}})
    assert rejected.status_code == 422 and '固定的建模环境不支持' in rejected.json()['detail']
    accepted = client.post(path, json={'request_key': 'old-defaults'})
    assert accepted.status_code == 201, accepted.text
    assert len(client.get(path).json()) == 2  # Original candidate plus the default candidate.
