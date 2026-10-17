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
        'quality': {'dtype': 'category', 'categories': ['0', '1', '2'], 'category_labels': {'0': '合格', '1': '压连不足', '2': '过压损伤', '-1': '未判定'}, 'missing_tokens': {'-1': 'unjudged'}},
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
    data.loc[8, ['x', 'category']] = None
    data.loc[9, 'x'] = None  # A partial, permitted missing value is still usable.
    data['unused_measurement'] = 'not yet understood'
    data['filter_measurement'] = np.arange(len(data))
    data.to_csv(config['source'], index=False)
    config['mapping'] = DataMapping(**config['mapping'], invalid_rows='exclude', field_rules={
        'x': {'pending_tokens': ['/'], 'allow_missing': ['unknown']},
        'y': {'dtype': 'category', 'categories': ['pass', 'reject'], 'category_labels': {'pass': '合格', 'reject': '缺陷', '-1': '未判定'}, 'missing_tokens': {'-1': 'unjudged'}},
        'unused_measurement': {'dtype': 'text', 'meaning': 'pending'},
    }).model_dump()
    config.update(engine='sklearn', models=['forest'], parameters={'n_estimators': 8}, batch_size=1)
    config['features'] = {'columns': ['x', 'category']}
    split = setup_split(config)
    assert split['missing_labels'] == 1 and split['excluded_data_rows'] == 4
    assert len(split['samples']) == 95 and not {3, 4, 5, 6, 8} & set(split['samples'])
    trained = worker.train(config)
    best = trained['best']
    assert best is not None, trained
    assert {row['label']: row['name'] for row in best['classification']['classes']} == {'pass': '合格', 'reject': '缺陷'}
    assert split['label_state_counts']['missing'] == 1
    assert split['unavailable_labels'] == 1
    predict = {**config, 'model': str(Path(config['output']) / 'trial-0'),
               'feature_columns': best['feature_columns'], 'classes': best['classes']}
    result = worker.predict(predict)
    assert result['rows'] == 100 and result['predicted_rows'] == 96 and result['unavailable_rows'] == 4
    output = pd.read_csv(Path(config['output']) / 'predictions.csv', dtype={'id': str})
    assert output.id.tolist() == data.id.tolist()
    valid = output.prediction_status == 'predicted'
    assert output.loc[valid, 'prediction'].map({'pass': '合格', 'reject': '缺陷'}).tolist() == output.loc[valid, 'prediction_name'].tolist()
    assert output.loc[~valid, 'prediction_name'].isna().all()
    assert output.loc[3:5, 'prediction'].isna().all()
    assert output.loc[3:5, 'prediction_reason'].str.len().gt(0).all()
    assert output.loc[6, 'prediction_status'] == 'predicted'  # Unknown target never blocks unlabeled inference.
    assert output.loc[8, 'prediction_status'] == 'unavailable'
    assert '所有选用特征' in output.loc[8, 'prediction_reason']
    assert output.loc[9, 'prediction_status'] == 'predicted'
    derived_dir = tmp_path / 'derived'
    worker.analyze({**config, 'output': str(derived_dir), 'analysis': DataAnalysisPlan(
        operation='outliers', columns=['filter_measurement'], method='bounds', lower=5, handling='drop').model_dump()})
    derived_prediction = {**predict, 'source': str(derived_dir / 'processed.csv'),
                          'state': str(derived_dir / 'data-state.json'), 'output': str(derived_dir)}
    worker.predict(derived_prediction)
    derived_rows = pd.read_csv(derived_dir / 'predictions.csv', dtype={'id': str})
    assert derived_rows.source_row.tolist() == list(range(5, 100))
    assert derived_rows.source_line.tolist() == list(range(7, 102))
    assert derived_rows.id.tolist() == data.id.tolist()[5:]
    data.loc[7, 'x'] = 'new-marker'
    data.to_csv(config['source'], index=False)
    assert worker.predict(predict)['unavailable_rows'] == 5
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


def test_profile_and_analysis_do_not_call_pending_or_invalid_cells_missing(tmp_path):
    from agent_platform.modeling_summary import profile_summary
    config = table(tmp_path)
    result = profile_summary(worker.profile(config))
    height = next(c for c in result['columns'] if c['name'] == 'height')
    assert height['counts'] == dict(valid=2, converted=0, missing=1, pending=2, invalid=1)
    assert sum(height['counts'].values()) == result['rows'] == 6
    assert height['missing'] == 1 and height['parsed_unavailable'] == 4
    assert height['missing_reasons'] == {'unknown': 1}
    assert height['invalid_reasons'] == {'数值格式错误': 1}
    quality = next(c for c in result['columns'] if c['name'] == 'quality')
    assert {v['value']: v['name'] for v in quality['value_counts']} == {'0': '合格', '1': '压连不足', '2': '过压损伤'}
    assert quality['missing_reasons'] == {'unjudged': 1}
    for operation in ('numeric', 'spearman', 'outliers'):
        analysis = worker.analyze({**config, 'analysis': DataAnalysisPlan(operation=operation, columns=['height', 'pressure']).model_dump()})
        assert analysis['missing_counts']['height'] == 1
        assert analysis['parsed_unavailable_counts']['height'] == 4
        assert analysis['data_state']['columns']['height']['counts'] == height['counts']
        report = Path(config['output'], 'data-summary.md').read_text()
        assert '| height | 2 | 0 | 1 | 2 | 1 | 4 |' in report
        assert '| quality | 0 | 合格 |' in report
        assert '| quality | -1 | 未判定 |' in report


def test_unresolved_only_rows_are_not_reported_as_empty(tmp_path):
    source = tmp_path / 'unknown.csv'
    source.write_text('height\n/\n3.5-4.2\nbad\n""\n')
    result = worker.profile({'source': str(source), 'mapping': DataMapping(field_rules={
        'height': {'pending_tokens': ['/']}}).model_dump(), 'output': str(tmp_path / 'out')})
    assert result['rows'] == 4 and result['empty_rows'] == 1
    assert result['columns'][0]['missing'] == 1


def test_category_name_contract_and_training_report_keep_business_meaning():
    from agent_platform.training_notes import classification_markdown
    from agent_platform.modeling_models import FieldRule
    with pytest.raises(ValueError, match='类别名称'):
        FieldRule(dtype='category', categories=['0'], category_labels={'1': '合格'})
    result = worker.classification_details(['0', '1', '2'], ['1', '1', '1'], ['0', '1', '2'],
                                           {'0': '合格', '1': '压连不足', '2': '过压损伤'})
    report = classification_markdown(result)
    assert '| 0（合格） |' in report and '| 2（过压损伤） |' in report
    assert result['classes'][0]['recall'] == 0
    unknown = worker.classification_details(['0', '1'], ['0', '0'], ['0', '1'], {'0': '合格'})
    assert unknown['classes'][1]['name'] == '含义待确认'


def test_state_report_tracks_processed_values_and_ordered_category_names(tmp_path):
    config = table(tmp_path)
    config['mapping']['field_rules']['quality']['ordered_values'] = ['0', '1', '2']
    profile = worker.profile(config)
    quality = next(c for c in profile['columns'] if c['name'] == 'quality')
    assert next(v for v in quality['value_counts'] if v['value'] == 0)['name'] == '合格'
    assert worker.category_labels(config)['0.0'] == '合格'
    worker.analyze({**config, 'analysis': DataAnalysisPlan(operation='outliers', columns=['height'],
        method='bounds', upper=10, handling='clip').model_dump()})
    report = Path(config['output'], 'data-summary.md').read_text()
    assert '| height | 1 | 1 | 1 | 2 | 1 | 4 |' in report
