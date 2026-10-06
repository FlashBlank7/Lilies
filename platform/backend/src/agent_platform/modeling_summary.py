"""Agent-facing modeling summaries. Stored records and HTTP detail stay complete."""
from __future__ import annotations

from copy import deepcopy
import json
import time


def pick(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def detail(action, **ids):
    return {'tool': 'project_modeling', 'arguments': {'action': action, **ids, 'view': 'full'}}


def profile_summary(value):
    result = pick(value, ('dataset_id', 'sampled', 'rows', 'duplicates', 'runtime', 'id_column_count', 'group_column_count'))
    result['columns'] = []
    for column in value.get('columns', []):
        compact = pick(column, ('name', 'dtype', 'missing', 'unique', 'min', 'max', 'mean', 'outliers'))
        distribution = column.get('distribution')
        if isinstance(distribution, list) and all(isinstance(row, dict) and 'label' in row and 'count' in row for row in distribution):
            # Keep small saved frequency tables, especially target classes.
            # They are observations, not a claim that all values are listed.
            if len(distribution) <= 12 and len(json.dumps(distribution)) <= 1500:
                compact['distribution'] = deepcopy(distribution)
            else:
                compact['distribution'] = {'preview_omitted': True, 'count': len(distribution)}
            rows, missing = value.get('rows'), column.get('missing')
            if (type(rows) is int and type(missing) is int
                    and all(type(row['count']) is int and row['count'] >= 0 for row in distribution)):
                represented = sum(row['count'] for row in distribution)
                compact['distribution_coverage'] = {
                    'represented_rows': represented, 'non_missing_rows': rows - missing,
                    'complete': represented == rows - missing,
                }
                if represented < rows - missing:
                    compact['distribution_coverage']['note'] = '部分频次；未列出的类别或批次数量未知，不能推断所有分组大小相同。'
        result['columns'].append(compact)
    if 'time' in value:
        result['time'] = pick(value['time'], ('start', 'end', 'invalid', 'median_interval_seconds'))
    if 'labels' in value:
        result['labels'] = profile_summary(value['labels'])
    if isinstance(value.get('preview'), list):
        result['preview'] = {'preview_omitted': True, 'count': len(value['preview'])}
    result['distribution_scope'] = 'Saved frequency tables or histogram bins; these may show only leading values, not every distinct value.'
    result['view'] = 'summary'
    return result


def dataset_summary(value):
    result = pick(value, ('id', 'name', 'status', 'replaces_id', 'mapping', 'files', 'created_at', 'updated_at', 'error'))
    result['analysis_available'] = [k for k in ('preview', 'profile') if value.get(k)]
    result.update(view='summary', detail=detail('profile', dataset_id=value['id']))
    return result


def split_summary(value, *, study=None):
    result = pick(value, ('missing_labels', 'evaluation_label'))
    # The split artifact records its creation-time label. Display the current
    # evaluation state without rewriting that immutable training artifact.
    if study and study.get('test_result'):
        result['evaluation_label'] = '保留测试已完成并保存结果，未参与模型搜索'
    elif study and study.get('status') == 'sealed':
        result['evaluation_label'] = '保留测试集已封存（此摘要没有已保存的测试结果）'
    for source, target in (('development', 'development_samples'), ('holdout', 'holdout_samples'), ('folds', 'folds')):
        if isinstance(value.get(source), list):
            result[target] = len(value[source])
    return result


def study_summary(value):
    result = pick(value, ('id', 'dataset_id', 'name', 'item_id', 'parent_study_id', 'status', 'evaluation', 'budget',
                         'best', 'baseline', 'trials_used', 'next_action', 'error', 'repair_candidate_id',
                         'failure_streak', 'no_improvement_batches', 'search_strategy', 'aide',
                         'test_result', 'created_at', 'updated_at'))
    elapsed = value.get('used_seconds', 0) + (max(0, time.time() - value['active_since']) if value.get('active_since') else 0)
    budget = value.get('budget', {})
    result['remaining_budget'] = {'seconds': round(max(0, budget.get('seconds', 0) - elapsed), 2),
                                  'trials': max(0, budget.get('trials', 0) - value.get('trials_used', 0))}
    result['metric_direction'] = 'minimize' if value['evaluation']['metric'] in {'mae', 'rmse'} else 'maximize'
    if value.get('split'):
        result['split'] = split_summary(value['split'], study=value)
    result.update(view='summary', detail=detail('read_study', study_id=value['id']))
    return result


def trial_summary(value):
    result = pick(value, ('slot', 'model', 'status', 'metrics', 'baseline', 'seconds', 'error', 'warnings', 'diagnostics',
                         'fold_metrics', 'task_id', 'run_id', 'started_at', 'completed_at'))
    # Preserve the worst groups and explicitly label the omitted tail.
    groups = value.get('group_errors', [])
    result['group_errors'] = deepcopy(groups[:5])
    result['group_errors_omitted'] = max(0, len(groups) - 5)
    return result


def candidate_summary(value):
    result = pick(value, ('id', 'study_id', 'status', 'hypothesis', 'parent_id', 'feedback_task_id', 'engine', 'models',
                         'batch_size', 'features', 'task_id', 'run_id', 'current', 'error', 'code_sha256',
                         'search_decision', 'created_at', 'updated_at'))
    result['trials'] = [trial_summary(t) for t in value.get('trials', [])]
    trials = result['trials']
    if len(trials) >= 2:
        shared = {}
        for key in ('baseline', 'diagnostics', 'task_id', 'run_id'):
            if all(key in trial for trial in trials):
                # Compare JSON values including their types: Python equality
                # alone would also merge false with 0 in nested diagnostics.
                first = json.dumps(trials[0][key], sort_keys=True)
                if all(json.dumps(trial[key], sort_keys=True) == first for trial in trials[1:]):
                    shared[key] = trials[0][key]
                    for trial in trials:
                        del trial[key]
        if shared:
            result['trial_shared'] = shared
            result['trial_shared_detail'] = (
                'Every trial inherits trial_shared; its own fields take precedence. '
                'Shared values were identical in all recorded trials.')
    result.update(view='summary', detail=detail('candidates', study_id=value['study_id'], candidate_id=value['id']))
    result['training_note'] = detail('training_note', study_id=value['study_id'], candidate_id=value['id'], slot=0)
    return result


def note_summary(value):
    result = pick(value, ('study_id', 'candidate_id', 'slot', 'title', 'evaluation', 'hypothesis', 'parent_candidate_id',
                         'feedback_task_id', 'task_id', 'run_id', 'is_current_best', 'study_status', 'gaps', 'scope',
                         'search_strategy', 'search_decision', 'test_result', 'files'))
    result['trial'] = trial_summary(value['trial'])
    result['dataset'] = pick(value['dataset'], ('id', 'name', 'mapping'))
    result['features'] = deepcopy(value['features'])
    split = value.get('split')
    result['split'] = None if split is None else split_summary(split, study={
        'status': value.get('study_status'), 'test_result': value.get('test_result')})
    result['comparison_trials'] = len(value.get('comparison', []))
    result.update(view='summary', detail=detail('training_note', study_id=value['study_id'],
                                               candidate_id=value['candidate_id'], slot=value['slot']))
    return result
