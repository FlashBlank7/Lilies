"""Group diagnostics use the same labeled population as model evaluation."""
import pytest

pd = pytest.importorskip('pandas')
from agent_platform import modeling_worker as worker
from agent_platform.modeling_summary import profile_summary


def test_profile_distinguishes_labeled_repeats_from_unlabeled_rows_and_missing_groups(tmp_path):
    source = tmp_path / 'data.csv'
    pd.DataFrame({'chip': ['001', '001', '002', '003', '003', None, None],
                  'x': range(7), 'y': [0, 1, 1, 0, None, 1, None]}).to_csv(source, index=False)
    result = worker.profile({'source': str(source), 'mapping': {'target': 'y', 'id_column': 'chip'}})
    assert result['duplicates'] == 0
    assert result['sample_structure'] == {'target': 'y', 'labeled_rows': 5, 'missing_labels': 2,
        'group_column': 'chip', 'groups': 3, 'repeated_groups': 1, 'rows_in_repeated_groups': 2, 'missing_groups': 1}
    summary = profile_summary(result)
    assert summary['sample_structure'] == result['sample_structure']
    assert 'chip' in summary['warnings'][0] and 'evaluation.split="group"' in summary['warnings'][0]
    assert summary['preview'] == {'preview_omitted': True, 'count': len(result['preview'])}
    assert result['preview'][0]['chip'] == '001'


def test_preview_does_not_claim_to_have_checked_unseen_repeated_samples(tmp_path):
    source = tmp_path / 'data.csv'
    pd.DataFrame({'chip': [str(i) for i in range(1000)] + ['0', '1'], 'y': [1] * 1002}).to_csv(source, index=False)
    config = {'source': str(source), 'mapping': {'target': 'y', 'group_column': 'chip'}}
    preview = worker.profile({**config, 'sampled': True})
    full = worker.profile(config)
    assert preview['sampled'] and preview['rows'] == 1000
    assert preview['sample_structure']['repeated_groups'] == 0
    assert not full['sampled'] and full['sample_structure']['repeated_groups'] == 2
    assert full['sample_structure']['rows_in_repeated_groups'] == 4


def test_independent_labels_define_sample_groups_not_measurement_rows(tmp_path):
    source, labels = tmp_path / 'measurements.csv', tmp_path / 'labels.csv'
    pd.DataFrame({'chip': ['a', 'a', 'b', 'b'], 'x': [1, 2, 3, 4]}).to_csv(source, index=False)
    pd.DataFrame({'chip': ['a', 'b'], 'y': [0, 1]}).to_csv(labels, index=False)
    result = worker.profile({'source': str(source), 'labels': str(labels),
        'mapping': {'target': 'y', 'id_column': 'chip'}})
    assert 'sample_structure' not in result
    assert result['labels']['sample_structure']['repeated_groups'] == 0
    assert profile_summary(result)['labels']['sample_structure']['labeled_rows'] == 2


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_profile_counts_boolean_flags_without_numeric_quantiles(tmp_path, extension):
    source = tmp_path / f'data.{extension}'
    frame = pd.DataFrame({'flag': [True, False, True, True, False, True],
                          'value': [1, 2, 3, 4, 5, 100]})
    if extension == 'xlsx':
        pytest.importorskip('openpyxl')
        frame.to_excel(source, index=False)
    else:
        frame.to_csv(source, index=False)

    result = worker.profile({'source': str(source), 'mapping': {}})

    flag, value = result['columns']
    assert result['rows'] == 6
    assert flag == {'name': 'flag', 'dtype': 'bool', 'missing': 0, 'unique': 2,
                    'value_counts': [{'value': True, 'count': 4}, {'value': False, 'count': 2}],
                    'distribution': [{'label': 'True', 'count': 4}, {'label': 'False', 'count': 2}]}
    assert value['min'] == 1 and value['max'] == 100
    assert value['mean'] == pytest.approx(115 / 6)
    assert value['outliers'] == 1
    assert sum(item['count'] for item in value['distribution']) == 6
    assert result['preview'][0]['flag'] is True


def test_profile_preserves_missing_boolean_flags(tmp_path):
    source = tmp_path / 'data.csv'
    pd.DataFrame({'flag': [True, None, False]}).to_csv(source, index=False)

    result = worker.profile({'source': str(source), 'mapping': {}})

    flag = result['columns'][0]
    assert flag['missing'] == 1 and flag['unique'] == 2
    assert {item['label']: item['count'] for item in flag['distribution']} == {'True': 1, 'False': 1}
    assert 'mean' not in flag and 'outliers' not in flag
    assert result['preview'][1]['flag'] is None


def test_profile_summary_keeps_exact_flag_counts_and_empty_rows(tmp_path):
    source = tmp_path / 'data.csv'
    pd.DataFrame({'original': [1, 0, 0, None], 'filled': [0, 1, 1, None],
                  'date': ['2026-01-01'] * 3 + [None]}).to_csv(source, index=False)
    result = profile_summary(worker.profile({'source': str(source), 'mapping': {}}))
    assert (result['rows'], result['empty_rows'], result['nonempty_rows']) == (4, 1, 3)
    original, filled = result['columns'][:2]
    assert {row['value']: row['count'] for row in original['value_counts']} == {0: 2, 1: 1}
    assert {row['value']: row['count'] for row in filled['value_counts']} == {0: 1, 1: 2}
    assert original['missing'] == filled['missing'] == 1
