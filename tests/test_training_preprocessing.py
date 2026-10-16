"""Actual numeric fitting and saved prediction across all supported ML engines."""
import json
from pathlib import Path

import pytest

pd = pytest.importorskip('pandas')
np = pytest.importorskip('numpy')
pytest.importorskip('sklearn')
from agent_platform import modeling_worker as worker
from tests.test_modeling_worker import fixture_data


@pytest.mark.parametrize('missing,scaling,fill', [('median', 'standard', 2), ('mean', 'robust', 2.4), ('constant', 'none', -9)])
def test_explicit_numeric_steps_fit_training_values_and_preserve_rows(tmp_path, missing, scaling, fill):
    from sklearn.linear_model import Ridge
    x = pd.DataFrame({'x': [0., 1., 2., 3., 100., np.nan], 'category': ['a', 'b'] * 3})
    original = x.copy()
    config = {'engine': 'sklearn', 'features': {'preprocessing': {
        'numeric_missing': missing, 'fill_value': -9, 'scaling': scaling,
        'outliers': 'iqr_clip', 'outlier_columns': ['x']}}, 'evaluation': {'problem': 'regression'}}
    model = worker.pipeline(x, config, Ridge()).fit(x, np.arange(len(x)))
    fitted = worker.fitted_preprocessing(model, len(x))
    assert fitted['outlier_bounds']['x'] == {'lower': -2., 'upper': 6., 'valid_samples': 5}
    assert fitted['fill_values']['x'] == pytest.approx(fill)
    values = np.array([0, 1, 2, 3, 6, fill])
    if scaling == 'standard':
        assert fitted['center']['x'] == pytest.approx(values.mean())
        assert fitted['scale']['x'] == pytest.approx(values.std())
    elif scaling == 'robust':
        assert fitted['center']['x'] == pytest.approx(np.median(values))
        assert fitted['scale']['x'] == pytest.approx(np.quantile(values, .75) - np.quantile(values, .25))
    else:
        assert 'center' not in fitted and 'scale' not in fitted
    new = pd.DataFrame({'x': [10000., 6., np.nan], 'category': ['unseen'] * 3})
    transformed = model[:-1].transform(new)
    assert transformed.shape[0] == len(new)
    assert np.isfinite(transformed).all()
    np.testing.assert_array_equal(transformed[0], transformed[1])
    assert worker.fitted_preprocessing(model, len(x)) == fitted
    pd.testing.assert_frame_equal(x, original)


def test_missing_error_applies_to_fit_and_later_prediction():
    from sklearn.linear_model import Ridge
    config = {'engine': 'sklearn', 'features': {'preprocessing': {'numeric_missing': 'error'}}}
    x = pd.DataFrame({'x': [1., 2., 3.]})
    model = worker.pipeline(x, config, Ridge()).fit(x, [1, 2, 3])
    with pytest.raises(ValueError, match='缺失.*x'):
        model.predict(pd.DataFrame({'x': [np.nan]}))
    with pytest.raises(ValueError, match='缺失.*x'):
        worker.pipeline(x, config, Ridge()).fit(pd.DataFrame({'x': [1., np.nan, 3.]}), [1, 2, 3])


@pytest.mark.parametrize('columns', [[], ['absent'], ['category'], ['boolean']])
def test_iqr_requires_explicit_existing_numeric_columns(columns):
    clipper = worker.preprocessing_module().IQRClipper(columns=columns)
    with pytest.raises(ValueError, match='数值'):
        clipper.fit(pd.DataFrame({'x': [1, 2], 'category': ['a', 'b'], 'boolean': [True, False]}))


def test_all_missing_training_column_has_no_invented_iqr_bounds():
    from sklearn.linear_model import Ridge
    x = pd.DataFrame({'x': [np.nan] * 4})
    config = {'engine': 'sklearn', 'features': {'preprocessing': {
        'outliers': 'iqr_clip', 'outlier_columns': ['x'], 'numeric_missing': 'constant', 'fill_value': 7, 'scaling': 'none'}}}
    model = worker.pipeline(x, config, Ridge()).fit(x, [1, 2, 3, 4])
    details = worker.fitted_preprocessing(model, 4)
    assert details['outlier_bounds']['x'] == {'lower': None, 'upper': None, 'valid_samples': 0}
    assert details['fill_values']['x'] == 7
    np.testing.assert_array_equal(model[:-1].transform(x), np.full((4, 1), 7))


@pytest.mark.parametrize('engine', ['sklearn', 'optuna', 'autogluon'])
def test_each_engine_fits_only_fold_training_rows_and_reuses_saved_preprocessing(tmp_path, engine):
    import joblib
    if engine == 'autogluon':
        ag = pytest.importorskip('autogluon.tabular')
    config = fixture_data(tmp_path)
    raw = pd.read_csv(config['source'], dtype={'id': str})
    raw['x'] = [*range(40), *range(100, 140), *([1e8] * 20)]
    raw.loc[2, 'x'] = np.nan
    raw.to_csv(config['source'], index=False)
    split = {'samples': list(range(100)), 'development': list(range(80)), 'holdout': list(range(80, 100)),
             'folds': [[list(range(40)), list(range(40, 80))], [list(range(40, 80)), list(range(40))]]}
    path = tmp_path / 'split.json'; worker.save(path, split)
    config.update(engine=engine, models=['linear'], batch_size=1, split=str(path),
                  autogluon_hyperparameters={'RF': {'n_estimators': 8}} if engine == 'autogluon' else None)
    config['evaluation']['folds'] = 2
    config['features'] = {'columns': ['x', 'category'], 'preprocessing': {
        'numeric_missing': 'mean', 'scaling': 'robust', 'outliers': 'iqr_clip', 'outlier_columns': ['x'], 'iqr_factor': 1.5}}
    result = worker.train(config)
    assert result['best'], result
    folder = Path(config['output']) / 'trial-0'
    statistics = json.loads((folder / 'preprocessing.json').read_text())
    for part, indices in [(statistics['folds'][i], train) for i, (train, _) in enumerate(split['folds'])] + [(statistics['development'], split['development'])]:
        values = raw.x.iloc[indices]
        q1, q3 = values.quantile([.25, .75]); lower, upper = q1 - 1.5 * (q3-q1), q3 + 1.5 * (q3-q1)
        assert part['outlier_bounds']['x']['lower'] == pytest.approx(lower)
        assert part['outlier_bounds']['x']['upper'] == pytest.approx(upper)
        assert part['fill_values']['x'] == pytest.approx(values.clip(lower, upper).mean())
        assert part['rows'] == len(indices)
    assert statistics['development']['outlier_bounds']['x']['upper'] < 1000
    new = tmp_path / 'new.csv'
    new.write_text('id,batch,x,category\n001,a,20,a\n002,a,,b\n003,a,100000000,a\n')
    predict_config = {**config, 'source': str(new), 'model': str(folder), 'feature_columns': result['best']['feature_columns']}
    worker.preprocessing_module()
    x, _, _ = worker.features(predict_config, prediction=True)
    if engine == 'autogluon':
        fitted = joblib.load(folder / 'preprocessing.joblib')
        model = ag.TabularPredictor.load(str(folder / 'autogluon'))
        expected = model.predict(fitted.transform(x)).to_numpy()
    else:
        model = joblib.load(folder / 'model.joblib')
        expected = model.predict(x)
    before = (folder / 'preprocessing.json').read_bytes()
    prediction = worker.predict(predict_config)
    np.testing.assert_allclose([row['prediction'] for row in prediction['preview']], expected)
    assert prediction['rows'] == 3
    held = worker.holdout({**config, 'model': str(folder), 'feature_columns': result['best']['feature_columns'], 'classes': None})
    assert held['rows'] == 20
    assert (folder / 'preprocessing.json').read_bytes() == before
    if engine == 'autogluon':
        (folder / 'preprocessing.joblib').unlink()
        with pytest.raises(ValueError, match='缺少已拟合的预处理'):
            worker.predict(predict_config)
