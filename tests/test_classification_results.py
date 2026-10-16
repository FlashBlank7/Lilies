"""Measured class diagnostics survive workflow output, agent summaries and downloads."""
import io
import json
import zipfile

from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_projects import configured, graph, node, edge, ref, start  # noqa: F401


def test_workflow_classification_details_reach_tools_and_training_notes(real_compute):
    config, _ = real_compute
    client, app, project, settings = config
    base = '/api/v1/projects/' + project['id']
    source = settings.workspace_root / project['id'] / 'requirement-package/classes.csv'
    source.parent.mkdir(exist_ok=True)
    source.write_text('id,batch,x,y\n' + '\n'.join(
        f'{i:03d},{i//2},{i%4},{"rare" if i==0 else "fail" if i%4==1 else "pass"}' for i in range(60)))
    graph(client, project['id'], [node('start', 'start'),
        node('data', 'data_analysis', source_path='requirement-package/classes.csv',
             mapping={'target': 'y', 'id_column': 'id', 'group_column': 'batch'}),
        node('train', 'model_train', dataset_id=ref('data', 'output', 'dataset_id'),
             evaluation={'problem': 'classification', 'metric': 'macro_f1', 'split': 'group', 'folds': 3, 'holdout_fraction': 0},
             candidate={'engine': 'sklearn', 'models': ['forest'], 'parameters': {'n_estimators': 8}, 'batch_size': 1},
             budget={'seconds': 180, 'trials': 1, 'trial_seconds': 45}),
        node('end', 'end', outputs={'result': ref('train', 'output')})],
        [edge('start', 'data'), edge('data', 'train'), edge('train', 'end')])
    task = wait_task(client, base, start(client, base, 'classes', workflow_id=project['id']))
    assert task['status'] == 'succeeded', task
    candidate = task['outputs']['result']
    trial = candidate['trials'][0]
    report = trial['classification']
    assert {row['label']: row['samples'] for row in report['classes']} == {'fail': 15, 'pass': 44, 'rare': 1}
    assert len(report['folds']) == 3 and sum(map(sum, report['confusion_matrix'])) == 60
    assert any('rare' in warning and '未见过' in warning for warning in trial['warnings'])
    arguments = {'action': 'candidates', 'study_id': candidate['study_id'], 'candidate_id': candidate['id']}
    response = client.post(base + '/agent-tools', json={'name': 'project_modeling', 'arguments': arguments})
    assert response.status_code == 200, response.text
    short = response.json()['trials'][0]
    assert short['classification']['classes'] == report['classes']
    assert short['classification']['folds'] == report['folds']
    assert short['classification']['baseline']['classes'] == report['baseline']['classes']
    assert short['warnings'] == trial['warnings']
    path = base + f'/modeling/studies/{candidate["study_id"]}/candidates/{candidate["id"]}'
    note = client.get(path + '/trials/0/note').json()
    assert note['note']['trial']['classification'] == report
    assert '各类别验证结果' in note['markdown'] and '验证混淆矩阵' in note['markdown']
    with zipfile.ZipFile(io.BytesIO(client.get(path + '/download').content)) as archive:
        assert json.loads(archive.read('training-note.json'))['trial']['classification'] == report
        assert '各次验证的类别分布' in archive.read('training-note.md').decode()
