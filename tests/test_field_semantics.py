"""Data meaning survives real parsing, derived tables, training and saved prediction."""
import json
from pathlib import Path

import pytest

pd = pytest.importorskip('pandas')
np = pytest.importorskip('numpy')
from agent_platform import modeling_worker as worker
from agent_platform.modeling_models import DataAnalysisPlan, DataMapping


def table(tmp_path, suffix='csv'):
    data = pd.DataFrame({'id': ['001', '002', '003', '004', '005', '006'],
                         'height': ['6.2', '3.5-4.2', '/', '6..2', '', '100'],
                         'pressure': [1, 2, 3, 4, 5, 6], 'quality': ['0', '1', '2', '-1', '0', '2']})
    source = tmp_path / ('data.' + suffix)
    data.to_excel(source, index=False) if suffix == 'xlsx' else data.to_csv(source, index=False)
    mapping = DataMapping(id_column='id', target='quality', invalid_rows='exclude', field_rules={
        'height': {'dtype': 'number', 'unit': 'mm', 'pending_tokens': ['/'], 'allow_missing': ['unknown']},
        'pressure': {'dtype': 'number'},
        'quality': {'dtype': 'category', 'categories': ['0', '1', '2'], 'missing_tokens': {'-1': 'unjudged'}},
    }).model_dump()
    return {'source': str(source), 'mapping': mapping, 'output': str(tmp_path / 'out')}


@pytest.mark.parametrize('suffix', ['csv', 'xlsx'])
def test_raw_values_parse_status_and_meaning_are_distinct(tmp_path, suffix):
    config = table(tmp_path, suffix)
    original = Path(config['source']).read_bytes()
    frame = worker.read_data(config)
    cells = [r for r in frame.attrs['data_state']['cells'] if r['column'] == 'height']
    assert [c['status'] for c in cells] == ['valid', 'pending', 'pending', 'invalid', 'missing', 'valid']
    assert cells[1]['raw'] == '3.5-4.2' and cells[1]['interval'] == [3.5, 4.2]
    assert cells[2]['raw'] == '/' and cells[2]['blocked']
    assert not cells[4]['blocked'] and cells[4]['reason'] == 'unknown'
    assert frame.id.tolist() == ['001', '002', '003', '004', '005', '006']
    assert frame.height.iloc[-1] == 100  # An extreme measurement is not automatically an error.
    assert frame.quality.iloc[0] == '0' and pd.isna(frame.quality.iloc[3])
    assert Path(config['source']).read_bytes() == original


def test_analysis_uses_valid_pairs_and_rejects_unordered_category_codes(tmp_path):
    config = table(tmp_path)
    config['analysis'] = DataAnalysisPlan(operation='spearman', columns=['height', 'pressure']).model_dump()
    result = worker.analyze(config)
    assert result['pairs'][0]['samples'] == 2
    assert result['pairs'][0]['excluded_unusable_rows'] == 3
    assert result['data_state']['counts']['pending'] == 2
    config['analysis']['columns'] = ['quality', 'pressure']
    with pytest.raises(ValueError, match='数值'):
        worker.analyze(config)


def test_derived_tables_keep_original_values_and_row_locations(tmp_path):
    config = table(tmp_path)
    config['analysis'] = DataAnalysisPlan(operation='numeric', columns=['height']).model_dump()
    worker.analyze(config)
    derived = {**config, 'source': str(Path(config['output']) / 'processed.csv'),
               'state': str(Path(config['output']) / 'data-state.json'), 'output': str(tmp_path / 'next')}
    reread = worker.read_data(derived)
    assert worker.blocked_data_rows(reread, ['height']).keys() == {1, 2, 3}
    derived['analysis'] = DataAnalysisPlan(operation='outliers', columns=['pressure'], method='bounds', lower=2, handling='drop').model_dump()
    worker.analyze(derived)
    next_config = {**derived, 'source': str(tmp_path / 'next/processed.csv'), 'state': str(tmp_path / 'next/data-state.json')}
    cells = worker.read_data(next_config).attrs['data_state']['cells']
    interval = next(r for r in cells if r['raw'] == '3.5-4.2')
    assert interval['row'] == 0 and interval['source_row'] == 1 and interval['source_line'] == 3
    assert interval['status'] == 'pending'
    state = json.loads(Path(next_config['state']).read_text())
    state['cells'].pop()
    Path(next_config['state']).write_text(json.dumps(state))
    with pytest.raises(ValueError, match='状态缺少'):
        worker.read_data(next_config)


def test_known_missing_reason_requires_explicit_permission_and_interval_choice(tmp_path):
    config = table(tmp_path)
    rule = config['mapping']['field_rules']['height']
    rule.update(pending_tokens=[], missing_tokens={'/': 'not_measured'}, intervals='midpoint')
    frame = worker.read_data(config)
    assert frame.height.iloc[1] == pytest.approx(3.85)
    assert 2 in worker.blocked_data_rows(frame, ['height'])
    rule['allow_missing'].append('not_measured')
    allowed = worker.read_data(config)
    assert 2 not in worker.blocked_data_rows(allowed, ['height'])
    record = next(r for r in allowed.attrs['data_state']['cells'] if r['column'] == 'height' and r['row'] == 2)
    assert record['reason'] == 'not_measured' and record['raw'] == '/'


def test_real_training_and_saved_prediction_preserve_unknown_rows(tmp_path):
    pytest.importorskip('optuna')
    from tests.test_modeling_worker import fixture_data, setup_split
    config = fixture_data(tmp_path, classification=True)
    data = pd.read_csv(config['source'], dtype={'id': str, 'x': str})
    data.loc[3:5, 'x'] = ['3.5-4.2', '/', '6..2']
    data.loc[6, 'y'] = '-1'
    data.to_csv(config['source'], index=False)
    config['mapping'] = DataMapping(**config['mapping'], invalid_rows='exclude', field_rules={
        'x': {'pending_tokens': ['/'], 'allow_missing': ['unknown']},
        'y': {'dtype': 'category', 'categories': ['pass', 'reject'], 'missing_tokens': {'-1': 'unjudged'}},
    }).model_dump()
    config.update(engine='sklearn', models=['forest'], parameters={'n_estimators': 8}, batch_size=1)
    config['features'] = {'columns': ['x', 'category']}
    split = setup_split(config)
    assert split['missing_labels'] == 1 and split['excluded_data_rows'] == 3
    assert len(split['samples']) == 96 and not {3, 4, 5, 6} & set(split['samples'])
    trained = worker.train(config)
    best = trained['best']
    assert best is not None, trained
    predict = {**config, 'model': str(Path(config['output']) / 'trial-0'),
               'feature_columns': best['feature_columns'], 'classes': best['classes']}
    result = worker.predict(predict)
    assert result['rows'] == 100 and result['predicted_rows'] == 97 and result['unavailable_rows'] == 3
    output = pd.read_csv(Path(config['output']) / 'predictions.csv', dtype={'id': str})
    assert output.id.tolist() == data.id.tolist()
    assert output.loc[3:5, 'prediction'].isna().all()
    assert output.loc[3:5, 'prediction_reason'].str.len().gt(0).all()
    assert output.loc[6, 'prediction_status'] == 'predicted'  # Unknown target never blocks unlabeled inference.
    data.loc[7, 'x'] = 'new-marker'
    data.to_csv(config['source'], index=False)
    assert worker.predict(predict)['unavailable_rows'] == 4
    assert worker.fitted_preprocessing(__import__('joblib').load(Path(predict['model']) / 'model.joblib'), 0)['fill_values']['x'] is not None
    config['mapping']['invalid_rows'] = 'error'
    with pytest.raises(ValueError, match='训练特征存在待确认'):
        worker.prepare(config)


def test_changed_semantics_require_original_data(tmp_path):
    config = table(tmp_path)
    config['analysis'] = DataAnalysisPlan(operation='numeric', columns=['height']).model_dump()
    worker.analyze(config)
    config.update(source=str(Path(config['output']) / 'processed.csv'), state=str(Path(config['output']) / 'data-state.json'))
    config['mapping']['field_rules']['height']['unit'] = 'cm'
    with pytest.raises(ValueError, match='字段规则已改变'):
        worker.read_data(config)
