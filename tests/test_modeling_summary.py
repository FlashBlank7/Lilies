"""Saved modeling summaries expose split sizes without training or changing history."""
import asyncio
from copy import deepcopy
import json

import pytest

from agent_platform.modeling_summary import baseline_comparison, candidate_summary, study_summary, note_summary, trial_summary
from tests.test_modeling import modeling, setup  # noqa: F401
from tests.test_projects import configured  # noqa: F401


def test_baseline_arithmetic_keeps_units_and_does_not_call_r2_an_error_reduction():
    metrics = {'mae': 2., 'rmse': 12., 'r2': .5, 'accuracy': .9}
    baseline = {'mae': 10., 'rmse': 8., 'r2': -.5, 'accuracy': .6}
    original = deepcopy((metrics, baseline))
    result = baseline_comparison(metrics, baseline)
    assert result['mae'] == {'baseline': 10., 'value_minus_baseline': -8., 'error_reduction_fraction': .8}
    assert result['rmse'] == {'baseline': 8., 'value_minus_baseline': 4., 'error_reduction_fraction': -.5}
    assert result['r2'] == {'baseline': -.5, 'value_minus_baseline': 1.}
    assert result['accuracy']['value_minus_baseline'] == pytest.approx(.3)
    assert 'error_reduction_fraction' not in result['accuracy']
    assert (metrics, baseline) == original


@pytest.mark.parametrize('value,baseline', [(None, 1), (1, None), (True, 1), (1, False),
    ('1', 2), (1, '2'), (float('inf'), 1), (1, float('nan')), (1e308, -1e308),
    (10**400, 1), (1, 10**400)])
def test_baseline_arithmetic_does_not_fabricate_invalid_comparisons(value, baseline):
    assert baseline_comparison({'mae': value}, {'mae': baseline}) == {}


@pytest.mark.parametrize('value,baseline', [(0, 0), (1, 0), (-1, 2), (1, -2)])
def test_baseline_arithmetic_avoids_undefined_or_invalid_error_ratios(value, baseline):
    assert baseline_comparison({'mae': value}, {'mae': baseline}) == {
        'mae': {'baseline': baseline, 'value_minus_baseline': value - baseline}}


def saved_trials():
    return [{
        'slot': slot, 'model': model, 'status': 'completed',
        'metrics': {'mae': .2 + slot / 10}, 'baseline': {'mae': .5},
        'diagnostics': [{'fold': 1, 'train_rows': 128, 'validation_rows': 64,
                         'constant_prediction': False}],
        'fold_metrics': [{'mae': .3 + slot / 10}],
        'group_errors': [{'group': 'batch-a', 'error': .1 + slot / 10}],
        'task_id': 'saved-task', 'run_id': 'saved-run',
        'effective_model': {'saved_detail': 'x' * 5000},
    } for slot, model in enumerate(('linear', 'forest'))]


@pytest.mark.parametrize('case,shared_keys', [
    ('same', {'baseline', 'diagnostics', 'task_id', 'run_id'}),
    ('different', {'baseline', 'task_id', 'run_id'}),
    ('missing', {'diagnostics', 'task_id', 'run_id'}),
    ('different_types', {'baseline', 'task_id', 'run_id'}),
    ('null', {'baseline', 'diagnostics', 'task_id', 'run_id'}),
    ('all_different', set()),
    ('single', set()),
    ('empty', set()),
])
def test_candidate_shares_only_present_identical_trial_values(case, shared_keys):
    trials = saved_trials()
    if case == 'different':
        trials[1]['diagnostics'][0]['validation_rows'] = 63
    elif case == 'missing':
        del trials[1]['baseline']
    elif case == 'different_types':
        trials[1]['diagnostics'][0]['constant_prediction'] = 0
    elif case == 'null':
        for trial in trials:
            trial['baseline'] = None
    elif case == 'all_different':
        trials[1].update(baseline={'mae': .7}, diagnostics=[], task_id='other-task', run_id='other-run')
    elif case == 'single':
        trials = trials[:1]
    elif case == 'empty':
        trials = []
    candidate = {'id': 'c', 'study_id': 's', 'trials': trials}
    before = deepcopy(candidate)
    expected = [trial_summary(trial) for trial in trials]

    summary = candidate_summary(candidate)

    assert set(summary.get('trial_shared', {})) == shared_keys
    if shared_keys:
        assert summary['trial_shared_detail']
        assert all(not shared_keys.intersection(trial) for trial in summary['trials'])
    else:
        assert 'trial_shared' not in summary and 'trial_shared_detail' not in summary
    restored = [{**summary.get('trial_shared', {}), **trial} for trial in summary['trials']]
    assert json.dumps(restored, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert candidate == before
    # The summary owns its nested values; callers cannot change saved records.
    if 'diagnostics' in summary.get('trial_shared', {}):
        summary['trial_shared']['diagnostics'][0]['train_rows'] = 0
        assert candidate == before


@pytest.mark.parametrize('engine', ['sklearn', 'optuna', 'autogluon'])
@pytest.mark.parametrize('nested', [True, False], ids=['workflow-output', 'independent-training'])
def test_shared_trials_reach_modeling_and_workflow_tools_without_changing_http_or_full_results(modeling, monkeypatch, engine, nested):
    (client, app, project, settings), service = modeling
    from tests.test_project_tool_help import prepare
    prepare(modeling[0])
    base, _, study, candidate = setup(client, project, settings)
    candidate.update(status='completed', engine=engine, trials=saved_trials())
    asyncio.run(service.put(project['id'], 'candidate', candidate))
    store = app.state.services.projects.store
    asyncio.run(store.create_task('saved-task', project['id'], 'saved-result', 'workflow',
                                 project['id'], {}, '', {}))
    outputs = {'training': candidate} if nested else candidate
    asyncio.run(store.update_task('saved-task', status='succeeded', outputs=outputs))

    async def unexpected_compute(*args, **kwargs):
        raise AssertionError('Inspecting a saved summary must not start computation')

    monkeypatch.setattr(service, 'compute', unexpected_compute)
    candidates_url = base + '/modeling/studies/' + study['id'] + '/candidates'
    candidate_url = candidates_url + '/' + candidate['id']
    before_http = client.get(candidate_url).json()
    before_http_summary = client.get(candidates_url + '?summary=true').json()
    before_task = client.get(base + '/tasks/saved-task').json()

    def tool(name, **arguments):
        response = client.post(base + '/agent-tools', json={'name': name, 'arguments': arguments})
        assert response.status_code == 200, response.text
        return response.json()

    candidate_args = {'action': 'candidates', 'study_id': study['id'], 'candidate_id': candidate['id']}
    direct = tool('project_modeling', **candidate_args)
    inspected = tool('workflow_run', action='inspect', task_id='saved-task')
    assert inspected['outputs_truncated'] is True
    assert (inspected['outputs']['training'] if nested else inspected['outputs']) == direct
    assert set(direct['trial_shared']) == {'baseline', 'diagnostics', 'task_id', 'run_id'}
    restored = [{**direct['trial_shared'], **trial} for trial in direct['trials']]
    assert restored == [trial_summary(trial) for trial in candidate['trials']]
    assert tool('project_modeling', **candidate_args, view='full') == before_http
    assert tool('workflow_run', action='inspect', task_id='saved-task', view='full')['outputs'] == before_task['outputs']
    path = ['training'] if nested else ['trials']
    assert tool('workflow_run', action='inspect', task_id='saved-task', output_path=path)['output'] == (candidate if nested else candidate['trials'])
    assert client.get(candidate_url).json() == before_http
    assert client.get(candidates_url + '?summary=true').json() == before_http_summary
    assert client.get(base + '/tasks/saved-task').json() == before_task
    assert [task['id'] for task in client.get(base + '/tasks').json()] == ['saved-task']


@pytest.mark.parametrize('split,counts', [
    ({}, {}),
    ({'development': None, 'holdout': 'unknown', 'folds': {'count': 3}}, {}),
    ({'development': [], 'holdout': [], 'folds': []},
     {'development_samples': 0, 'holdout_samples': 0, 'folds': 0}),
    ({'development': [0, 1], 'holdout': None, 'folds': []},
     {'development_samples': 2, 'folds': 0}),
])
def test_study_summary_only_counts_saved_arrays(split, counts):
    metadata = {'missing_labels': 1, 'evaluation_label': '仅验证集结果'}
    study = {'id': 's', 'evaluation': {'metric': 'mae'}, 'split': {**metadata, **split}}
    before = deepcopy(study)
    assert study_summary(study)['split'] == {**metadata, **counts}
    assert study == before


@pytest.mark.parametrize('result,expected_label', [
    (None, '保留测试集已封存（此摘要没有已保存的测试结果）'),
    ({'metrics': {'mae': .12}, 'rows': 12}, '保留测试已完成并保存结果，未参与模型搜索'),
])
def test_sealed_study_and_trial_note_show_saved_evaluation_state_without_rewriting_split(result, expected_label):
    split = {'development': list(range(48)), 'holdout': list(range(48, 60)), 'folds': [],
             'evaluation_label': '保留测试集（尚未使用）'}
    study = {'id': 's', 'status': 'sealed', 'evaluation': {'metric': 'mae'},
             'split': deepcopy(split), 'test_result': deepcopy(result)}
    before = deepcopy(study)
    summary = study_summary(study)
    assert summary['split']['evaluation_label'] == expected_label
    assert summary['split']['holdout_samples'] == 12
    assert summary['test_result'] == result
    assert study == before
    note = {'study_id': 's', 'candidate_id': 'c', 'slot': 0, 'trial': {},
            'dataset': {'id': 'd'}, 'features': {}, 'split': deepcopy(split),
            'study_status': 'sealed', 'test_result': deepcopy(result)}
    original = deepcopy(note)
    assert note_summary(note)['split']['evaluation_label'] == expected_label
    assert note == original


def test_http_study_summary_counts_saved_split_without_changing_detail(modeling, monkeypatch):
    (client, app, project, settings), service = modeling

    async def unexpected_compute(*args, **kwargs):
        raise AssertionError('Reading a study summary must not start training')

    monkeypatch.setattr(service, 'compute', unexpected_compute)
    base, dataset, study, candidate = setup(client, project, settings)
    development = list(range(48))
    split = {'samples': list(range(60)), 'development': development, 'holdout': list(range(48, 60)),
             'folds': [[development[16:], development[:16]],
                       [development[:16] + development[32:], development[16:32]],
                       [development[:32], development[32:]]],
             'missing_labels': 0, 'evaluation_label': '保留测试集（尚未使用）'}
    study['split'] = deepcopy(split)
    asyncio.run(service.put(project['id'], 'study', study))
    detail_url = base + '/modeling/studies/' + study['id']
    before = client.get(detail_url).json()

    response = client.get(base + '/modeling/studies?summary=true')
    assert response.status_code == 200, response.text
    summary = next(value for value in response.json() if value['id'] == study['id'])
    assert summary['split'] == {'development_samples': 48, 'holdout_samples': 12, 'folds': 3,
                                'missing_labels': 0, 'evaluation_label': '保留测试集（尚未使用）'}
    assert summary['evaluation'] == study['evaluation']
    assert summary['image'] == study['image']  # Existing HTTP summary fields remain available.
    assert 'request' not in summary
    assert before['split'] == split
    assert client.get(detail_url).json() == before
    assert client.get(base + '/modeling/studies').json() == [before]
    assert client.get(base + '/tasks').json() == []

    from agent_platform.project_agent_context import conversation_context
    context = asyncio.run(conversation_context(
        app.state.services, project['id'], {'phase': 'working'},
        {'status': 'draft', 'revision': 0, 'document': ''}, '解释已有训练结果'))
    saved = next(value for value in context['modeling'] if value['id'] == study['id'])
    assert saved['split'] == summary['split']
    assert saved['evaluation'] == study['evaluation']
    assert not {'samples', 'development', 'holdout'} & saved['split'].keys()
    assert client.get(detail_url).json() == before
    assert client.get(base + '/tasks').json() == []

    study.update(status='sealed', test_result={'metrics': {'mae': .12}, 'rows': 12})
    asyncio.run(service.put(project['id'], 'study', study))
    current = next(value for value in client.get(base + '/modeling/studies?summary=true').json()
                   if value['id'] == study['id'])
    context = asyncio.run(conversation_context(
        app.state.services, project['id'], {'phase': 'working'},
        {'status': 'draft', 'revision': 0, 'document': ''}, '解释已有训练结果'))
    contextual = next(value for value in context['modeling'] if value['id'] == study['id'])
    assert current['split'] == contextual['split'] == study_summary(study)['split']
    assert current['split']['evaluation_label'] == '保留测试已完成并保存结果，未参与模型搜索'
    assert client.get(detail_url).json()['split'] == split
    assert client.get(base + '/tasks').json() == []


def test_finished_studies_remain_readable_without_repeating_scores_in_every_turn(modeling, monkeypatch):
    (client, app, project, settings), service = modeling
    from agent_platform.project_agent_context import conversation_context
    from tests.test_project_tool_help import prepare
    prepare(modeling[0])
    base, _, study, _ = setup(client, project, settings)
    study.update(best={'candidate_id': 'saved-candidate', 'slot': 0, 'metrics': {'mae': 0.0}},
                 baseline={'mae': 0.7}, failure_streak=2,
                 repair_candidate_id='failed-candidate', next_action='read previous error')

    async def unexpected_compute(*args, **kwargs):
        raise AssertionError('Context and result reads must not start computation')

    monkeypatch.setattr(service, 'compute', unexpected_compute)
    discussion = {'status': 'draft', 'revision': 0, 'document': ''}

    for status in ('running', 'failed', 'sealed'):
        study['status'] = status
        asyncio.run(service.put(project['id'], 'study', study))
        before = client.get(base + '/modeling/studies/' + study['id']).json()
        context = asyncio.run(conversation_context(
            app.state.services, project['id'], {'phase': 'working'}, discussion, '解释已有结果'))
        entry = next(value for value in context['modeling'] if value['id'] == study['id'])
        assert entry['dataset_id'] == study['dataset_id']
        assert entry['evaluation'] == study['evaluation']
        assert entry['status'] == status
        if status == 'sealed':
            assert not {'best', 'baseline', 'budget', 'next_action', 'repair_candidate_id'} & entry.keys()
            read = entry['read_with']
            response = client.post(base + '/agent-tools', json={
                'name': read['tool'], 'arguments': read['arguments']})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['best'] == before['best']
            assert result['baseline'] == before['baseline']
            assert result['evaluation'] == before['evaluation']
            assert result['budget'] == before['budget']
        else:
            for key in ('best', 'baseline', 'budget', 'next_action', 'repair_candidate_id', 'failure_streak'):
                assert entry[key] == before[key]
        assert client.get(base + '/modeling/studies/' + study['id']).json() == before
        assert client.get(base + '/tasks').json() == []
