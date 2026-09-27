"""Result context stays useful without copying bulk data into each model turn."""
from copy import deepcopy

from agent_platform.project_agent_context import task_summary
from agent_platform.project_metrics import payload_measurement


def test_large_table_keeps_metrics_downloads_and_late_output_fields():
    task = {'id': 'task', 'status': 'succeeded', 'outputs': {
        'features': {
            'rows': 2000,
            'preview': [{'category': 'sample', 'value': i} for i in range(2000)],
            'artifacts': [{'file_path': 'results/features.csv', 'label': 'Feature table'}],
        },
        **{f'output_{i}': i for i in range(25)},
        'test': {'rows': 48, 'metrics': {'accuracy': 46 / 48},
                 'classification': {'classes': [{'label': 'review', 'samples': 28}],
                                    'confusion_matrix': [[20, 0], [2, 26]]}},
    }}
    original = deepcopy(task)
    summary = task_summary(task)
    assert summary['outputs_truncated']
    assert set(summary['outputs']) == set(task['outputs'])
    assert summary['outputs']['test'] == task['outputs']['test']
    assert summary['outputs']['features']['artifacts'] == task['outputs']['features']['artifacts']
    assert summary['outputs']['features']['rows'] == 2000
    assert summary['outputs']['features']['preview']['count'] == 2000
    assert payload_measurement(summary)['bytes'] < 2000
    assert task == original


def test_large_code_and_nested_training_report_leave_discoverable_paths():
    task = {'id': 'task', 'outputs': {
        'code': 'print("large source")\n' * 1000,
        'training': {f'trial_{i}': {'log': 'training log\n' * 1000} for i in range(30)},
        'empty': [], 'missing': None, 'flag': False, 'zero': 0,
    }}
    summary = task_summary(task)
    assert summary['outputs']['code']['characters'] == len(task['outputs']['code'])
    assert summary['outputs']['training']['fields'][0] == 'trial_0'
    assert summary['outputs']['training']['field_count'] == 30
    for key in ('empty', 'missing', 'flag', 'zero'):
        assert summary['outputs'][key] == task['outputs'][key]
    assert payload_measurement(summary)['bytes'] < 1500


def test_small_results_are_exact_including_long_lists():
    outputs = {'classes': [{'label': f'class-{i}', 'count': i} for i in range(30)]}
    summary = task_summary({'id': 'task', 'outputs': outputs})
    assert not summary['outputs_truncated']
    assert summary['outputs'] == outputs


def test_identical_report_is_readable_once_with_metrics_and_downloads():
    report = '# 数据分析\n' + '只有一条稀有类别，预测时点未知，不能宣称模型可靠。\n' * 70
    task = {'id': 'task', 'outputs': {'markdown': report, 'result': {
        'markdown': report, 'metrics': {'rows': 24, 'rare': 1},
        'artifacts': [{'file_path': 'results/analysis.md', 'label': '分析报告'}]}}}
    original = deepcopy(task)
    assert payload_measurement(task['outputs'])['bytes'] > 8000
    summary = task_summary(task)
    assert not summary['outputs_truncated']
    assert summary['outputs']['markdown'] == report
    assert 'markdown' not in summary['outputs']['result']
    assert summary['outputs']['result']['metrics'] == {'rows': 24, 'rare': 1}
    assert summary['outputs']['result']['artifacts'] == task['outputs']['result']['artifacts']
    assert summary['output_aliases'] == [{'path': ['result', 'markdown'], 'same_as': ['markdown']}]
    assert payload_measurement(summary['outputs'])['bytes'] < 8000
    assert task == original


def test_different_report_text_is_not_treated_as_duplicate():
    report = '独立测试尚未完成。' * 100
    outputs = {'markdown': report, 'result': {'markdown': report + '补充：已完成。'}}
    summary = task_summary({'id': 'task', 'outputs': outputs})
    assert 'output_aliases' not in summary
    assert summary['outputs'] == outputs


def test_deduplication_does_not_inline_unbounded_reports():
    report = '长报告。' * 10000
    task = {'id': 'task', 'outputs': {'markdown': report, 'result': {
        'markdown': report, 'artifacts': [{'file_path': 'results/long.md'}]}}}
    summary = task_summary(task)
    assert summary['outputs_truncated']
    assert summary['outputs']['markdown']['preview_omitted']
    assert summary['outputs']['markdown']['characters'] == len(report)
    assert summary['outputs']['result']['artifacts'] == [{'file_path': 'results/long.md'}]
    assert task['outputs']['result']['markdown'] == report


def test_sample_previews_do_not_hide_small_downloads_or_metric_fields():
    artifacts = [{'file_path': 'results/' + 'a' * 120 + '/features.csv', 'label': 'Feature table'},
                 {'file_path': 'results/' + 'b' * 120 + '/samples.csv', 'label': 'Samples'}]
    task = {'id': 'task', 'outputs': {
        'raw': ['row'] * 2000,
        'features': {'artifacts': artifacts, 'rows': 200,
                     'preview': ['x' * 50] * 17, 'distribution': ['y' * 50] * 17,
                     'metrics': {'excluded': 2, 'missing': 0}},
    }}
    outputs = task_summary(task)['outputs']
    assert outputs['features']['artifacts'] == artifacts
    assert outputs['features']['metrics'] == task['outputs']['features']['metrics']
    assert outputs['features']['rows'] == 200
    assert any(isinstance(outputs['features'][key], dict) and outputs['features'][key].get('preview_omitted')
               for key in ('preview', 'distribution'))


def test_native_training_keeps_candidate_comparison_without_fold_row_indices():
    trials = [{'slot': i, 'model': model, 'status': 'completed',
               'metrics': {'macro_f1': score}, 'baseline': {'macro_f1': .22},
               'warnings': ['Small class'], 'fold_metrics': [{'macro_f1': score - .1}],
               'fold_indices': list(range(3000))}
              for i, (model, score) in enumerate([('linear', .39), ('forest', .47)])]
    task = {'id': 'task', 'outputs': {'training': {
        'id': 'candidate', 'study_id': 'study', 'engine': 'sklearn', 'status': 'completed',
        'models': ['linear', 'forest'], 'features': {'exclude': ['batch']}, 'trials': trials}}}
    original = deepcopy(task)
    summary = task_summary(task)['outputs']['training']
    assert summary['view'] == 'summary'
    for expected, actual in zip(trials, summary['trials'], strict=True):
        for key in ('slot', 'model', 'status', 'metrics', 'baseline', 'warnings', 'fold_metrics'):
            assert actual[key] == expected[key]
        assert 'fold_indices' not in actual
    assert summary['detail']['arguments']['candidate_id'] == 'candidate'
    assert summary['features']['exclude'] == ['batch']
    assert payload_measurement(summary)['bytes'] < 3000
    assert task == original


def test_similarly_named_business_fields_still_allow_arbitrary_json():
    for engine, trials in [({'kind': 'custom'}, []), ('sklearn', [1, 'sample', None]),
                           ('sklearn', [{'group_errors': 7}])]:
        task = {'id': 'task', 'outputs': {'custom': {
            'id': 'business', 'study_id': 'reference', 'engine': engine,
            'trials': trials, 'text': 'x' * 10000}}}
        summary = task_summary(task)['outputs']['custom']
        assert summary['engine'] == engine and summary['trials'] == trials
