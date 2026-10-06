"""Each turn keeps complete study facts while sharing identical saved values."""
from copy import deepcopy
import json

import pytest

from agent_platform.project_agent_context import conversation_context
from tests.test_projects import configured as configured


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def saved_studies(count=4):
    return [{
        'id': f'study-{index}', 'dataset_id': f'dataset-{index}', 'name': f'研究{index}',
        'status': 'sealed', 'trials_used': index,
        'evaluation': {'metric': 'roc_auc', 'folds': 3, 'seed': 0, 'stratify': False,
                       'holdout_fraction': .2, 'group_column': 'batch', 'time_column': None},
        'split': {'development': list(range(48)), 'holdout': list(range(48, 60)),
                  'folds': [[], [], []], 'missing_labels': 0,
                  'evaluation_label': '原划分创建时的说明'},
        'test_result': {'metrics': {'roc_auc': .8 + index / 100}, 'rows': 12},
    } for index in range(count)]


@pytest.fixture
def study_context(configured, monkeypatch):
    client, app, project, _ = configured
    services = app.state.services

    async def unexpected_compute(*args, **kwargs):
        raise AssertionError('Building context must not execute a modeling task')

    monkeypatch.setattr(services.modeling, 'compute', unexpected_compute)

    def save(studies):
        for study in studies:
            client.portal.call(services.modeling.put, project['id'], 'study', deepcopy(study))

    def read():
        return client.portal.call(conversation_context, services, project['id'], {'phase': 'working'},
                                  {'status': 'draft', 'revision': 0, 'document': ''}, '只解释已有结果。')

    return configured, save, read


def expand(context):
    return [{**context.get('modeling_shared', {}), **entry} for entry in context['modeling']]


def test_shared_context_preserves_complete_study_facts_and_saves_sent_bytes(study_context):
    (client, app, project, _), save, read = study_context
    studies = saved_studies()
    # Different statuses and per-study details stay attached to their own IDs.
    studies[1].update(status='running', best={'candidate_id': 'candidate-b', 'slot': 0},
                      budget={'trials': 8, 'seconds': 300}, next_action='continue')
    studies[2].update(status='failed', error='failed trial', repair_candidate_id='candidate-c',
                      failure_streak=1)
    save(studies)
    before = client.portal.call(app.state.services.modeling.list, project['id'], 'study')

    context = read()
    restored = {entry['id']: entry for entry in expand(context)}

    assert len(restored) == len(studies)
    for study in studies:
        entry = restored[study['id']]
        assert serialized(entry['evaluation']) == serialized(study['evaluation'])
        assert entry['split'] == {
            'missing_labels': 0, 'evaluation_label': '保留测试已完成并保存结果，未参与模型搜索',
            'development_samples': 48, 'holdout_samples': 12, 'folds': 3,
        }
        for key in ('dataset_id', 'name', 'status', 'trials_used'):
            assert entry[key] == study[key]
        if study['status'] != 'sealed':
            for key in ('best', 'budget', 'next_action', 'error', 'repair_candidate_id', 'failure_streak'):
                assert entry[key] == study.get(key)
        else:
            assert entry['read_with']['arguments']['study_id'] == study['id']
    expanded = {key: value for key, value in context.items()
                if key not in {'modeling_shared', 'modeling_shared_detail'}}
    expanded['modeling'] = expand(context)
    assert len(serialized(context)) < len(serialized(expanded)) - 500
    assert client.portal.call(app.state.services.modeling.list, project['id'], 'study') == before
    assert client.get('/api/v1/projects/' + project['id'] + '/tasks').json() == []


@pytest.mark.parametrize('field,original,changed', [
    ('evaluation', 0, False),
    ('evaluation', 0, 0.0),
    ('evaluation', False, None),
    ('split', 0, False),
    ('split', 0, 0.0),
    ('split', 0, None),
])
def test_different_json_values_remain_on_the_correct_study(study_context, field, original, changed):
    _, save, read = study_context
    studies = saved_studies()
    key = 'seed' if field == 'evaluation' else 'missing_labels'
    for study in studies:
        study[field][key] = original
    studies[1][field][key] = changed
    save(studies)

    context = read()
    assert field not in context.get('modeling_shared', {})
    restored = {entry['id']: entry for entry in expand(context)}
    for study in studies:
        assert serialized(restored[study['id']][field][key]) == serialized(study[field][key])


@pytest.mark.parametrize('field', ['evaluation', 'split'])
def test_missing_study_facts_are_not_inherited_from_other_studies(study_context, field):
    _, save, read = study_context
    studies = saved_studies()
    del studies[1][field]
    save(studies)

    context = read()
    assert field not in context.get('modeling_shared', {})
    restored = {entry['id']: entry for entry in expand(context)}
    if field == 'split':
        assert field not in restored[studies[1]['id']]
    else:
        # The existing index represents a missing evaluation as null.
        assert restored[studies[1]['id']][field] is None
    assert restored[studies[0]['id']][field] is not None


@pytest.mark.parametrize('value', [0, False, None])
def test_identical_zero_false_and_null_facts_survive_shared_context(study_context, value):
    _, save, read = study_context
    studies = saved_studies()
    for study in studies:
        study['evaluation'] = value
        study['split']['missing_labels'] = value
    save(studies)

    context = read()
    assert context.get('modeling_shared')
    for entry in expand(context):
        assert serialized(entry['evaluation']) == serialized(value)
        assert serialized(entry['split']['missing_labels']) == serialized(value)


@pytest.mark.parametrize('count', [0, 1, 4])
def test_small_studies_keep_the_original_format_when_sharing_would_cost_more(study_context, count):
    _, save, read = study_context
    studies = saved_studies(count)
    for study in studies:
        study['evaluation'] = None
        del study['split']
    save(studies)

    context = read()
    assert 'modeling_shared' not in context and 'modeling_shared_detail' not in context
    assert len(context['modeling']) == count
    assert all(entry['evaluation'] is None and 'split' not in entry for entry in context['modeling'])


def test_next_turn_refreshes_shared_facts_without_rewriting_history_or_full_tools(study_context):
    (client, _, project, _), save, read = study_context
    from tests.test_project_tool_help import prepare
    prepare(study_context[0])
    studies = saved_studies()
    save(studies)
    base = '/api/v1/projects/' + project['id']
    before_http = {study['id']: client.get(base + '/modeling/studies/' + study['id']).json()
                   for study in studies}
    first = read()
    first_snapshot = deepcopy(first)

    for entry in first['modeling']:
        response = client.post(base + '/agent-tools', json={
            'name': entry['read_with']['tool'],
            'arguments': {**entry['read_with']['arguments'], 'view': 'full'},
        })
        assert response.status_code == 200, response.text
        assert response.json() == before_http[entry['id']]

    changed = deepcopy(studies[1])
    changed['evaluation']['metric'] = 'accuracy'
    changed['split']['holdout'] = list(range(48, 58))
    save([changed])
    second = read()
    current = {entry['id']: entry for entry in expand(second)}
    assert current[changed['id']]['evaluation']['metric'] == 'accuracy'
    assert current[changed['id']]['split']['holdout_samples'] == 10
    assert current[studies[0]['id']]['evaluation']['metric'] == 'roc_auc'
    assert current[studies[0]['id']]['split']['holdout_samples'] == 12
    assert first == first_snapshot
    assert {key: value for key, value in first.items() if not key.startswith('modeling')} == {
        key: value for key, value in second.items() if not key.startswith('modeling')}

    # Editing this turn's local representation cannot change saved history or
    # leak into the next turn, which reads the current records independently.
    first['modeling_shared']['evaluation']['metric'] = 'mutated context'
    third = read()
    assert serialized(third) == serialized(second)
    for study in studies:
        if study['id'] != changed['id']:
            assert client.get(base + '/modeling/studies/' + study['id']).json() == before_http[study['id']]
    assert client.get(base + '/tasks').json() == []
