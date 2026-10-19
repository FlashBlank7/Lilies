"""Selection semantics shared by the data workspace and the isolated ML worker."""
from __future__ import annotations


def select_table(frame, selection):
    import math
    import pandas as pd
    columns = selection.get('columns') or list(frame.columns)
    if len(set(columns)) != len(columns) or any(c not in frame for c in columns):
        raise ValueError('所选字段不存在或重复，请重新选择字段')
    indices = selection.get('row_indices')
    if indices is not None and (len(set(indices)) != len(indices) or any(i < 0 or i >= len(frame) for i in indices)):
        raise ValueError('选中行已失效，请重新读取原数据')
    mask = pd.Series(True, index=frame.index)
    for condition in selection.get('filters', []):
        column, operator, value = condition['column'], condition['operator'], condition.get('value')
        if column not in frame:
            raise ValueError('筛选字段不存在：' + column)
        series = frame[column]
        if operator == 'state':
            cells = frame.attrs.get('data_state', {}).get('cells', [])
            if not any(c['column'] == column for c in cells):
                raise ValueError('请先定义字段规则，再按数据状态筛选')
            positions = {c['row'] for c in cells if c['column'] == column and c['status'] == value}
            match = pd.Series(frame.index.isin(positions), index=frame.index)
        elif operator in ('gt', 'ge', 'lt', 'le'):
            try:
                number = float(value)
            except (ValueError, TypeError) as error:
                raise ValueError('数值筛选需要填写数字') from error
            if not math.isfinite(number):
                raise ValueError('筛选值必须是有限数字')
            numeric = pd.to_numeric(series, errors='coerce')
            match = getattr(numeric, operator)(number) & numeric.notna()
        elif operator == 'contains':
            match = series.astype('string').str.contains(str(value), regex=False, na=False)
        elif operator in ('eq', 'ne'):
            if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
                try:
                    number = float(value)
                except (ValueError, TypeError) as error:
                    raise ValueError('数值字段的相等筛选需要数字') from error
                if not math.isfinite(number):
                    raise ValueError('筛选值必须是有限数字')
                match = series.eq(number).fillna(False)
            else:
                match = series.astype('string').eq(str(value)).fillna(False)
            if operator == 'ne':
                match = ~match
        elif operator == 'empty':
            match = series.isna() | series.astype('string').eq('').fillna(False)
        else:
            raise ValueError('不支持的筛选方式：' + operator)
        mask &= match
    if indices is not None:
        selected = frame.iloc[indices]
        selected = selected.loc[mask.loc[selected.index]]
    else:
        selected = frame.loc[mask]
    return selected.loc[:, columns].copy()
