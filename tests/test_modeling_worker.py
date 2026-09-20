"""Real-library modeling checks. Run in the locked modeling environment."""
from pathlib import Path

import pytest

pd = pytest.importorskip('pandas', reason='Run with the requirements-modeling.txt environment')
np = pytest.importorskip('numpy')
pytest.importorskip('optuna')
from agent_platform import modeling_worker as worker


def fixture_data(tmp_path, classification=False):
    rng = np.random.default_rng(12)
    x = rng.normal(size=100)
    y = np.where(x > 0, 'pass', 'reject') if classification else 4 * x + rng.normal(0, .1, size=100)
    data = pd.DataFrame({'id': [f'{i:04d}' for i in range(100)], 'batch': np.repeat(np.arange(20), 5), 'x': x, 'category': ['a', 'b'] * 50, 'y': y})
    data.loc[2, 'x'] = np.nan
    path = tmp_path / 'data.csv'; data.to_csv(path, index=False)
    output = tmp_path / 'out'; output.mkdir()
    return {'source': str(path), 'labels': '', 'mapping': {'kind': 'tabular', 'target': 'y', 'id_column': 'id', 'group_column': 'batch'},
            'evaluation': {'problem': 'classification' if classification else 'regression', 'metric': 'macro_f1' if classification else 'mae', 'split': 'group', 'folds': 3, 'seed': 42, 'holdout_fraction': .2},
            'features': {}, 'engine': 'optuna', 'models': ['linear', 'forest'], 'parameters': {}, 'batch_size': 2, 'trial_seconds': 60, 'remaining_seconds': 180, 'output': str(output)}


def setup_split(config):
    split = worker.prepare(config)
    path = Path(config['output']).parent / 'split.json'; worker.save(path, split)
    config['split'] = str(path)
    return split


def test_group_split_no_subject_overlap_and_holdout(tmp_path):
    config = fixture_data(tmp_path)
    split = setup_split(config)
    frame, _ = worker.samples(config)
    test_groups = set(frame.batch.iloc[split['holdout']])
    for train, valid in split['folds']:
        assert set(train + valid) <= set(split['development'])
        assert not set(frame.batch.iloc[train]) & set(frame.batch.iloc[valid])
        assert not (set(frame.batch.iloc[train]) | set(frame.batch.iloc[valid])) & test_groups
    config['evaluation']['split'] = 'random'
    with pytest.raises(ValueError, match='同组'):
        worker.prepare(config)


@pytest.mark.parametrize('classification', [False, True])
def test_real_search_persist_resume_and_unlabeled_prediction(tmp_path, classification):
    config = fixture_data(tmp_path, classification)
    setup_split(config)
    result = worker.train(config)
    assert result['best'], result
    assert result['best']['metrics'][config['evaluation']['metric']] is not None
    import joblib
    from datetime import datetime
    for t in result['trials']:
        model = joblib.load(Path(config['output']) / f'trial-{t["slot"]}/model.joblib')
        assert t['effective_model']['parameters'] == model.named_steps['model'].get_params()
        assert datetime.fromisoformat(t['completed_at']) >= datetime.fromisoformat(t['started_at'])
        assert t['search']['distributions']
        predictions = pd.read_csv(Path(config['output']) / f'trial-{t["slot"]}/validation-predictions.csv')
        if classification:
            from sklearn.metrics import roc_auc_score
            assert roc_auc_score(predictions.actual, predictions['probability:1']) == pytest.approx(t['metrics']['roc_auc'])
        else:
            assert (predictions.actual - predictions.baseline_prediction).abs().mean() == pytest.approx(t['baseline']['mae'])
        assert set(predictions.fold) == {1, 2, 3}
    if not classification:
        assert result['best']['metrics']['mae'] < result['best']['baseline']['mae']
    path = Path(config['output']) / 'trial-0' / 'result.json'
    mtime = path.stat().st_mtime_ns
    resumed = worker.train(config)
    assert path.stat().st_mtime_ns == mtime
    assert resumed['best'] == result['best']
    if classification:
        final = worker.holdout({**config, 'model': str(Path(config['output']) / f'trial-{result["best"]["slot"]}'),
                                'feature_columns': result['best']['feature_columns'], 'classes': result['best']['classes']})
        held = pd.read_csv(Path(config['output']) / 'holdout-predictions.csv')
        assert roc_auc_score(held.actual, held['probability:1']) == pytest.approx(final['metrics']['roc_auc'])
    frame = pd.read_csv(config['source'], dtype={'id': str}); frame = frame.drop(columns=['y'])
    frame.to_csv(tmp_path / 'unlabeled.csv', index=False)
    predict = {**config, 'source': str(tmp_path / 'unlabeled.csv'), 'model': str(Path(config['output']) / f'trial-{result["best"]["slot"]}'), 'feature_columns': result['best']['feature_columns']}
    prediction = worker.predict(predict)
    assert prediction['rows'] == len(frame)
    assert prediction['preview'][0]['id'] == '0000'


def test_difficult_samples_not_deleted_and_target_never_a_feature(tmp_path):
    config = fixture_data(tmp_path)
    config['features'] = {'columns': ['x', 'y']}
    with pytest.raises(ValueError, match='标签'):
        worker.features(config)
    config['features'] = {}
    frame = pd.read_csv(config['source']); frame.loc[1, 'y'] = np.nan; frame.to_csv(config['source'], index=False)
    split = worker.prepare(config)
    assert split['missing_labels'] == 1
    assert len(split['samples']) == 99
    assert 2 in split['samples']  # missing feature stays in evaluation


def test_temporal_windows_exclude_future_and_late_arrivals(tmp_path):
    path = tmp_path / 'process.csv'
    pd.DataFrame({'rod': ['01'] * 4, 'time': ['2026-01-01 00:00:00', '2026-01-01 00:01:00', '2026-01-01 00:02:00', '2026-01-01 00:03:00'],
                  'available': ['2026-01-01 00:00:00', '2026-01-01 00:01:00', '2026-01-02 00:00:00', '2026-01-01 00:03:00'], 'temperature': [1., 3., 900., 1000.]}).to_csv(path, index=False)
    labels = tmp_path / 'labels.csv'
    pd.DataFrame({'rod': ['01'] * 8, 'cutoff': ['2026-01-01 00:02:30'] * 8, 'y': range(8)}).to_csv(labels, index=False)
    config = {'source': str(path), 'labels': str(labels), 'mapping': {'kind': 'timeseries', 'target': 'y', 'id_column': 'rod', 'time_column': 'time', 'prediction_time_column': 'cutoff', 'available_time_column': 'available'}, 'features': {}}
    x, _, descriptions = worker.features(config)
    assert list(x.temperature__mean) == [2.] * 8
    assert all(d['source'] == 'temperature' for d in descriptions)
    config['features']['window_seconds'] = 1
    with pytest.raises(ValueError, match='没有可用测量'):
        worker.features(config)


def test_preprocessing_fits_only_training_rows(tmp_path):
    from sklearn.linear_model import Ridge
    config = fixture_data(tmp_path)
    x, frame, _ = worker.features(config)
    x.loc[80:, 'x'] = 1e9
    pipeline = worker.pipeline(x, config, Ridge())
    pipeline.fit(x.iloc[:80], frame.y.iloc[:80])
    fill = pipeline.named_steps['preprocess'].named_transformers_['numeric'].named_steps['fill']
    assert fill.statistics_[0] == pytest.approx(x.x.iloc[:80].median())
    assert abs(fill.statistics_[0]) < 2


def test_forward_validation_respects_windows_and_label_availability(tmp_path):
    config = fixture_data(tmp_path)
    frame = pd.read_csv(config['source']); dates = pd.date_range('2026-01-01', periods=len(frame), freq='h', tz='UTC')
    frame['time'], frame['available'] = dates, dates + pd.Timedelta(hours=4)
    frame['batch'] = 'one-machine'; frame.to_csv(config['source'], index=False)
    config['mapping'].update(prediction_time_column='time', label_available_time_column='available')
    config['evaluation'].update(split='time', gap_seconds=7200)
    split = worker.prepare(config)
    for train, valid in split['folds']:
        assert set(train + valid) <= set(split['development'])
        assert dates[train].max() + pd.Timedelta(hours=2) < dates[valid].min()
        assert (dates[train] + pd.Timedelta(hours=4)).max() < dates[valid].min()
    assert (dates[split['development']] + pd.Timedelta(hours=4)).max() < dates[split['holdout']].min()


@pytest.mark.parametrize('classification', [False, True])
def test_autogluon_compares_on_same_saved_rows(tmp_path, classification):
    ag = pytest.importorskip('autogluon.tabular')
    config = fixture_data(tmp_path, classification); split = setup_split(config)
    config.update(engine='autogluon', batch_size=1, trial_seconds=90)
    result = worker.train(config)
    assert result['best'], result
    predictions = pd.read_csv(Path(config['output']) / 'trial-0' / 'validation-predictions.csv')
    assert predictions['sample'].tolist() == [split['samples'][i] for _, valid in split['folds'] for i in valid]
    import json
    info = json.loads((Path(config['output']) / 'trial-0/autogluon-models.json').read_text())
    effective = result['best']['effective_model']
    assert effective['selected_model'] in info['model_info']
    assert effective['parameters'] == info['model_info'][effective['selected_model']]
    expected_metric = 'f1_macro' if classification else 'mean_absolute_error'
    for name in ['autogluon', *[f'ag-fold-{i}' for i in range(len(split['folds']))]]:
        predictor = ag.TabularPredictor.load(str(Path(config['output']) / 'trial-0' / name))
        assert predictor.eval_metric.name == expected_metric
        assert predictor.problem_type == ('binary' if classification else 'regression')


def test_numeric_time_units_are_explicit_and_preview_is_real(tmp_path):
    path = tmp_path / 'data.tsv'
    pd.DataFrame({'rod': ['01'] * 3, 'time': [1000, 2000, 3000], 'temperature': [2, 4, 6]}).to_csv(path, sep='\t', index=False)
    config = {'source': str(path), 'mapping': {'id_column': 'rod', 'time_column': 'time'}}
    with pytest.raises(ValueError, match='time_unit'):
        worker.profile(config)
    config['mapping']['time_unit'] = 'ms'
    result = worker.profile(config)
    assert result['time']['median_interval_seconds'] == 1
    assert [p['value'] for p in result['time']['preview']['points']] == [2, 4, 6]
    assert '1970-01-01 00:00:01' in result['time']['start']


def test_excel_sheet_and_string_identifiers_are_preserved(tmp_path):
    pytest.importorskip('openpyxl')
    path = tmp_path / 'input.xlsx'
    pd.DataFrame({'id': ['001', '002'], 'value': [3, 5]}).to_excel(path, sheet_name='measurement', index=False)
    table = worker.read_table(path, {'sheet': 'measurement', 'id_column': 'id'})
    assert table.id.tolist() == ['001', '002']
    assert table.value.tolist() == [3, 5]


def test_partial_batch_resumes_only_unfinished_slots(tmp_path, monkeypatch):
    config = fixture_data(tmp_path); setup_split(config)
    emit = worker.emit
    def interrupt(kind, **value):
        if kind == 'trial_started' and value['slot'] == 1:
            raise KeyboardInterrupt('Interrupted before second trial')
    monkeypatch.setattr(worker, 'emit', interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.train(config)
    saved = Path(config['output']) / 'trial-0/result.json'
    original = saved.read_bytes(), saved.stat().st_mtime_ns
    monkeypatch.setattr(worker, 'emit', emit)
    result = worker.train(config)
    assert saved.read_bytes() == original[0]
    assert saved.stat().st_mtime_ns == original[1]
    assert len(result['trials']) == 2
    assert all(t['status'] == 'completed' for t in result['trials'])
    assert list((Path(config['output']) / 'interrupted-attempts').glob('trial-1-*/attempt.json'))


def test_timeout_terminates_training_that_ignores_alarm(tmp_path):
    import time
    config = fixture_data(tmp_path); setup_split(config)
    code = tmp_path / 'slow.py'
    code.write_text('import signal,time\nfrom sklearn.preprocessing import FunctionTransformer\ndef slow(x):\n signal.signal(signal.SIGALRM, signal.SIG_IGN)\n time.sleep(30)\n return x\ndef build_transformer():\n return FunctionTransformer(slow, validate=False)\n')
    config.update(transformer=str(code), engine='sklearn', models=['linear'], batch_size=1, trial_seconds=5)
    begin = time.monotonic()
    result = worker.train(config)
    assert time.monotonic() - begin < 15
    assert result['best'] is None
    assert result['trials'][0]['status'] == 'failed'
    assert '计算进程已终止' in result['trials'][0]['error']
    assert not (Path(config['output']) / 'trial-0/model.joblib').exists()


def test_small_sample_trees_learn_and_explicit_constant_configuration_is_reported(tmp_path):
    config = fixture_data(tmp_path)
    frame = pd.DataFrame({'id': range(32), 'x': np.linspace(-2, 2, 32)})
    frame['y'] = frame.x * 3
    frame.to_csv(config['source'], index=False)
    config.update(mapping={'kind': 'tabular', 'target': 'y', 'id_column': 'id'}, engine='sklearn', models=['hist_gradient'], batch_size=1)
    config['evaluation'].update(split='random', folds=3)
    setup_split(config)
    result = worker.train(config)['best']
    assert result and result['metrics']['mae'] < result['baseline']['mae']
    assert all(d['unique_predictions'] > 1 for d in result['diagnostics'])
    assert result['effective_model']['parameters']['min_samples_leaf'] < 20
    explicit = Path(config['output']).parent / 'explicit'; explicit.mkdir()
    config.update(output=str(explicit), parameters={'min_samples_leaf': 20})
    constant = worker.train(config)['best']
    assert constant['effective_model']['parameters']['min_samples_leaf'] == 20
    assert all(d['constant_prediction'] for d in constant['diagnostics'])
    assert constant['warnings'] and constant['metrics']['mae'] is not None


def test_svm_search_configuration_is_fitted_and_saved(tmp_path):
    config = fixture_data(tmp_path); setup_split(config)
    config.update(models=['svm'], batch_size=1, parameters={'kernel': 'rbf'}, search_space={'svm': {
        'C': {'type': 'float', 'low': 2, 'high': 2},
        'gamma': {'type': 'categorical', 'choices': [.03]},
        'epsilon': {'type': 'float', 'low': .04, 'high': .04}}})
    result = worker.train(config)['best']
    assert result, result
    assert result['parameters'] == {'C': 2., 'gamma': .03, 'epsilon': .04}
    assert result['effective_model']['parameters']['kernel'] == 'rbf'
    for k, v in result['parameters'].items():
        assert result['effective_model']['parameters'][k] == v
    assert set(result['search']['distributions']) == {'C', 'gamma', 'epsilon'}
    import optuna
    trial = optuna.create_study().ask()
    worker.estimator('svm', {**config, 'search_space': {}, 'parameters': {'kernel': 'rbf'}}, trial)
    assert {'C', 'gamma', 'epsilon'} == set(trial.params)


def test_autogluon_cpu_models_and_small_sample_parameters_are_used(tmp_path):
    ag = pytest.importorskip('autogluon.tabular')
    config = fixture_data(tmp_path)
    data = pd.read_csv(config['source']).iloc[:32]; data.to_csv(config['source'], index=False)
    config.update(mapping={'kind': 'tabular', 'target': 'y', 'id_column': 'id'}, engine='autogluon', batch_size=1)
    config['evaluation'].update(split='random')
    setup_split(config)
    result = worker.train(config)['best']
    assert result, result
    assert set(result['effective_model']['fit_hyperparameters']) == {'GBM', 'RF', 'XT'}
    assert any(d['unique_predictions'] > 1 for d in result['diagnostics'])
    predictor = ag.TabularPredictor.load(str(Path(config['output']) / 'trial-0/autogluon'))
    assert any('RandomForest' in m for m in predictor.model_names())
    assert any('ExtraTrees' in m for m in predictor.model_names())
    parameters = {'GBM': [{'min_data_in_leaf': 7}, {'min_child_samples': 4}]}
    assert worker.autogluon_parameters({**config, 'autogluon_hyperparameters': parameters}) == parameters
