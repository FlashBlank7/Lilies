"""Holdout results retain the model and validation metrics actually evaluated."""
import asyncio
from copy import deepcopy

import pytest

from agent_platform.project_agent_context import task_summary
from tests.test_modeling import modeling, real_compute, setup, wait_task  # noqa: F401
from tests.test_projects import configured, edge, graph, node, ref, start  # noqa: F401


@pytest.mark.parametrize('missing', [None, 'dataset_name', 'validation_metrics'])
def test_finalize_records_the_mounted_trial_not_separate_best_metadata(modeling, monkeypatch, missing):
    (client, app, project, settings), service = modeling
    base, dataset, study, candidate = setup(client, project, settings)
    candidate['trials'] = [
        {'slot': 0, 'model': 'linear', 'status': 'completed', 'metrics': {'mae': .3},
         'feature_columns': ['x'], 'classes': []},
        {'slot': 1, 'model': 'forest', 'status': 'completed', 'metrics': {'mae': .2},
         'feature_columns': ['x'], 'classes': []},
    ]
    study.update(best={'candidate_id': candidate['id'], 'slot': 1,
                       'model': 'stale-display-name', 'metrics': {'mae': 999}},
                 split={'holdout': list(range(48, 60))})
    if missing == 'dataset_name':
        del dataset['name']
        asyncio.run(service.put(project['id'], 'dataset', dataset))
    elif missing == 'validation_metrics':
        del candidate['trials'][1]['metrics']
    asyncio.run(service.put(project['id'], 'candidate', candidate))
    asyncio.run(service.put(project['id'], 'study', study))
    measured = {'metrics': {'mae': .25}, 'rows': 12, 'label': '保留测试集结果'}
    mounts = []

    async def compute(pid, actual_dataset, config, folder, **kwargs):
        assert pid == project['id'] and actual_dataset['id'] == dataset['id']
        assert config['action'] == 'holdout'
        mounts.extend(kwargs['extra_mounts'])
        return measured

    monkeypatch.setattr(service, 'compute', compute)
    result = asyncio.run(service.finalize(project['id'], study['id'], 'final-run'))
    expected = {'study_id': study['id'], 'dataset_id': dataset['id'], 'dataset_name': dataset.get('name'),
                'candidate_id': candidate['id'], 'slot': 1, 'model': 'forest',
                'validation_metrics': None if missing == 'validation_metrics' else {'mae': .2}}
    assert result == {**measured, 'evaluated_model': expected}
    assert next(path for path, target in mounts if target == '/model') == (
        service.path(project['id'], candidate['id']) / 'output/trial-1')
    assert 'evaluated_model' not in measured
    assert client.get(base + '/modeling/studies/' + study['id']).json()['test_result'] == result
    summary = task_summary({'id': 'saved', 'outputs': {'test': result, 'large': 'x' * 9000}})
    assert summary['outputs_truncated']
    assert summary['outputs']['test'] == result


def test_legacy_test_result_is_returned_unchanged_without_guessing_a_model(modeling, monkeypatch):
    (client, app, project, settings), service = modeling
    base, _, study, _ = setup(client, project, settings)
    legacy = {'metrics': {'mae': .17}, 'rows': 12, 'label': '已有测试结果'}
    study.update(status='sealed', best=None, test_result=deepcopy(legacy))
    asyncio.run(service.put(project['id'], 'study', study))

    async def unexpected_compute(*args, **kwargs):
        raise AssertionError('A saved holdout result must not be recomputed')

    monkeypatch.setattr(service, 'compute', unexpected_compute)
    result = asyncio.run(service.finalize(project['id'], study['id'], 'new-reader'))
    assert result == legacy and 'evaluated_model' not in result
    assert client.get(base + '/modeling/studies/' + study['id']).json()['test_result'] == legacy
    assert task_summary({'id': 'old', 'outputs': {'test': result}})['outputs']['test'] == legacy
    assert client.get(base + '/tasks').json() == []


def test_real_holdout_snapshot_matches_selected_trial_and_survives_later_best_change(real_compute, monkeypatch):
    (client, app, project, settings), service = real_compute
    base, dataset, study, _ = setup(client, project, settings)
    candidates = base + '/modeling/studies/' + study['id'] + '/candidates'
    response = client.post(candidates, json={'request_key': 'evaluated-model', 'engine': 'sklearn',
                                           'models': ['linear', 'forest'], 'batch_size': 2})
    assert response.status_code == 201, response.text
    candidate = response.json()
    graph(client, project['id'], [node('start', 'start'),
        node('train', 'model_train', study_id=study['id'], candidate_id=candidate['id']),
        node('test', 'model_train', study_id=study['id'], finalize=True),
        node('end', 'end', outputs={'training': ref('train', 'output'), 'test': ref('test', 'output')})],
        [edge('start', 'train'), edge('train', 'test'), edge('test', 'end')])
    original_compute = service.compute
    holdout_mounts = []

    async def observed_compute(*args, **kwargs):
        if args[2]['action'] == 'holdout':
            holdout_mounts.append(next(path for path, target in kwargs['extra_mounts'] if target == '/model'))
        return await original_compute(*args, **kwargs)

    monkeypatch.setattr(service, 'compute', observed_compute)
    task = wait_task(client, base, start(client, base, 'evaluate-model', purpose='customer_trial'))
    assert task['status'] == 'succeeded', task
    result = task['outputs']['test']
    saved = result['evaluated_model']
    trials = task['outputs']['training']['trials']
    selected = next(trial for trial in trials if trial['slot'] == saved['slot'])
    assert saved == {'study_id': study['id'], 'dataset_id': dataset['id'], 'dataset_name': dataset['name'],
                     'candidate_id': candidate['id'], 'slot': selected['slot'], 'model': selected['model'],
                     'validation_metrics': selected['metrics']}
    assert result['rows'] == 12 and 'mae' in result['metrics']
    assert holdout_mounts == [service.path(project['id'], candidate['id']) / 'output' / f'trial-{selected["slot"]}']
    study_url = base + '/modeling/studies/' + study['id']
    current = client.get(study_url).json()
    assert current['best']['slot'] == saved['slot']
    assert current['test_result'] == result
    assert task_summary(task)['outputs']['test']['evaluated_model'] == saved
    detail_before = client.get(base + '/tasks/' + task['id']).json()

    other = next(trial for trial in trials if trial['slot'] != saved['slot'])
    current['best'] = {'candidate_id': candidate['id'], 'slot': other['slot'],
                       'model': other['model'], 'metrics': other['metrics']}
    asyncio.run(service.put(project['id'], 'study', current))
    assert asyncio.run(service.finalize(project['id'], study['id'], 'later-reader')) == result
    assert client.get(study_url).json()['test_result'] == result
    assert client.get(base + '/tasks/' + task['id']).json() == detail_before
    assert len(holdout_mounts) == 1
