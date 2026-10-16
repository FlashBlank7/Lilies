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
    assert 'preview' not in summary
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
