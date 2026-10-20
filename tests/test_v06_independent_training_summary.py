"""Independent training uses the public tools and real task lifecycle, offline."""
import asyncio
from copy import deepcopy
import json

import pytest

from tests.test_modeling import modeling as modeling, wait_task  # noqa: F401
from tests.test_projects import configured as configured  # noqa: F401


@pytest.fixture
def training_project(modeling):
    (client, app, project, settings), service = modeling
    base = '/api/v1/projects/' + project['id']
    response = client.post(base + '/datasets/upload',
        files={'file': ('train.csv', b'x,y\n1,2\n2,4\n3,6\n4,8\n', 'text/csv')},
        data={'mapping': json.dumps({'target': 'y'})})
    assert response.status_code == 201, response.text
    response = client.post(base + '/modeling/studies', json={
        'dataset_id': response.json()['id'], 'request_key': 'summary-study',
        'budget': {'trials': 3, 'seconds': 180, 'trial_seconds': 45},
    })
    assert response.status_code == 201, response.text
    yield client, app, project, service, base, response.json()


def train(client, base, study, **options):
    response = client.post(base + '/agent-tools', json={
        'name': 'project_modeling', 'arguments': {
            'action': 'train', 'study_id': study['id'],
            'candidate': {'request_key': 'same-training', 'engine': 'sklearn',
                          'models': ['linear', 'forest'], 'batch_size': 2},
            **options,
        },
    })
    assert response.status_code == 200, response.text
    return response.json()


async def prepare(folder):
    split = {'development': [0, 1, 2], 'holdout': [3],
             'folds': [[[0, 1], [2]], [[0, 2], [1]]]}
    output = folder / 'output'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'split.json').write_text(json.dumps(split))
    return split


def test_train_default_summary_preserves_measured_trials_and_full_storage(training_project, monkeypatch):
    client, _, project, service, base, study = training_project
    draft_before = client.get('/api/v1/applications/' + project['id'] + '/draft').json()
    calls = []
    trials = [
        {'slot': 0, 'model': 'linear', 'status': 'completed', 'seconds': 1.25,
         'metrics': {'mae': .125, 'rmse': .25}, 'baseline': {'mae': 1.75},
         'fold_metrics': [{'mae': .1}, {'mae': .15}],
         'effective_model': {'coefficients': list(range(4000))},
         'prediction_preview': [{'actual': i, 'prediction': i + .125} for i in range(400)],
         'feature_columns': ['x']},
        {'slot': 1, 'model': 'forest', 'status': 'failed', 'seconds': .25,
         'error': '训练字段缺失：temperature'},
    ]

    async def compute(pid, dataset, config, folder, *, on_event=None, **kwargs):
        calls.append(config['action'])
        if config['action'] == 'prepare':
            return await prepare(folder)
        assert config['action'] == 'train'
        for trial in trials:
            await on_event({'kind': 'trial', **deepcopy(trial)})
        return {'status': 'completed', 'features': [{'name': 'x', 'source': 'x'}],
                'runtime': {'seconds': 1.5, 'peak_memory_bytes': 1024}}

    monkeypatch.setattr(service, 'compute', compute)
    summary = train(client, base, study)
    assert summary['status'] == 'succeeded'
    stored = client.get(base + '/tasks/' + summary['id']).json()
    full = train(client, base, study, view='full')
    duplicate = train(client, base, study)
    assert full['id'] == duplicate['id'] == stored['id'] == summary['id']
    assert calls == ['prepare', 'train']
    assert full == stored
    assert stored['workflow_id'] == '' and stored['runs'] == []
    assert len(client.get(base + '/tasks').json()) == 1
    assert client.get('/api/v1/applications/' + project['id'] + '/draft').json() == draft_before
    recorded = stored['outputs']['trials']
    assert recorded[0]['effective_model'] == trials[0]['effective_model']
    assert recorded[0]['prediction_preview'] == trials[0]['prediction_preview']
    candidate = client.get(base + '/modeling/studies/' + study['id'] + '/candidates/' + stored['outputs']['id']).json()
    assert candidate == stored['outputs']
    assert summary['view'] == duplicate['view'] == 'summary'
    measured = summary['outputs']['trials']
    assert [(t['slot'], t['model'], t['status']) for t in measured] == [
        (0, 'linear', 'completed'), (1, 'forest', 'failed')]
    assert measured[0]['metrics'] == trials[0]['metrics']
    assert measured[0]['baseline'] == trials[0]['baseline']
    assert measured[0]['fold_metrics'] == trials[0]['fold_metrics']
    assert measured[1]['error'] == trials[1]['error']
    assert 'effective_model' not in measured[0] and 'prediction_preview' not in measured[0]
    assert len(json.dumps(summary)) < len(json.dumps(full))
    assert client.get(base + '/tasks/' + summary['id']).json() == stored


def test_train_background_summary_tracks_the_original_task_for_agent_stop(training_project, monkeypatch):
    client, app, project, service, base, study = training_project
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    async def compute(pid, dataset, config, folder, *, on_event=None, **kwargs):
        calls.append(config['action'])
        if config['action'] == 'prepare':
            return await prepare(folder)
        assert config['action'] == 'train'
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(service, 'compute', compute)
    task = train(client, base, study, wait=False)
    client.portal.call(asyncio.wait_for, entered.wait(), 2)
    try:
        duplicate = train(client, base, study, wait=False)
        full = train(client, base, study, wait=False, view='full')
        assert duplicate['id'] == full['id'] == task['id']
        assert task['status'] in {'queued', 'running'}
        assert calls == ['prepare', 'train']
        assert app.state.services.local_agents.load(project['id'])['continue_work'] is True
        response = client.post(base + '/agent-session/stop')
        assert response.status_code == 200, response.text
        client.portal.call(asyncio.wait_for, cancelled.wait(), 2)
        stopped = wait_task(client, base, task, seconds=2)
        assert stopped['status'] == 'interrupted'
        candidate = client.get(base + '/modeling/studies/' + study['id'] + '/candidates/' + full['inputs']['candidate_id']).json()
        assert candidate['status'] == 'interrupted' and candidate['task_id'] == task['id']
        assert candidate['trials'] == []
        assert len(client.get(base + '/tasks').json()) == 1
        assert task['view'] == duplicate['view'] == 'summary'
        assert 'view' not in full
    finally:
        client.post(base + '/tasks/' + task['id'] + '/stop').raise_for_status()
