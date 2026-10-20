"""Fold-fitted transforms shipped alongside worker.py in images and model bundles."""
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils.validation import check_is_fitted


class IQRClipper(TransformerMixin, BaseEstimator):
    def __init__(self, columns=(), factor=1.5):
        self.columns = columns
        self.factor = factor

    def fit(self, X, y=None):
        if not self.columns:
            raise ValueError('训练异常值截断需要明确选择数值字段')
        if not np.isfinite(self.factor) or self.factor <= 0:
            raise ValueError('IQR 倍数必须是大于 0 的有限数值')
        self._check_columns(X)
        self.feature_names_in_ = np.asarray(X.columns, dtype=object)
        self.n_features_in_ = len(X.columns)
        self.bounds_ = {}
        self.warnings_ = []
        for column in self.columns:
            values = X[column].dropna()
            if values.empty:
                self.bounds_[column] = {'lower': None, 'upper': None, 'valid_samples': 0}
                continue
            q1, q3 = values.quantile([.25, .75])
            width = q3 - q1
            if width == 0:
                self.warnings_.append(f'字段 {column} 的训练四分位距为 0（Q1 = Q3 = {q1:g}）；'
                                      f'IQR 截断会把该列所有非缺失值（包括预测输入）变成 {q1:g}，无法保留该列的数值变化。')
            self.bounds_[column] = {'lower': float(q1 - self.factor * width),
                                    'upper': float(q3 + self.factor * width), 'valid_samples': len(values)}
        return self

    def _check_columns(self, X):
        if not isinstance(X, pd.DataFrame):
            raise ValueError('训练异常值截断需要带字段名称的表格')
        for column in self.columns:
            if column not in X or not pd.api.types.is_numeric_dtype(X[column]) or pd.api.types.is_bool_dtype(X[column]):
                raise ValueError(f'训练异常值截断字段必须存在且为数值：{column}')
            if not np.isfinite(X[column].dropna().to_numpy(dtype=float)).all():
                raise ValueError(f'训练异常值截断字段包含无穷值：{column}')

    def transform(self, X):
        check_is_fitted(self, 'bounds_')
        self._check_columns(X)
        result = X.copy()
        for column, limits in self.bounds_.items():
            if limits['lower'] is not None:
                result[column] = result[column].clip(limits['lower'], limits['upper'])
        return result

    def get_feature_names_out(self, input_features=None):
        check_is_fitted(self, 'feature_names_in_')
        return self.feature_names_in_.copy()


class RequireCompleteNumeric(TransformerMixin, BaseEstimator):
    def fit(self, X, y=None):
        self.feature_names_in_ = np.asarray(X.columns, dtype=object)
        self.n_features_in_ = len(X.columns)
        self.transform(X)
        return self

    def transform(self, X):
        check_is_fitted(self, 'feature_names_in_')
        missing = X.columns[X.isna().any()].tolist()
        if missing:
            raise ValueError('数值缺失值策略为遇到缺失即停止；以下字段存在缺失：' + '、'.join(missing))
        return X.copy()

    def get_feature_names_out(self, input_features=None):
        check_is_fitted(self, 'feature_names_in_')
        return self.feature_names_in_.copy()
