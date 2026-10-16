"""Real statistical calculations, runnable in the offline modeling image."""
import json
from pathlib import Path

import pytest

pd = pytest.importorskip('pandas')
pytest.importorskip('scipy')
from agent_platform import modeling_worker as worker


def job(tmp_path, frame, **analysis):
    source = tmp_path / 'source.csv'
    frame.to_csv(source, index=False)
    return {'source': str(source), 'output': str(tmp_path / 'output'),
            'mapping': {'kind': 'tabular', 'id_column': 'id'},
            'analysis': {'operation': 'spearman', 'columns': ['x', 'y'],
                         'missing': 'pairwise', 'method': 'iqr', 'handling': 'flag',
                         'iqr_factor': 1.5, 'lower': None, 'upper': None, **analysis}}


def test_spearman_ties_missing_and_export(tmp_path):
    from scipy.stats import spearmanr
    config = job(tmp_path, pd.DataFrame({'x': [1., 2., 2., 4., 5.], 'y': [9., 7., 6., None, 1.]}))
    result = worker.analyze(config)
    expected = spearmanr([1, 2, 2, 5], [9, 7, 6, 1])
    assert result['pairs'][0]['rho'] == pytest.approx(expected.statistic)
    assert result['pairs'][0]['p_value'] == pytest.approx(expected.pvalue)
    assert result['sample_counts'] == [[5, 4], [4, 4]]
    assert result['missing_counts'] == {'x': 0, 'y': 1}
    saved = json.loads((Path(config['output']) / 'analysis.json').read_text())
    assert saved == result
    assert pd.read_csv(Path(config['output']) / 'correlations.csv').samples.tolist() == [4]


def test_complete_case_and_undefined_pairs(tmp_path):
    config = job(tmp_path, pd.DataFrame({'x': [1., 2., 3., 4.], 'y': [4., 3., 2., 1.],
                                        'z': [8., 8., None, 8.]}), columns=['x', 'y', 'z'])
    pairwise = worker.analyze(config)
    assert pairwise['sample_counts'][0][1] == 4
    assert pairwise['pairs'][1]['rho'] is None
    assert pairwise['pairs'][1]['reason'] == '包含常数列'
    config['analysis']['missing'] = 'complete'
    assert worker.analyze(config)['sample_counts'][0][1] == 3
    config['analysis']['missing'] = 'raise'
    with pytest.raises(ValueError, match='缺失值'):
        worker.analyze(config)


@pytest.mark.parametrize('handling', ['flag', 'drop', 'clip'])
def test_outliers_preserve_original_ids_missing_and_row_mapping(tmp_path, handling):
    frame = pd.DataFrame({'id': ['001', '002', '003', '004', '005', '006'],
                          'x': [1., 2., 3., 4., 100., None], 'other': ['a'] * 6})
    config = job(tmp_path, frame, operation='outliers', columns=['x'], handling=handling)
    before = Path(config['source']).read_bytes()
    result = worker.analyze(config)
    assert result['affected_rows'] == result['affected_cells'] == 1
    assert result['bounds']['x'] == {'lower': -1., 'upper': 7.}
    assert Path(config['source']).read_bytes() == before
    folder = Path(config['output'])
    assert pd.read_csv(folder / 'outliers.csv').source_row.tolist() == [4]
    assert pd.read_csv(folder / 'source-rows.csv').source_row.tolist() == ([0, 1, 2, 3, 5] if handling == 'drop' else list(range(6)))
    if handling == 'flag':
        assert not (folder / 'processed.csv').exists()
    else:
        output = pd.read_csv(folder / 'processed.csv', dtype={'id': str})
        assert output.id.iloc[0] == '001' and pd.isna(output.x.iloc[-1])
        assert len(output) == (5 if handling == 'drop' else 6)
        if handling == 'clip':
            assert output.x.iloc[4] == 7


def test_one_sided_bounds_and_all_rows_removed(tmp_path):
    config = job(tmp_path, pd.DataFrame({'x': [1, 2, 10]}), operation='outliers', columns=['x'],
                 method='bounds', upper=3, handling='drop')
    assert worker.analyze(config)['output_rows'] == 2
    config['analysis']['upper'] = 0
    with pytest.raises(ValueError, match='全部样本'):
        worker.analyze(config)


@pytest.mark.parametrize('column,values,message', [
    ('missing', [1, 2, 3], '不存在'), ('x', ['a', 'b', 'c'], '数值'),
    ('x', [1., float('inf'), 3.], '无穷'), ('x', [True, False, True], '数值'),
])
def test_rejects_incompatible_numeric_inputs(tmp_path, column, values, message):
    config = job(tmp_path, pd.DataFrame({'x': values, 'y': [3, 2, 1]}), columns=[column, 'y'])
    with pytest.raises(ValueError, match=message):
        worker.analyze(config)


def test_small_samples_are_explicit_and_never_nan_json(tmp_path):
    result = worker.analyze(job(tmp_path, pd.DataFrame({'x': [1., 2.], 'y': [2., 1.]})))
    assert result['pairs'][0]['status'] == 'undefined'
    assert result['pairs'][0]['samples'] == 2
    assert result['pairs'][0]['rho'] is None
    json.dumps(result, allow_nan=False)
