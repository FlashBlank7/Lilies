"""CPU worker. JSON in, JSONL events out; no platform database or agent credentials.

This file is copied into the modeling image. Metrics are computed here from
predictions, never accepted from an agent. Each completed trial is committed
atomically before its completion event is emitted.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item'):
        return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(clean(value), ensure_ascii=False, allow_nan=False))
    os.replace(temp, path)


def emit(kind, **value):
    print(json.dumps(clean({'kind': kind, **value}), ensure_ascii=False, allow_nan=False), flush=True)


def read_table(path, mapping, preview=False):
    import pandas as pd
    keys = [mapping.get(k) for k in ('id_column', 'group_column') if mapping.get(k)]
    if Path(path).suffix.lower() == '.xlsx':
        return pd.read_excel(path, sheet_name=mapping.get('sheet', 0), dtype={k: str for k in keys}, nrows=1000 if preview else None)
    return pd.read_csv(path, sep='\t' if Path(path).suffix.lower() == '.tsv' else ',', dtype={k: str for k in keys}, nrows=1000 if preview else None)


def timestamps(value, mapping, errors='raise'):
    import numbers
    import pandas as pd
    unit = mapping.get('time_unit', 'iso')
    numeric = isinstance(value, numbers.Number) or (hasattr(value, 'dtype') and pd.api.types.is_numeric_dtype(value))
    if numeric and unit == 'iso':
        raise ValueError('数值时间需要明确 time_unit（s／ms／us／ns），不能猜测采样单位')
    return pd.to_datetime(value, utc=True, errors=errors, **({'unit': unit} if numeric else {}))


def profile(config):
    import numpy as np
    import pandas as pd
    mapping = config['mapping']
    frame = read_table(config['source'], mapping, config.get('sampled', False))
    columns = []
    for name in frame.columns:
        series = frame[name]
        info = {'name': str(name), 'dtype': str(series.dtype), 'missing': int(series.isna().sum()), 'unique': int(series.nunique())}
        if pd.api.types.is_numeric_dtype(series):
            finite = series.replace([np.inf, -np.inf], np.nan).dropna()
            if len(finite):
                q1, q3 = finite.quantile([.25, .75])
                counts, bins = np.histogram(finite, bins=min(20, max(1, finite.nunique())))
                info.update(min=float(finite.min()), max=float(finite.max()), mean=float(finite.mean()),
                            outliers=int(((finite < q1 - 1.5 * (q3 - q1)) | (finite > q3 + 1.5 * (q3 - q1))).sum()),
                            distribution=[{'label': f'{bins[i]:.4g}–{bins[i+1]:.4g}', 'count': int(n)} for i, n in enumerate(counts)])
        else:
            info['distribution'] = [{'label': str(k), 'count': int(v)} for k, v in series.value_counts().head(12).items()]
        columns.append(info)
    result = {'sampled': config.get('sampled', False), 'rows': len(frame), 'duplicates': int(frame.duplicated().sum()), 'columns': columns,
              'preview': frame.head(12).astype(object).where(pd.notna(frame.head(12)), None).to_dict('records')}
    time_col = mapping.get('time_column')
    if time_col and time_col in frame:
        stamp = timestamps(frame[time_col], mapping, errors='coerce')
        intervals = frame.assign(_stamp=stamp).sort_values('_stamp')
        key = mapping.get('id_column') or mapping.get('group_column')
        delta = intervals.groupby(key)._stamp.diff() if key else intervals._stamp.diff()
        result['time'] = {'start': str(stamp.min()), 'end': str(stamp.max()), 'invalid': int(stamp.isna().sum()),
                          'median_interval_seconds': delta.dt.total_seconds().median()}
        values = [c for c in frame.select_dtypes(include='number') if c != time_col and c != mapping.get('target')]
        if values and len(intervals):
            preview = intervals.loc[intervals[key] == intervals[key].iloc[0]] if key else intervals
            preview = preview.iloc[::max(1, len(preview) // 120)].head(120)
            result['time']['preview'] = {'sampled': True, 'series': values[0], 'group': str(preview[key].iloc[0]) if key and len(preview) else '',
                'points': [{'time': str(row['_stamp']), 'value': row[values[0]]} for _, row in preview.iterrows()]}
    for key in ('id_column', 'group_column'):
        if mapping.get(key) in frame:
            result[key + '_count'] = int(frame[mapping[key]].nunique())
    if config.get('labels'):
        result['labels'] = profile({**config, 'source': config['labels'], 'labels': ''})
    return clean(result)


def samples(config, prediction=False):
    import numpy as np
    import pandas as pd
    mapping = config['mapping']
    frame = read_table(config.get('labels') or config['source'], mapping)
    frame['_sample'] = np.arange(len(frame))
    target = mapping.get('target')
    missing = 0
    if not prediction:
        if not target or target not in frame:
            raise ValueError('请指定实际存在的目标列；时序目标须在标签表中')
        missing = int(frame[target].isna().sum())
        frame = frame.loc[frame[target].notna()].copy()
        if len(frame) < 8:
            raise ValueError('至少需要 8 个有标签样本；当前标签不足')
        if config.get('evaluation', {}).get('problem') == 'regression':
            frame[target] = pd.to_numeric(frame[target], errors='raise')
            if not np.isfinite(frame[target]).all():
                raise ValueError('回归标签包含无穷值')
    for key in ('id_column', 'group_column'):
        col = mapping.get(key)
        if col and (col not in frame or frame[col].isna().any()):
            raise ValueError(f'{key} 缺失，不能静默丢弃这些样本')
    return frame.reset_index(drop=True), missing


def prepare(config):
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import GroupKFold, GroupShuffleSplit, KFold, StratifiedKFold, train_test_split, TimeSeriesSplit
    frame, missing = samples(config)
    rule, mapping = config['evaluation'], config['mapping']
    y = frame[mapping['target']]
    indices = np.arange(len(frame))
    group = mapping.get('group_column') or mapping.get('id_column')
    if group and frame[group].duplicated().any() and rule['split'] == 'random':
        raise ValueError('存在同组重复样本，请使用按组划分或时间向前验证')
    if mapping['kind'] == 'timeseries' and rule['split'] == 'random':
        raise ValueError('工艺时序需按组划分或时间向前验证')
    fraction = rule['holdout_fraction']
    holdout = np.array([], dtype=int)
    if rule['split'] == 'group':
        if not group:
            raise ValueError('按组验证需要 group_column 或时序 id_column')
        if fraction:
            indices, holdout = next(GroupShuffleSplit(n_splits=1, test_size=fraction, random_state=rule['seed']).split(indices, y, frame[group]))
        splitter = GroupKFold(n_splits=rule['folds'])
        folds = [(indices[a], indices[b]) for a, b in splitter.split(indices, y.iloc[indices], frame[group].iloc[indices])]
    elif rule['split'] == 'time':
        col = mapping.get('prediction_time_column') or mapping.get('time_column')
        if not col:
            raise ValueError('时间验证需要预测时点列')
        stamps = timestamps(frame[col], mapping)
        if stamps.isna().any():
            raise ValueError('预测时点不得缺失')
        # Split unique timestamps: equal-time observations cannot cross a boundary.
        unique = np.sort(stamps.unique())
        if fraction:
            boundary = unique[max(1, int(len(unique) * (1 - fraction)))]
            holdout = np.flatnonzero(stamps >= boundary)
            indices = np.flatnonzero(stamps < boundary)
        availability = mapping.get('label_available_time_column')
        available = timestamps(frame[availability], mapping) if availability else stamps
        if available.isna().any():
            raise ValueError('标签可用时间缺失，无法确认未来信息隔离')
        # Purge the final-test boundary before constructing any search fold.
        # Otherwise validation windows could overlap the supposedly untouched test.
        if len(holdout):
            cutoff = stamps.iloc[holdout].min()
            indices = indices[(available.iloc[indices] < cutoff).to_numpy()]
            if rule.get('gap_seconds'):
                indices = indices[(stamps.iloc[indices] < cutoff - pd.Timedelta(seconds=rule['gap_seconds'])).to_numpy()]
            elif group:
                indices = indices[~frame[group].iloc[indices].isin(frame[group].iloc[holdout]).to_numpy()]
        unique = np.sort(stamps.iloc[indices].unique())
        folds = []
        for a, b in TimeSeriesSplit(n_splits=rule['folds']).split(unique):
            train = indices[stamps.iloc[indices].isin(unique[a])]
            valid = indices[stamps.iloc[indices].isin(unique[b])]
            if rule.get('gap_seconds'):
                train = train[(stamps.iloc[train] < stamps.iloc[valid].min() - pd.Timedelta(seconds=rule['gap_seconds'])).to_numpy()]
            if availability:
                train = train[(available.iloc[train] < stamps.iloc[valid].min()).to_numpy()]
            # Repeated subjects imply overlapping windows. Purge those subjects.
            if group and not rule.get('gap_seconds'):
                train = train[~frame[group].iloc[train].isin(frame[group].iloc[valid]).to_numpy()]
            folds.append((train, valid))
    else:
        if fraction:
            indices, holdout = train_test_split(indices, test_size=fraction, random_state=rule['seed'], stratify=y if rule['problem'] == 'classification' else None)
        splitter = StratifiedKFold(rule['folds'], shuffle=True, random_state=rule['seed']) if rule['problem'] == 'classification' else KFold(rule['folds'], shuffle=True, random_state=rule['seed'])
        folds = [(indices[a], indices[b]) for a, b in splitter.split(indices, y.iloc[indices])]
    # A holdout subject must not enter any search fold, including temporal purging.
    if len(holdout) and group and rule['split'] == 'time' and not rule.get('gap_seconds'):
        allowed = ~frame[group].isin(frame[group].iloc[holdout])
        folds = [(a[allowed.iloc[a].to_numpy()], b[allowed.iloc[b].to_numpy()]) for a, b in folds]
    for train, valid in folds:
        if len(train) < 2 or len(valid) < 2:
            raise ValueError('隔离组别／时间后样本不足，请补充数据或降低折数')
        if rule['problem'] == 'classification' and y.iloc[train].nunique() < 2:
            raise ValueError('训练折少于两类，当前划分不支持分类训练')
    return {'samples': frame['_sample'].tolist(), 'missing_labels': missing, 'folds': [[a.tolist(), b.tolist()] for a, b in folds],
            'development': indices.tolist(), 'holdout': holdout.tolist(), 'evaluation_label': '保留测试集（尚未使用）' if len(holdout) else '仅验证集结果'}


def features(config, prediction=False):
    import numpy as np
    import pandas as pd
    frame, _ = samples(config, prediction)
    mapping, plan = config['mapping'], config.get('features', {})
    metadata = {mapping.get(k) for k in ('target', 'id_column', 'group_column', 'time_column', 'prediction_time_column', 'available_time_column', 'label_available_time_column')}
    descriptions = []
    if mapping['kind'] == 'timeseries':
        from tsfresh import extract_features
        from tsfresh.feature_extraction import MinimalFCParameters
        if not config.get('labels'):
            raise ValueError('时序需要单独标签／预测时点清单')
        raw = read_table(config['source'], mapping)
        clock = timestamps(raw[mapping['time_column']], mapping)
        availability = timestamps(raw[mapping['available_time_column']], mapping) if mapping.get('available_time_column') else clock
        if clock.isna().any() or availability.isna().any():
            raise ValueError('测量时间或可用时间缺失')
        columns = plan.get('columns') or [c for c in raw.select_dtypes(include='number').columns if c not in metadata]
        columns = [c for c in columns if c not in plan.get('exclude', [])]
        if not columns or set(columns) & metadata:
            raise ValueError('特征不可包含标签或元数据字段，且至少选择一个数值测量字段')
        chunks = []
        for i, row in frame.iterrows():
            cutoff = timestamps(row[mapping['prediction_time_column']], mapping)
            if pd.isna(cutoff):
                raise ValueError('预测时点缺失')
            mask = (raw[mapping['id_column']] == row[mapping['id_column']]) & (clock <= cutoff) & (availability <= cutoff)
            if plan.get('window_seconds'):
                mask &= clock > cutoff - pd.Timedelta(seconds=plan['window_seconds'])
            chunk = raw.loc[mask, columns].copy()
            if chunk.empty:
                raise ValueError(f'样本 {i} 在预测时点前没有可用测量，不能删除后继续评估')
            if chunk.isna().any().any() or not np.isfinite(chunk.to_numpy(dtype=float)).all():
                raise ValueError('时序测量有缺失／无穷值，请明确清洗方案后登记新版本')
            chunk['_id'], chunk['_time'] = i, clock.loc[mask].astype('int64').to_numpy()
            chunks.append(chunk)
        settings = MinimalFCParameters()
        if plan.get('timeseries') == 'extended':
            settings.update(absolute_sum_of_changes=None, mean_abs_change=None, linear_trend=[{'attr': 'slope'}],
                            autocorrelation=[{'lag': 1}], fft_coefficient=[{'coeff': 1, 'attr': 'abs'}])
        x = extract_features(pd.concat(chunks, ignore_index=True), column_id='_id', column_sort='_time', default_fc_parameters=settings, n_jobs=0, disable_progressbar=True)
        x = x.reindex(range(len(frame))).replace([np.inf, -np.inf], np.nan)
        for c in x:
            descriptions.append({'name': c, 'source': c.split('__')[0], 'calculation': c.split('__', 1)[-1], 'window_seconds': plan.get('window_seconds'), 'condition': '测量时间及可用时间不晚于预测时点'})
    else:
        columns = plan.get('columns') or [c for c in frame if c not in metadata and c != '_sample']
        if set(columns) & metadata:
            raise ValueError('标签、标识及时间元数据不能作为普通特征')
        x = frame[[c for c in columns if c not in plan.get('exclude', [])]].copy()
        x = x.replace([np.inf, -np.inf], np.nan)
        descriptions = [{'name': c, 'source': c, 'calculation': '原始字段；缺失填补及编码仅在训练折拟合'} for c in x]
    if not len(x.columns):
        raise ValueError('没有可用特征')
    return x, frame, descriptions


def cached_features(config):
    import joblib
    cache = Path(config['output']) / 'features.joblib'
    if cache.exists():
        return joblib.load(cache)
    if config.get('feature_cache'):
        result = joblib.load(config['feature_cache'])
    else:
        result = features(config)
    joblib.dump(result, cache.with_suffix('.tmp'))
    os.replace(cache.with_suffix('.tmp'), cache)
    return result


def pipeline(x, config, model):
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler
    from sklearn.impute import SimpleImputer
    from sklearn.feature_selection import SelectKBest, f_regression, f_classif
    steps = []
    if config.get('transformer'):
        spec = importlib.util.spec_from_file_location('project_transformer', config['transformer'])
        module = importlib.util.module_from_spec(spec)
        sys.modules['project_transformer'] = module
        spec.loader.exec_module(module)
        steps.append(('project_features', module.build_transformer()))
    # Dtype selectors run at fit time, including after a project transformer.
    from sklearn.compose import make_column_selector
    prep = ColumnTransformer([
        ('numeric', Pipeline([('fill', SimpleImputer(strategy='median', keep_empty_features=True)), ('scale', StandardScaler())]), make_column_selector(dtype_include='number')),
        ('categorical', Pipeline([('fill', SimpleImputer(strategy='most_frequent', keep_empty_features=True)), ('encode', OneHotEncoder(handle_unknown='ignore', sparse_output=False))]), make_column_selector(dtype_exclude='number')),
    ], sparse_threshold=0)
    steps.append(('preprocess', prep))
    if config.get('features', {}).get('select_k'):
        # Percentile would alter the requested semantics. k is checked by sklearn.
        steps.append(('select', SelectKBest(f_regression if config['evaluation']['problem'] == 'regression' else f_classif, k=config['features']['select_k'])))
    steps.append(('model', model))
    return Pipeline(steps)


def estimator(name, config, trial=None):
    from sklearn.linear_model import Ridge, LogisticRegression
    from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier, HistGradientBoostingRegressor, HistGradientBoostingClassifier
    from sklearn.svm import SVR, SVC
    regression = config['evaluation']['problem'] == 'regression'
    params = dict(config.get('parameters', {}))
    seed = config['evaluation']['seed']
    smallest_fold = config.get('_min_train_samples', 100)
    if name == 'hist_gradient':
        params.setdefault('min_samples_leaf', max(2, min(20, smallest_fold // 5)))
    if trial:
        custom = config.get('search_space', {}).get(name)
        if custom is not None:
            for key, spec in custom.items():
                if spec['type'] == 'categorical':
                    params[key] = trial.suggest_categorical(key, spec['choices'])
                elif spec['type'] == 'int':
                    params[key] = trial.suggest_int(key, int(spec['low']), int(spec['high']), log=spec.get('log', False))
                else:
                    params[key] = trial.suggest_float(key, spec['low'], spec['high'], log=spec.get('log', False))
        elif name == 'linear' and ('alpha' if regression else 'C') not in params:
            params['alpha' if regression else 'C'] = trial.suggest_float('regularization', .001, 100, log=True)
        elif name == 'forest':
            if 'max_depth' not in params:
                params['max_depth'] = trial.suggest_int('max_depth', 2, 16)
            if 'min_samples_leaf' not in params:
                params['min_samples_leaf'] = trial.suggest_int('min_samples_leaf', 1, max(1, min(10, smallest_fold // 2)))
        elif name == 'hist_gradient':
            if 'learning_rate' not in params:
                params['learning_rate'] = trial.suggest_float('learning_rate', .01, .3, log=True)
            if 'max_leaf_nodes' not in params:
                params['max_leaf_nodes'] = trial.suggest_int('max_leaf_nodes', 4, 31)
            if 'min_samples_leaf' not in config.get('parameters', {}):
                params['min_samples_leaf'] = trial.suggest_int('min_samples_leaf', 1, max(1, min(20, smallest_fold // 2)))
        elif name == 'svm':
            if 'C' not in params:
                params['C'] = trial.suggest_float('C', .01, 100, log=True)
            if 'kernel' not in params:
                params['kernel'] = trial.suggest_categorical('kernel', ['rbf', 'linear'])
            if params['kernel'] != 'linear' and 'gamma' not in params:
                params['gamma'] = trial.suggest_float('gamma', .0001, 1, log=True)
            if regression and 'epsilon' not in params:
                params['epsilon'] = trial.suggest_float('epsilon', .001, .5, log=True)
    if name == 'linear':
        if not regression:
            params.setdefault('max_iter', 1000)
        return Ridge(**params) if regression else LogisticRegression(**params)
    if name == 'forest':
        params.setdefault('n_estimators', 100)
        params.setdefault('random_state', seed)
        requested_jobs = params.get('n_jobs', 4)
        params['n_jobs'] = 4 if requested_jobs in (-1, None) else max(1, min(4, int(requested_jobs)))
        return (RandomForestRegressor if regression else RandomForestClassifier)(**params)
    if name == 'hist_gradient':
        params.setdefault('random_state', seed)
        return (HistGradientBoostingRegressor if regression else HistGradientBoostingClassifier)(**params)
    if not regression:
        params.update(probability=True)
        params.setdefault('random_state', seed)
    return SVR(**params) if regression else SVC(**params)


def autogluon_parameters(config):
    import copy
    parameters = copy.deepcopy(config.get('autogluon_hyperparameters') or {'GBM': {}, 'RF': {}, 'XT': {}})
    leaf = max(2, min(20, config.get('_min_train_samples', 100) // 5))
    if 'GBM' in parameters:
        variants = parameters['GBM'] if isinstance(parameters['GBM'], list) else [parameters['GBM']]
        for variant in variants:
            if not any(k in variant for k in ('min_data_in_leaf', 'min_child_samples', 'min_samples_leaf', 'min_data', 'min_data_per_leaf')):
                variant['min_data_in_leaf'] = leaf
    return parameters


def prediction_diagnostics(y, predictions, fold, train_rows, problem):
    import numpy as np
    pred, actual = np.asarray(predictions), np.asarray(y)
    unique = int(len(np.unique(pred)))
    constant = unique == 1 and len(np.unique(actual)) > 1
    return {'fold': fold, 'train_rows': train_rows, 'validation_rows': len(pred), 'unique_predictions': unique,
            'prediction_std': float(np.std(pred)) if problem == 'regression' else None,
            'actual_std': float(np.std(actual)) if problem == 'regression' else None,
            'constant_prediction': constant}


def scores(y, predictions, problem, probabilities=None, classes=None):
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score, f1_score, accuracy_score, roc_auc_score
    if problem == 'regression':
        return {'mae': mean_absolute_error(y, predictions), 'rmse': mean_squared_error(y, predictions) ** .5, 'r2': r2_score(y, predictions)}
    # Keep the trained class domain in macro averaging even when a small test
    # split contains no examples of a rare class. Also expose unseen test labels.
    labels = list(dict.fromkeys([*(classes if classes is not None else []), *y, *predictions]))
    result = {'macro_f1': f1_score(y, predictions, labels=labels, average='macro', zero_division=0), 'accuracy': accuracy_score(y, predictions)}
    if probabilities is not None:
        try:
            result['roc_auc'] = roc_auc_score(y, probabilities[:, 1] if len(classes) == 2 else probabilities, labels=classes, multi_class='ovr')
        except ValueError:
            result['roc_auc'] = None
    return clean(result)


def classification_details(y, predictions, classes):
    from sklearn.metrics import precision_recall_fscore_support, confusion_matrix
    labels = list(dict.fromkeys([*classes, *y, *predictions]))
    precision, recall, f1, support = precision_recall_fscore_support(y, predictions, labels=labels, zero_division=0)
    return {'classes': [{'label': str(label), 'precision': float(precision[i]), 'recall': float(recall[i]),
                        'f1': float(f1[i]), 'samples': int(support[i])} for i, label in enumerate(labels)],
            'confusion_matrix': confusion_matrix(y, predictions, labels=labels).tolist(),
            'note': '宏平均使用固定类别范围；样本数为 0 的类别无法据此评价可靠性。'}


def decision_confidence(predictions, probabilities, classes):
    positions = {label: i for i, label in enumerate(classes)}
    values = [float(row[positions[label]]) for label, row in zip(predictions, probabilities)]
    if len(values) != len(predictions) or any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        raise ValueError('阈值选择需要完整且有效的分类概率')
    return values


def select_acceptance(truth, predictions, probabilities, classes, target, minimum):
    """Select on out-of-fold predictions only; holdout never enters this function."""
    confidence = decision_confidence(predictions, probabilities, classes)
    if len(truth) != len(predictions) or not truth:
        raise ValueError('阈值选择缺少验证样本')
    ordered = sorted(zip(confidence, [a == b for a, b in zip(truth, predictions)]), reverse=True)
    curve, correct, chosen = [], 0, None
    for i, (value, matches) in enumerate(ordered):
        correct += int(matches)
        if i + 1 < len(ordered) and ordered[i + 1][0] == value:
            continue  # All equal scores must receive the same decision.
        point = {'threshold': value, 'accepted': i + 1, 'accuracy': correct / (i + 1), 'coverage': (i + 1) / len(ordered)}
        curve.append(point)
        if point['accepted'] >= minimum and point['accuracy'] >= target:
            chosen = point
    return {'status': 'selected' if chosen else 'unavailable', 'threshold': chosen['threshold'] if chosen else None,
            'target_accuracy': target, 'minimum_samples': minimum, 'validation_rows': len(truth),
            'validation': chosen, 'curve': curve,
            'note': '仅用折外验证预测选阈值；验证准确率不是独立测试或生产保证。无可用阈值时全部交由复核。'}


def acceptance_result(policy, predictions, probabilities, classes, truth=None):
    confidence = decision_confidence(predictions, probabilities, classes)
    accepted = [policy['status'] == 'selected' and value >= policy['threshold'] for value in confidence]
    count = sum(accepted)
    result = {'threshold': policy['threshold'], 'status': policy['status'], 'rows': len(accepted),
              'accepted': count, 'review': len(accepted) - count, 'coverage': count / len(accepted) if accepted else 0,
              'accuracy': sum(a == b for a, b, keep in zip(truth, predictions, accepted) if keep) / count if truth is not None and count else None}
    return accepted, result


def evaluate(config, x, frame, split, model_name, folder, trial=None):
    import numpy as np
    import pandas as pd
    import joblib
    from sklearn.dummy import DummyClassifier, DummyRegressor
    rule, mapping = config['evaluation'], config['mapping']
    y, target = frame[mapping['target']], mapping['target']
    predictions, truth, rows, probabilities = [], [], [], []
    fold_scores = []
    diagnostics = []
    baseline_predictions, baseline_probabilities = [], []
    classes = np.sort(y.iloc[split['development']].unique()) if rule['problem'] == 'classification' else None
    if config['engine'] == 'autogluon':
        ag_problem = 'regression' if classes is None else ('binary' if len(classes) == 2 else 'multiclass')
        ag_metric = {'mae': 'mean_absolute_error', 'rmse': 'root_mean_squared_error',
                     'macro_f1': 'f1_macro', 'roc_auc': 'roc_auc_ovr' if ag_problem == 'multiclass' else 'roc_auc'}.get(rule['metric'], rule['metric'])
        ag_options = {'label': target, 'problem_type': ag_problem, 'eval_metric': ag_metric, 'verbosity': 0}
        ag_parameters = autogluon_parameters(config)
    for index, (train, valid) in enumerate(split['folds']):
        if config['engine'] == 'autogluon':
            from autogluon.tabular import TabularPredictor
            # Outer validation is never passed to fit; AG tunes only on training.
            fitted = TabularPredictor(**ag_options, path=str(folder / f'ag-fold-{index}'))
            fitted.fit(x.iloc[train].assign(**{target: y.iloc[train].to_numpy()}), hyperparameters=ag_parameters, presets='medium_quality', time_limit=max(2, config['trial_seconds'] / (len(split['folds']) + 1)), num_cpus=4, num_gpus=0, memory_limit=3, num_bag_folds=0, num_stack_levels=0)
            pred = fitted.predict(x.iloc[valid]).to_numpy()
            proba = fitted.predict_proba(x.iloc[valid]).reindex(columns=classes, fill_value=0).to_numpy() if classes is not None else None
        else:
            fitted = pipeline(x, config, estimator(model_name, config, trial))
            fitted.fit(x.iloc[train], y.iloc[train])
            pred = fitted.predict(x.iloc[valid])
            proba = None
            if classes is not None:
                proba = pd.DataFrame(fitted.predict_proba(x.iloc[valid]), columns=fitted.classes_).reindex(columns=classes, fill_value=0).to_numpy()
        fold_scores.append(scores(y.iloc[valid], pred, rule['problem'], proba, classes))
        diagnostics.append(prediction_diagnostics(y.iloc[valid], pred, index + 1, len(train), rule['problem']))
        dummy = DummyRegressor(strategy='median' if rule['metric'] == 'mae' else 'mean') if classes is None else DummyClassifier(strategy='prior')
        dummy.fit(x.iloc[train], y.iloc[train])
        baseline_predictions.extend(dummy.predict(x.iloc[valid]).tolist())
        if classes is not None:
            baseline_probabilities.extend(pd.DataFrame(dummy.predict_proba(x.iloc[valid]), columns=dummy.classes_).reindex(columns=classes, fill_value=0).to_numpy().tolist())
            probabilities.extend(proba.tolist())
        predictions.extend(pred.tolist()); truth.extend(y.iloc[valid].tolist()); rows.extend(valid)
    metric = scores(truth, predictions, rule['problem'], np.array(probabilities) if probabilities else None, classes)
    baseline = scores(truth, baseline_predictions, rule['problem'], np.array(baseline_probabilities) if baseline_probabilities else None, classes)
    if metric.get(rule['metric']) is None:
        raise ValueError('固定验证折无法计算指定指标；不能跳过失败折报告提升')
    out = pd.DataFrame({'sample': frame['_sample'].iloc[rows].to_numpy(), 'split_index': rows,
                        'fold': [i + 1 for i, (_, valid) in enumerate(split['folds']) for _ in valid],
                        'actual': truth, 'prediction': predictions, 'baseline_prediction': baseline_predictions})
    if mapping.get('id_column'):
        out['sample_id'] = frame[mapping['id_column']].iloc[rows].to_numpy()
    if classes is not None:
        # Preserve probabilities to make ROC-AUC and the dummy comparison reproducible.
        # Class order is saved alongside the trial, avoiding ambiguous column labels.
        for i in range(len(classes)):
            out[f'probability:{i}'] = np.asarray(probabilities)[:, i]
            out[f'baseline_probability:{i}'] = np.asarray(baseline_probabilities)[:, i]
    group_errors = []
    groups = mapping.get('error_group_columns') or [mapping.get('group_column') or mapping.get('id_column')]
    for group in filter(None, groups):
        if group not in frame:
            raise ValueError(f'误差分组字段不存在：{group}')
        out['group:' + group] = frame[group].iloc[rows].to_numpy()
        for key, part in out.groupby('group:' + group):
            group_errors.append({'column': group, 'group': str(key), 'samples': len(part), 'error': float((part.actual - part.prediction).abs().mean()) if classes is None else float((part.actual != part.prediction).mean())})
    out.to_csv(folder / 'validation-predictions.csv', index=False)
    acceptance = None
    if rule.get('acceptance_accuracy') is not None:
        acceptance = select_acceptance(truth, predictions, probabilities, classes,
                                       rule['acceptance_accuracy'], rule.get('acceptance_min_samples', 10))
        save(folder / 'acceptance.json', acceptance)
    development = split['development']
    importance = []
    if config['engine'] == 'autogluon':
        from autogluon.tabular import TabularPredictor
        model = TabularPredictor(**ag_options, path=str(folder / 'autogluon'))
        model.fit(x.iloc[development].assign(**{target: y.iloc[development].to_numpy()}), hyperparameters=ag_parameters, presets='medium_quality', time_limit=max(2, config['trial_seconds'] / (len(split['folds']) + 1)), num_cpus=4, num_gpus=0, memory_limit=3, num_bag_folds=0, num_stack_levels=0)
        info = json.loads(json.dumps(model.info(), default=str))
        save(folder / 'autogluon-models.json', info)
        selected = model.model_best
        effective = {'class': 'autogluon.tabular.TabularPredictor', 'selected_model': selected,
                     'parameters': info.get('model_info', {}).get(selected, {}), 'fit_hyperparameters': ag_parameters}
    else:
        model = pipeline(x, config, estimator(model_name, config, trial))
        model.fit(x.iloc[development], y.iloc[development])
        final = model.named_steps['model']
        effective = {'class': type(final).__module__ + '.' + type(final).__name__, 'parameters': final.get_params()}
        values = getattr(final, 'feature_importances_', None)
        if values is None and hasattr(final, 'coef_'):
            values = np.abs(final.coef_).reshape(-1) if final.coef_.ndim == 1 else np.abs(final.coef_).mean(axis=0)
        try:
            names = model[:-1].get_feature_names_out()
            if values is not None:
                importance = sorted([{'name': str(n), 'value': float(v)} for n, v in zip(names, values)], key=lambda r: r['value'], reverse=True)[:30]
        except (AttributeError, ValueError):
            pass
        joblib.dump(model, folder / 'model.joblib.tmp')
        os.replace(folder / 'model.joblib.tmp', folder / 'model.joblib')
    constant_folds = [d['fold'] for d in diagnostics if d['constant_prediction']]
    warnings = [f'第 {"、".join(map(str, constant_folds))} 折对不同实测值给出了相同预测；请检查样本量、特征和参数。此提示不代表算法方向无效。'] if constant_folds else []
    return {'metrics': metric, 'baseline': baseline, 'fold_metrics': fold_scores, 'diagnostics': diagnostics, 'warnings': warnings, 'group_errors': sorted(group_errors, key=lambda r: r['error'], reverse=True)[:50],
            'acceptance': acceptance, 'effective_model': effective, 'importance': importance, 'validation_rows': len(rows), 'prediction_preview': out.head(30).to_dict('records'), 'feature_columns': list(x.columns), 'classes': classes.tolist() if classes is not None else None}


def bounded_evaluate(config, name, folder, seconds, trial):
    """A process boundary enforces deadlines even inside a native estimator."""
    args = {**config, '_trial_model': name, '_trial_folder': str(folder)}
    if trial is not None:
        # Ask in the parent; the child fits these exact parameters without DB access.
        args['parameters'] = estimator(name, config, trial).get_params()
    path = folder / 'trial-input.json'
    save(path, args)
    with (folder / 'training.log').open('w') as log:
        process = subprocess.Popen([sys.executable, __file__, '--trial', str(path)], stdout=log, stderr=subprocess.STDOUT)
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            raise TimeoutError('单次训练超过时间预算，计算进程已终止') from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    result_path = folder / 'trial-output.json'
    if not result_path.exists():
        raise RuntimeError('训练进程异常退出：' + (folder / 'training.log').read_text()[-1200:])
    result = json.loads(result_path.read_text())
    if process.returncode or result.get('error'):
        raise RuntimeError(result.get('error') or '训练进程异常退出')
    return result['result']


def train(config):
    import optuna
    optuna.logging.set_verbosity(optuna.logging.ERROR)
    output = Path(config['output']); output.mkdir(parents=True, exist_ok=True)
    split = json.loads(Path(config['split']).read_text())
    config = {**config, '_min_train_samples': min(len(train) for train, _ in split['folds'])}
    x, frame, descriptions = cached_features(config)
    if frame['_sample'].tolist() != split['samples']:
        raise ValueError('样本与固定划分不一致，需创建新研究')
    save(output / 'features.json', descriptions)
    direction = 'minimize' if config['evaluation']['metric'] in ('mae', 'rmse') else 'maximize'
    study = optuna.create_study(storage='sqlite:///' + str(config.get('optuna_storage') or output / 'optuna.db'), study_name='parameters', load_if_exists=True, direction=direction,
                                sampler=optuna.samplers.TPESampler(seed=config['evaluation']['seed'] + len(list(output.glob('trial-*/result.json'))), n_startup_trials=2))
    # An interrupted ask has no completion. It retains its slot and is retried.
    saved_results = [json.loads(p.read_text()) for p in output.glob('trial-*/result.json')]
    for trial in study.get_trials(states=(optuna.trial.TrialState.RUNNING,)):
        saved = next((r for r in saved_results if r.get('optuna_trial') == trial.number and r['status'] == 'completed'), None)
        if saved:
            study.tell(trial.number, saved['metrics'][config['evaluation']['metric']])
        else:
            study.tell(trial.number, state=optuna.trial.TrialState.FAIL)
    started = time.monotonic()
    for slot in range(config['batch_size']):
        result_path = output / f'trial-{slot}' / 'result.json'
        if result_path.exists():
            emit('trial', **json.loads(result_path.read_text()), replayed=True)
            continue
        if time.monotonic() - started >= config['remaining_seconds']:
            break
        folder = result_path.parent
        # Preserve interrupted attempts for inspection; only result.json commits.
        if folder.exists():
            from uuid import uuid4
            attempts = output / 'interrupted-attempts'; attempts.mkdir(exist_ok=True)
            folder.rename(attempts / f'trial-{slot}-{uuid4()}')
        folder.mkdir()
        trial = study.ask()
        name = config['models'][slot % len(config['models'])] if config['engine'] != 'autogluon' else 'autogluon'
        begin = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        save(folder / 'attempt.json', {'slot': slot, 'model': name, 'started_at': started_at, 'optuna_trial': trial.number,
                                      'requested_parameters': config.get('parameters', {})})
        emit('trial_started', slot=slot, model=name, started_at=started_at)
        try:
            seconds = max(.1, min(config['trial_seconds'], config['remaining_seconds'] - (begin - started)))
            result = bounded_evaluate(config, name, folder, seconds, trial if config['engine'] == 'optuna' else None)
            result.update(slot=slot, model=name, status='completed', parameters=trial.params, seconds=time.monotonic() - begin)
            result['optuna_trial'] = trial.number
        except Exception as exc:
            result = {'slot': slot, 'model': name, 'status': 'failed', 'error': str(exc), 'seconds': time.monotonic() - begin}
        result.update(started_at=started_at, completed_at=datetime.now(timezone.utc).isoformat(), optuna_trial=trial.number,
                      parameters=trial.params, requested_parameters=config.get('parameters', {}),
                      search={'engine': config['engine'], 'sampler': 'TPESampler' if config['engine'] == 'optuna' else None,
                              'seed': config['evaluation']['seed'] + len(saved_results), 'n_startup_trials': 2,
                              'distributions': {k: json.loads(optuna.distributions.distribution_to_json(v)) for k, v in trial.distributions.items()}})
        save(result_path, result)
        if result['status'] == 'completed':
            study.tell(trial, result['metrics'][config['evaluation']['metric']])
        else:
            study.tell(trial, state=optuna.trial.TrialState.FAIL)
        emit('trial', **result)
    completed = [json.loads(p.read_text()) for p in sorted(output.glob('trial-*/result.json'))]
    good = [t for t in completed if t['status'] == 'completed']
    best = sorted(good, key=lambda t: t['metrics'][config['evaluation']['metric']], reverse=direction == 'maximize')
    return {'trials': completed, 'best': best[0] if best else None, 'features': descriptions, 'status': 'completed' if len(completed) == config['batch_size'] else 'interrupted'}


def predict(config):
    import joblib
    x, frame, _ = features(config, prediction=True)
    columns = config['feature_columns']
    if set(columns) != set(x.columns):
        raise ValueError('预测字段与训练特征不匹配')
    x = x[columns]
    if config.get('transformer'):
        spec = importlib.util.spec_from_file_location('project_transformer', config['transformer'])
        module = importlib.util.module_from_spec(spec); sys.modules['project_transformer'] = module; spec.loader.exec_module(module)
    if config['engine'] == 'autogluon':
        from autogluon.tabular import TabularPredictor
        model = TabularPredictor.load(str(Path(config['model']) / 'autogluon'))
    else:
        model = joblib.load(Path(config['model']) / 'model.joblib')
    predictions = model.predict(x)
    output = frame[[config['mapping']['id_column']]].copy() if config['mapping'].get('id_column') else frame[['_sample']].copy()
    output['prediction'] = predictions
    probability_columns = {}
    if config.get('classes'):
        import numpy as np
        probabilities = model.predict_proba(x)
        classes = list(probabilities.columns) if hasattr(probabilities, 'columns') else list(model.classes_)
        probabilities = np.asarray(probabilities)
        for i, label in enumerate(classes):
            column = 'probability_' + str(label)
            output[column] = probabilities[:, i]
            probability_columns[str(label)] = column
    acceptance = None
    policy_path = Path(config['model']) / 'acceptance.json'
    if policy_path.exists():
        policy = json.loads(policy_path.read_text())
        accepted, acceptance = acceptance_result(policy, list(predictions), probabilities, classes)
        output['decision'] = ['accept_prediction' if keep else 'review' for keep in accepted]
    output.to_csv(Path(config['output']) / 'predictions.csv', index=False)
    return {'rows': len(output), 'preview': output.head(30).to_dict('records'), 'probability_columns': probability_columns, 'acceptance': acceptance}


def holdout(config):
    import joblib
    import numpy as np
    x, frame, _ = features(config)
    split = json.loads(Path(config['split']).read_text())
    indices = split['holdout']
    if not indices:
        raise ValueError('本研究没有保留测试样本，只能报告验证结果')
    x = x[config['feature_columns']].iloc[indices]
    if config.get('transformer'):
        spec = importlib.util.spec_from_file_location('project_transformer', config['transformer'])
        module = importlib.util.module_from_spec(spec); sys.modules['project_transformer'] = module; spec.loader.exec_module(module)
    if config['engine'] == 'autogluon':
        from autogluon.tabular import TabularPredictor
        model = TabularPredictor.load(str(Path(config['model']) / 'autogluon'))
    else:
        model = joblib.load(Path(config['model']) / 'model.joblib')
    y = frame[config['mapping']['target']].iloc[indices]
    pred = model.predict(x)
    classes = np.array(config['classes']) if config.get('classes') else None
    proba = model.predict_proba(x) if classes is not None else None
    if hasattr(proba, 'reindex'):
        proba = proba.reindex(columns=classes, fill_value=0).to_numpy()
    result = {'metrics': scores(y, pred, config['evaluation']['problem'], proba, classes), 'rows': len(indices),
              'label': '保留测试集结果；从此不再用于本研究搜索。未声明为未见数据的盲测。'}
    if classes is not None:
        result['classification'] = classification_details(y, pred, classes)
    import pandas as pd
    out = pd.DataFrame({'sample': frame['_sample'].iloc[indices], 'actual': y, 'prediction': pred})
    if classes is not None:
        for i in range(len(classes)):
            out[f'probability:{i}'] = proba[:, i]
    policy_path = Path(config['model']) / 'acceptance.json'
    if policy_path.exists():
        policy = json.loads(policy_path.read_text())
        accepted, details = acceptance_result(policy, list(pred), proba, classes, list(y))
        result['acceptance'] = {'selection': policy, 'test': details}
        out['decision'] = ['accept_prediction' if keep else 'review' for keep in accepted]
    out.to_csv(Path(config['output']) / 'holdout-predictions.csv', index=False)
    return result


def inspect_model(config):
    import joblib
    import warnings
    from sklearn.exceptions import InconsistentVersionWarning
    from sklearn.pipeline import Pipeline
    with warnings.catch_warnings():
        warnings.simplefilter('error', InconsistentVersionWarning)
        model = joblib.load(config['model'])
    if not isinstance(model, Pipeline) or len(model.steps) < 2:
        raise ValueError('模型包必须是包含预处理与预测器的 sklearn Pipeline；单独权重请先配套预处理')
    inferred = list(getattr(model, 'feature_names_in_', []))
    columns = config.get('feature_columns') or inferred
    if not columns or not all(isinstance(c, str) for c in columns) or len(set(columns)) != len(columns):
        raise ValueError('模型没有可用字段名称，请明确填写训练时的输入字段')
    if inferred and columns != inferred:
        raise ValueError('声明字段与模型记录的训练字段顺序不一致')
    return {'feature_columns': columns, 'classes': list(model.classes_) if hasattr(model, 'classes_') else None}


def main():
    if sys.argv[1] == '--trial':
        config = json.loads(Path(sys.argv[2]).read_text())
        folder = Path(config['_trial_folder'])
        try:
            x, frame, _ = cached_features(config)
            result = evaluate(config, x, frame, json.loads(Path(config['split']).read_text()), config['_trial_model'], folder)
            save(folder / 'trial-output.json', {'result': result})
        except Exception as error:
            save(folder / 'trial-output.json', {'error': str(error)})
            raise
        return
    config = json.loads(Path(sys.argv[1]).read_text())
    started = time.monotonic()
    action = config['action']
    if action == 'inspect_model':
        result = inspect_model(config)
    elif action == 'profile':
        result = profile(config)
    elif action == 'prepare':
        result = prepare(config)
        save(Path(config['output']) / 'split.json', result)
    elif action == 'features':
        x, _, descriptions = cached_features(config)
        result = {'rows': len(x), 'columns': len(x.columns), 'features': descriptions}
    elif action == 'train':
        result = train(config)
    elif action == 'predict':
        result = predict(config)
    elif action == 'holdout':
        result = holdout(config)
    else:
        raise ValueError('Unknown modeling action')
    import resource
    peak = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * (1024 if sys.platform == 'linux' else 1)
    if Path('/sys/fs/cgroup/memory.peak').exists():
        peak = int(Path('/sys/fs/cgroup/memory.peak').read_text())
    emit('result', result=clean(result), seconds=time.monotonic() - started, peak_memory_bytes=peak)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('error', error=str(error), trace=traceback.format_exc()[-4000:])
        sys.exit(1)
