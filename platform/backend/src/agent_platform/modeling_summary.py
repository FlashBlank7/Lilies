"""Agent-facing modeling summaries. Stored records and HTTP detail stay complete."""
from __future__ import annotations

from copy import deepcopy
import time


def pick(value, keys):
    return {key: deepcopy(value[key]) for key in keys if key in value}


def detail(action, **ids):
    return {'tool': 'project_modeling', 'arguments': {'action': action, **ids, 'view': 'full'}}


def profile_summary(value):
    result = pick(value, ('sampled', 'rows', 'empty_rows', 'nonempty_rows', 'duplicates', 'runtime', 'id_column_count', 'group_column_count',
                         'sample_structure', 'warnings'))
    result['columns'] = [pick(c, ('name', 'dtype', 'missing', 'unique', 'min', 'max', 'mean', 'outliers', 'value_counts',
                                'counts', 'missing_reasons', 'invalid_reasons', 'parsed_unavailable', 'category_labels'))
                         for c in value.get('columns', [])]
    if value.get('data_state'):
        result['data_state'] = pick(value['data_state'], ('counts', 'blocked_cells', 'issues_preview', 'note'))
    if 'time' in value:
        result['time'] = pick(value['time'], ('start', 'end', 'invalid', 'median_interval_seconds'))
    if 'labels' in value:
        result['labels'] = profile_summary(value['labels'])
    result['view'] = 'summary'
    return result


def dataset_summary(value):
    result = pick(value, ('id', 'name', 'status', 'replaces_id', 'mapping', 'files', 'created_at', 'updated_at', 'error'))
    result['analysis_available'] = [k for k in ('preview', 'profile') if value.get(k)]
    result.update(view='summary', detail=detail('profile', dataset_id=value['id']))
    return result


def split_summary(split, test_result=None):
    if split is None:
        return None
    result = pick(split, ('missing_labels', 'unavailable_labels', 'label_state_counts', 'excluded_data_rows', 'evaluation_label'))
    for name, field in [('samples', 'labeled_samples'), ('development', 'development_samples'), ('holdout', 'holdout_samples')]:
        if name in split:
            result[field] = len(split[name])
    if 'folds' in split:
        result['folds'] = len(split['folds'])
    result['holdout_status'] = ('evaluated' if test_result else
        'not_evaluated' if result.get('holdout_samples', 0) > 0 else
        'not_reserved' if result.get('holdout_samples') == 0 else 'unknown')
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
        result['split'] = split_summary(value['split'], value.get('test_result'))
    result.update(view='summary', detail=detail('read_study', study_id=value['id']))
    return result


def trial_summary(value):
    result = pick(value, ('slot', 'model', 'status', 'metrics', 'baseline', 'seconds', 'error', 'warnings', 'diagnostics',
                         'fold_metrics', 'resources', 'task_id', 'run_id', 'started_at', 'completed_at'))
    # Preserve the worst groups and explicitly label the omitted tail.
    groups = value.get('group_errors', [])
    result['group_errors'] = deepcopy(groups[:5])
    result['group_errors_omitted'] = max(0, len(groups) - 5)
    if value.get('classification'):
        classification = value['classification']
        result['classification'] = pick(classification, ('classes', 'folds', 'note'))
        if classification.get('baseline'):
            result['classification']['baseline'] = pick(classification['baseline'], ('classes',))
    if value.get('preprocessing'):
        result['preprocessing'] = pick(value['preprocessing'], ('settings',))
        result['preprocessing']['development'] = pick(value['preprocessing'].get('development', {}), ('rows', 'managed_by', 'numeric_columns'))
    return result


def candidate_summary(value):
    result = pick(value, ('id', 'study_id', 'status', 'hypothesis', 'parent_id', 'feedback_task_id', 'engine', 'models',
                         'batch_size', 'features', 'task_id', 'run_id', 'current', 'error', 'code_sha256',
                         'search_decision', 'created_at', 'updated_at'))
    result['trials'] = [trial_summary(t) for t in value.get('trials', [])]
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
    result['split'] = split_summary(value.get('split'), value.get('test_result'))
    result['comparison_trials'] = len(value.get('comparison', []))
    result.update(view='summary', detail=detail('training_note', study_id=value['study_id'],
                                               candidate_id=value['candidate_id'], slot=value['slot']))
    return result
