"""CPU worker. JSON in, JSONL events out; no platform database or agent credentials.

This file is copied into the modeling image. Metrics are computed here from
predictions, never accepted from an agent. Each completed trial is committed
atomically before its completion event is emitted.
"""
from __future__ import annotations

import importlib.util
import hashlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback

CAPABILITIES = ['categorical_missing_preserve', 'field_semantics_v1', 'field_reporting_v1', 'table_prepare_v1']


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


def read_table(path, mapping, preview=False, literal_columns=()):
    import pandas as pd
    if Path(path).suffix.lower() != '.xlsx' and mapping.get('sheet', 0) != 0:
        raise ValueError('CSV/TSV 没有 Excel 工作表；请更新 sheet=0 及实际 header_row/skip_after_header，或使用 prepare 返回的数据集，不能沿用 Excel 布局')
    keys = [mapping.get(k) for k in ('id_column', 'group_column') if mapping.get(k)]
    header = mapping.get('header_row', 0)
    options = {'header': header, 'skiprows': list(range(header + 1, header + 1 + mapping.get('skip_after_header', 0))),
               'nrows': 1000 if preview else None}
    def read(**extra):
        if Path(path).suffix.lower() == '.xlsx':
            return pd.read_excel(path, sheet_name=mapping.get('sheet', 0), **options, **extra)
        return pd.read_csv(path, sep='\t' if Path(path).suffix.lower() == '.tsv' else ',', skip_blank_lines=False, **options, **extra)
    frame = read(dtype={k: str for k in keys})
    if literal_columns:
        # Preserve tokens such as "NA" in explicitly converted columns. Other
        # columns retain the established reader semantics, including labels.
        raw = read(usecols=lambda name: name in literal_columns, keep_default_na=False, dtype=object)
        for name in raw:
            frame[name] = raw[name]
    return frame


def prepare_numeric(frame, columns, rules):
    """Stateless, explicitly configured conversion shared by analysis and models."""
    import numpy as np
    import pandas as pd
    unknown = [name for name in columns if name not in frame]
    if unknown:
        raise ValueError('数值整理列不存在：' + '、'.join(unknown))
    if len(columns) != len(set(columns)):
        raise ValueError('数值整理列不能重复')
    number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
    interval = re.compile(rf'^({number})\s*[-–—~～]\s*({number})$')
    tokens = {token.strip() for token in rules.get('missing_tokens', [])}
    policy = rules.get('intervals', 'reject')
    if policy not in {'reject', 'midpoint', 'lower', 'upper', 'missing'}:
        raise ValueError('未知区间值处理方式')
    result, changes, errors = frame.copy(), [], []
    for name in columns:
        values = []
        for row, original in frame[name].items():
            text = str(original).strip()
            kind = ''
            value = np.nan
            if pd.isna(original) or text == '':
                pass
            elif isinstance(original, (bool, np.bool_)):
                errors.append(f'{name} 数据行 {row}：布尔值不能作为数值')
            elif text in tokens:
                kind = 'missing_token'
            elif re.fullmatch(number, text):
                value = float(text)
                if isinstance(original, str):
                    kind = 'numeric_text'
            elif match := interval.fullmatch(text):
                low, high = map(float, match.groups())
                if not math.isfinite(low) or not math.isfinite(high) or low > high:
                    errors.append(f'{name} 数据行 {row}：区间边界无效 {text!r}')
                elif policy == 'reject':
                    errors.append(f'{name} 数据行 {row}：区间值 {text!r} 需要明确处理方式')
                else:
                    kind = 'interval_' + policy
                    value = {'midpoint': low / 2 + high / 2, 'lower': low, 'upper': high, 'missing': np.nan}[policy]
            else:
                errors.append(f'{name} 数据行 {row}：无法转换 {text!r}；请明确占位符或修正原数据')
            if not pd.isna(value) and not math.isfinite(value):
                errors.append(f'{name} 数据行 {row}：数值超出有限范围')
            values.append(value)
            if kind:
                changes.append({'source_row': int(row), 'column': name, 'original': text,
                                'value': None if pd.isna(value) else value, 'conversion': kind})
        result[name] = pd.Series(values, index=frame.index, dtype=float)
    if errors:
        raise ValueError('数值整理失败（数据行从 0 开始）：' + '；'.join(errors[:10]) +
                         (f'；共 {len(errors)} 处错误' if len(errors) > 10 else ''))
    return result, changes


def timestamps(value, mapping, errors='raise'):
    import numbers
    import pandas as pd
    unit = mapping.get('time_unit', 'iso')
    numeric = isinstance(value, numbers.Number) or (hasattr(value, 'dtype') and pd.api.types.is_numeric_dtype(value))
    if numeric and unit == 'iso':
        raise ValueError('数值时间需要明确 time_unit（s／ms／us／ns），不能猜测采样单位')
    return pd.to_datetime(value, utc=True, errors=errors, **({'unit': unit} if numeric else {}))


def read_data(config, *, preview=False, literal_columns=()):
    """Keep physical values and semantic decisions together, including derived tables."""
    import numpy as np
    import pandas as pd
    mapping = config['mapping']
    rules = mapping.get('field_rules', {})
    frame = read_table(config.get('labels') or config['source'], mapping, preview,
                       literal_columns=list(dict.fromkeys([*literal_columns, *rules])))
    if not rules:
        if config.get('state'):
            raise ValueError('数据带有字段状态，不能丢弃 field_rules 后读取')
        return frame
    records = []
    if config.get('state'):
        state = json.loads(Path(config['state']).read_text())
        if state.get('mapping') and any(mapping.get(k, 0) != state['mapping'].get(k, 0) for k in ('sheet', 'header_row', 'skip_after_header')):
            raise ValueError('中间表读取布局与保存状态不一致，不能再次跳过表头；请使用 prepare 返回的数据集或其 mapping')
        if state.get('table_sha256') and hashlib.sha256(Path(config['source']).read_bytes()).hexdigest() != state['table_sha256']:
            raise ValueError('处理后的表格已经改变，不能沿用旧数据状态；请从原始表重新处理')
        if state['rules'] != rules:
            raise ValueError('字段规则已改变，请从原始表登记新版本；不能重新解释处理后的数值')
        if not preview and state['rows'] != len(frame):
            raise ValueError('数据状态与表格行数不一致，请同时保留处理结果和状态文件')
        records = [r for r in state['cells'] if r['row'] in frame.index]
        expected = {(int(i), c) for c in rules if c in frame for i in frame.index}
        if {(r['row'], r['column']) for r in records} != expected or len(records) != len(expected):
            raise ValueError('数据状态缺少或重复单元格，不能自动当作普通缺失')
    else:
        number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
        interval = re.compile(rf'^({number})\s*[-–—~～]\s*({number})$')
        for column, rule in rules.items():
            if column not in frame:
                continue  # Prediction need not supply targets or unused fields.
            for row, raw in frame[column].items():
                text = '' if pd.isna(raw) else str(raw)
                token = text.strip()
                value, status, reason, blocked = None, 'valid', '', False
                bounds = None
                if rule.get('meaning') == 'pending' or token in rule.get('pending_tokens', []):
                    status, reason, blocked = 'pending', '含义待确认', True
                elif not token or token in rule.get('missing_tokens', {}):
                    status = 'missing'
                    reason = rule.get('missing_tokens', {}).get(token, 'unknown')
                    blocked = reason not in rule.get('allow_missing', [])
                elif rule.get('dtype', 'number') != 'number':
                    categories = rule.get('categories') or rule.get('ordered_values', [])
                    if categories and token not in categories:
                        status, reason, blocked = 'pending', '未定义类别', True
                    else:
                        order = rule.get('ordered_values', [])
                        value = order.index(token) if order else token
                elif isinstance(raw, (bool, np.bool_)):
                    status, reason, blocked = 'invalid', '布尔值不是测量数值', True
                elif re.fullmatch(number, token):
                    value = float(token)
                elif match := interval.fullmatch(token):
                    low, high = map(float, match.groups())
                    bounds = [low, high]
                    if not all(map(math.isfinite, bounds)) or low > high:
                        status, reason, blocked = 'invalid', '区间边界无效', True
                    elif rule.get('intervals', 'pending') == 'pending':
                        status, reason, blocked = 'pending', '区间含义或取值方式待确认', True
                    else:
                        status, reason = 'converted', 'interval_' + rule['intervals']
                        value = {'lower': low, 'upper': high, 'midpoint': low / 2 + high / 2}[rule['intervals']]
                else:
                    status, reason, blocked = 'invalid', '数值格式错误', True
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(value):
                        value, status, reason, blocked = None, 'invalid', '数值非有限', True
                    elif ((rule.get('lower') is not None and value < rule['lower']) or
                          (rule.get('upper') is not None and value > rule['upper'])):
                        status, reason, blocked = 'invalid', '超出已定义业务范围', True
                records.append({'row': int(row), 'source_row': int(row),
                    'source_line': int(row) + mapping.get('header_row', 0) + mapping.get('skip_after_header', 0) + 2,
                    'column': column, 'raw': text, 'value': value, 'status': status, 'reason': reason,
                    'blocked': blocked, 'interval': bounds, 'unit': rule.get('unit', '')})
    for column, rule in rules.items():
        if column in frame:
            cells = {r['row']: r for r in records if r['column'] == column}
            values = [np.nan if cells[i]['blocked'] or cells[i]['value'] is None else cells[i]['value'] for i in frame.index]
            dtype = float if rule.get('dtype', 'number') == 'number' or rule.get('ordered_values') else object
            frame[column] = pd.Series(values, index=frame.index, dtype=dtype)
    frame.attrs['data_state'] = {'rules': rules, 'rows': len(frame), 'cells': records, 'mapping': mapping}
    return frame


def data_state_summary(frame):
    from collections import Counter
    state = frame.attrs.get('data_state')
    if not state:
        return {}
    # Derived/filtered frames retain original row identifiers in their state.
    ids = list(frame['_sample'] if '_sample' in frame else frame.index)
    positions = dict(zip(ids, frame.index))
    cells = [r for r in state['cells'] if r['row'] in positions and r['column'] in frame]
    for i, record in enumerate(cells):
        if not record['blocked'] and clean(frame.at[positions[record['row']], record['column']]) != record['value']:
            cells[i] = {**record, 'status': 'converted', 'reason': 'processed_value'}
    issues = [r for r in cells if r['status'] != 'valid']
    columns = {}
    for column, rule in state['rules'].items():
        records = [r for r in cells if r['column'] == column]
        if column not in frame:
            continue
        counts = Counter(r['status'] for r in records)
        columns[column] = {
            'counts': {key: counts[key] for key in ('valid', 'converted', 'missing', 'pending', 'invalid')},
            'missing_reasons': dict(Counter(r['reason'] for r in records if r['status'] == 'missing')),
            'invalid_reasons': dict(Counter(r['reason'] for r in records if r['status'] == 'invalid')),
            'parsed_unavailable': int(frame[column].isna().sum()),
            'category_labels': rule.get('category_labels', {}),
        }
    return {'counts': dict(Counter(r['status'] for r in cells)),
            'columns': columns, 'blocked_cells': sum(r['blocked'] for r in cells),
            'issues_preview': issues[:30], 'field_rules': state['rules'],
            'note': 'valid/converted/missing/pending/invalid 互斥，逐列相加为数据行数。missing 仅为已识别缺失；parsed_unavailable 是解析后不可用总数，不能称作缺失。'}


def missing_counts(frame, columns):
    state = data_state_summary(frame).get('columns', {})
    return {name: state[name]['counts']['missing'] if name in state else int(frame[name].isna().sum()) for name in columns}


def encoded_category_labels(rule):
    names = rule.get('category_labels', {})
    if rule.get('ordered_values') and names:
        return {key: names.get(code, '含义待确认') for i, code in enumerate(rule['ordered_values']) for key in (str(i), str(float(i)))}
    return names


def category_labels(config):
    mapping = config['mapping']
    return encoded_category_labels(mapping.get('field_rules', {}).get(mapping.get('target'), {}))


def blocked_data_rows(frame, columns, require_observed=False):
    reasons = {}
    for cell in frame.attrs.get('data_state', {}).get('cells', []):
        if cell['column'] in columns and cell['blocked']:
            reasons.setdefault(cell['row'], []).append(f"{cell['column']}：{cell['reason']}（原值 {cell['raw']!r}）")
    if require_observed and frame.attrs.get('data_state'):
        selected = [c for c in columns if c in frame and c != '_sample']
        if selected:
            empty = frame[selected].isna().all(axis=1)
            for row in frame.loc[empty, '_sample']:
                reasons.setdefault(int(row), []).append('所有选用特征均无可用观测值')
    return reasons


def write_data_state(frame, output):
    state = frame.attrs.get('data_state')
    if not state:
        return
    # _sample keeps original positions after excluding training rows.
    ids = frame['_sample'].tolist() if '_sample' in frame else list(frame.index)
    positions = {int(old): new for new, old in enumerate(ids)}
    cells = []
    for r in state['cells']:
        if r['row'] not in positions or r['column'] not in frame:
            continue
        cell = {**r, 'row': positions[r['row']]}
        # Scalar access avoids pandas copying the complete attrs state for each row.
        value = clean(frame.at[frame.index[cell['row']], r['column']])
        if not r['blocked'] and value != r['value']:
            cell.update(value=value, status='converted', reason='processed_value')
        cells.append(cell)
    processed = Path(output) / 'processed.csv'
    save(Path(output) / 'data-state.json', {**state, 'rows': len(frame), 'cells': cells,
         **({'mapping': {**state['mapping'], 'sheet': 0, 'header_row': 0, 'skip_after_header': 0}} if processed.is_file() else {}),
         **({'table_sha256': hashlib.sha256(processed.read_bytes()).hexdigest()} if processed.is_file() else {})})
    summary = data_state_summary(frame)
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['# 字段数据状态', '', summary['note'], '',
             '| 字段 | 有效 | 已转换 | 缺失 | 待确认 | 格式或范围错误 | 解析后不可用合计 |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for column, info in summary['columns'].items():
        lines.append('| ' + ' | '.join(map(cell, [column, *info['counts'].values(), info['parsed_unavailable']])) + ' |')
    lines += ['', '## 已声明的类别含义', '', '| 字段 | 原编码 | 业务含义 |', '|---|---|---|']
    for column, rule in state['rules'].items():
        for code, name in rule.get('category_labels', {}).items():
            lines.append('| ' + ' | '.join(map(cell, [column, code, name])) + ' |')
    lines += ['', '缺失原因按 unknown（未知缺失原因）、not_measured（未测量）、not_applicable（不适用）、unjudged（未判定）分别计数：']
    for column, info in summary['columns'].items():
        if info['missing_reasons']:
            lines.append(f"- {cell(column)}：" + '；'.join(f'{cell(k)} = {v}' for k, v in info['missing_reasons'].items()))
    Path(output, 'data-summary.md').write_text('\n'.join(lines) + '\n')


def profile(config):
    import numpy as np
    import pandas as pd
    mapping = config['mapping']
    frame = read_data({**config, 'labels': ''}, preview=config.get('sampled', False))
    state = data_state_summary(frame)
    columns = []
    for name in frame.columns:
        series = frame[name]
        info = {'name': str(name), 'dtype': str(series.dtype), 'missing': int(series.isna().sum()), 'unique': int(series.nunique())}
        if name in state.get('columns', {}):
            info.update(state['columns'][name])
            info['missing'] = info['counts']['missing']
        if info['unique'] <= 12:
            names = encoded_category_labels(mapping.get('field_rules', {}).get(name, {}))
            info['value_counts'] = [{'value': clean(k) if isinstance(k, (str, int, float, bool, np.generic)) else str(k),
                                    'count': int(v), **({'name': names.get(str(k), '含义待确认')} if names else {})}
                                    for k, v in series.value_counts().items()]
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
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
    empty = frame.isna().all(axis=1)
    for record in frame.attrs.get('data_state', {}).get('cells', []):
        if str(record['raw']).strip():
            empty.at[record['row']] = False
    empty_rows = int(empty.sum())
    result = {'sampled': config.get('sampled', False), 'rows': len(frame), 'empty_rows': empty_rows, 'nonempty_rows': len(frame) - empty_rows,
              'duplicates': int(frame.duplicated().sum()), 'columns': columns,
              'preview': frame.head(12).astype(object).where(pd.notna(frame.head(12)), None).to_dict('records')}
    if frame.attrs.get('data_state'):
        result['data_state'] = state
        write_data_state(frame, config['output'])
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
    target = mapping.get('target')
    if target and target in frame:
        labeled = frame.loc[frame[target].notna()]
        structure = {'target': target, 'labeled_rows': len(labeled),
                     'missing_labels': int(frame[target].isna().sum())}
        if target in state.get('columns', {}):
            structure.update(label_state_counts=state['columns'][target]['counts'],
                             unavailable_labels=int(frame[target].isna().sum()))
        group = mapping.get('group_column') or mapping.get('id_column')
        if group and group in labeled:
            counts = labeled[group].value_counts()
            repeated = counts[counts > 1]
            structure.update(group_column=group, groups=len(counts), repeated_groups=len(repeated),
                             rows_in_repeated_groups=int(repeated.sum()), missing_groups=int(labeled[group].isna().sum()))
            if len(repeated):
                scope = '当前预览' if config.get('sampled', False) else '有标签数据'
                result['warnings'] = [f'{scope}中，{group} 有 {len(repeated)} 个重复组，涉及 {int(repeated.sum())} 行；'
                    '随机划分可能让同一对象同时进入训练和验证。请使用 evaluation.split="group"；'
                    '只有预测时点明确时才选择时间向前验证。']
        result['sample_structure'] = structure
    if config.get('labels'):
        result['labels'] = profile({**config, 'source': config['labels'], 'labels': ''})
    return clean(result)


def analyze(config):
    """Explicit numeric operations on a single table, independent of training."""
    import numpy as np
    import pandas as pd
    from scipy.stats import spearmanr

    plan = config['analysis']
    operation = plan['operation']
    if operation == 'profile':
        return profile(config)
    if config['mapping'].get('kind', 'tabular') != 'tabular' or config.get('labels'):
        raise ValueError('统计与异常值操作需要单张表格；请先明确整理时序或独立标签表')
    columns = plan['columns']
    minimum = 0 if operation == 'prepare' else 2 if operation == 'spearman' else 1
    if len(columns) < minimum:
        raise ValueError(f'请明确选择至少 {minimum} 个数值分析列')
    frame = read_data(config, literal_columns=columns if operation == 'numeric' else ())
    unknown = [name for name in columns if name not in frame]
    if unknown:
        raise ValueError('分析列不存在：' + '、'.join(unknown))
    if operation == 'prepare':
        if not frame.attrs.get('data_state') or not set(config['mapping'].get('field_rules', {})).intersection(frame.columns):
            raise ValueError('整理表格前请在 mapping.field_rules 声明需要保留的字段含义；已有数据概况可继续直接使用')
        indices = plan.get('row_indices')
        if indices is not None and (len(set(indices)) != len(indices) or any(i < 0 or i >= len(frame) for i in indices)):
            raise ValueError('所选行号越界或重复；row_indices 是当前数据集从 0 开始的数据行号')
        processed = frame if indices is None else frame.iloc[indices].copy()
        output = Path(config['output']); output.mkdir(parents=True, exist_ok=True)
        processed.to_csv(output / 'processed.csv', index=False)
        write_data_state(processed, output)
        state = json.loads((output / 'data-state.json').read_text())
        origins = {r['row']: r for r in state['cells']}
        pd.DataFrame([{'row': i, 'source_row': origins[i]['source_row'], 'source_line': origins[i]['source_line']} for i in range(len(processed))],
                     columns=['row', 'source_row', 'source_line']).to_csv(output / 'source-rows.csv', index=False)
        result = {'operation': operation, 'rows': len(frame), 'output_rows': len(processed), 'excluded_rows': len(frame)-len(processed),
                  'mapping': state['mapping'], 'data_state': data_state_summary(processed),
                  'row_numbering': 'row 是本次结果行；source_row/source_line 沿用原始数据/原表行号。未选择 row_indices 时所有行（含空行）保留。'}
        save(output / 'analysis.json', result)
        return clean(result)
    if operation == 'numeric':
        ruled = config['mapping'].get('field_rules', {})
        processed, changes = prepare_numeric(frame, [c for c in columns if c not in ruled], plan.get('numeric', {}))
        output = Path(config['output']); output.mkdir(parents=True, exist_ok=True)
        processed.to_csv(output / 'processed.csv', index=False)
        pd.DataFrame(changes, columns=['source_row', 'column', 'original', 'value', 'conversion']).to_csv(output / 'conversions.csv', index=False)
        pd.DataFrame({'source_row': processed.index}).to_csv(output / 'source-rows.csv', index=False)
        result = clean({'operation': operation, 'rows': len(frame), 'output_rows': len(processed), 'columns': columns,
            'converted_cells': len(changes), 'affected_rows': len({item['source_row'] for item in changes}),
            'missing_counts': missing_counts(processed, columns),
            'parsed_unavailable_counts': {name: int(processed[name].isna().sum()) for name in columns},
            'preview': processed.head(12).astype(object).where(processed.head(12).notna(), None).to_dict('records'),
            'row_numbering': 'source_row 从 0 开始，指向按表头设置读取后的数据行',
            'warnings': ['区间值按所选规则转换；规则应依据数据含义确定。'] if any(c['conversion'].startswith('interval_') for c in changes) else []})
        save(output / 'analysis.json', result)
        if ruled:
            result['data_state'] = data_state_summary(frame)
            write_data_state(processed, output)
            save(output / 'analysis.json', result)
        return result
    invalid = [name for name in columns if not pd.api.types.is_numeric_dtype(frame[name])
               or pd.api.types.is_bool_dtype(frame[name])]
    if invalid:
        raise ValueError('分析列必须为数值，不能自动转换文本或布尔列：' + '、'.join(invalid))
    selected = frame[columns].astype(float)
    if np.isinf(selected.to_numpy()).any():
        raise ValueError('分析列含无穷值，请先修正数据')
    output = Path(config['output']); output.mkdir(parents=True, exist_ok=True)
    result = {'operation': operation, 'rows': len(frame), 'columns': columns,
              'missing_counts': missing_counts(frame, columns),
              'parsed_unavailable_counts': {name: int(selected[name].isna().sum()) for name in columns}}
    if frame.attrs.get('data_state'):
        result['data_state'] = data_state_summary(frame)
        write_data_state(frame, output)
    if operation == 'spearman':
        missing = plan['missing']
        if missing == 'raise' and selected.isna().any().any():
            raise ValueError('分析列存在缺失值；请选择成对删除或整行删除')
        if missing == 'complete':
            selected = selected.dropna()
        size = len(columns)
        matrix = [[None] * size for _ in columns]
        counts = [[0] * size for _ in columns]
        pairs = []
        for i, left in enumerate(columns):
            for j in range(i, size):
                right = columns[j]
                valid = selected[left].notna() & selected[right].notna()
                x, y = selected.loc[valid, left], selected.loc[valid, right]
                n = len(x)
                reason, rho, p = '', None, None
                if n < 3:
                    reason = '有效样本少于 3'
                elif x.nunique() < 2 or y.nunique() < 2:
                    reason = '包含常数列'
                elif i == j:
                    rho = 1.0
                else:
                    statistic = spearmanr(x, y)
                    rho, p = float(statistic.statistic), float(statistic.pvalue)
                matrix[i][j] = matrix[j][i] = rho
                counts[i][j] = counts[j][i] = n
                if i != j:
                    pairs.append({'left': left, 'right': right, 'rho': rho, 'p_value': p,
                                  'samples': n, 'status': 'undefined' if reason else 'ok', 'reason': reason})
                    if frame.attrs.get('data_state'):
                        excluded = blocked_data_rows(frame, [left, right])
                        pairs[-1]['excluded_unusable_rows'] = len(excluded)
        result.update(missing=missing, correlation=matrix, sample_counts=counts, pairs=pairs,
                      warnings=['p 值使用渐近近似且未经多重比较校正；小样本结论需要进一步统计检验。'])
        pd.DataFrame(pairs).to_csv(output / 'correlations.csv', index=False)
    elif operation == 'outliers':
        method, handling = plan['method'], plan['handling']
        if method == 'bounds' and plan['lower'] is None and plan['upper'] is None:
            raise ValueError('固定边界方法需要至少填写一个边界')
        flags = pd.DataFrame(False, index=frame.index, columns=columns)
        bounds, details, warnings = {}, [], []
        processed = frame.copy()
        for name in columns:
            series = selected[name]
            finite = series.dropna()
            if finite.empty:
                raise ValueError(f'分析列 {name} 没有有效数值')
            if method == 'iqr':
                q1, q3 = finite.quantile([.25, .75])
                width = (q3 - q1) * plan['iqr_factor']
                low, high = float(q1 - width), float(q3 + width)
                if q3 == q1:
                    effect = {'flag': '标记为异常', 'drop': '删除所在行', 'clip': f'截成 {low:g}'}[handling]
                    warnings.append(f'字段 {name} 的四分位距为 0（Q1 = Q3 = {low:g}）；'
                                    f'按当前 IQR 规则，其他取值都会{effect}。这不能单独证明它们是业务异常，请结合工况检查。')
            else:
                low, high = plan['lower'], plan['upper']
            bounds[name] = {'lower': low, 'upper': high}
            mask = ((series < low) if low is not None else False) | ((series > high) if high is not None else False)
            flags[name] = mask
            for index in frame.index[flags[name]]:
                details.append({'source_row': int(index), 'column': name, 'value': float(series.loc[index]),
                                'lower': low, 'upper': high})
            if handling == 'clip':
                processed[name] = series.clip(lower=low, upper=high)
        affected = flags.any(axis=1)
        if handling == 'drop':
            processed = processed.loc[~affected]
        if handling != 'flag':
            if processed.empty:
                raise ValueError('异常值处理会删除全部样本，请调整边界')
            processed.to_csv(output / 'processed.csv', index=False)
            write_data_state(processed, output)
        pd.DataFrame(details, columns=['source_row', 'column', 'value', 'lower', 'upper']).to_csv(output / 'outliers.csv', index=False)
        pd.DataFrame({'source_row': processed.index}).to_csv(output / 'source-rows.csv', index=False)
        result.update(method=method, handling=handling, bounds=bounds, affected_rows=int(affected.sum()),
                      affected_cells=int(flags.to_numpy().sum()), output_rows=len(processed),
                      row_numbering='source_row 从 0 开始，指向原始表格的数据行，不包含表头',
                      preview=processed.head(12).astype(object).where(processed.head(12).notna(), None).to_dict('records'),
                      warnings=warnings + (['IQR 边界由本次全表估计；模型评价中的自适应处理应在训练折内拟合。'] if method == 'iqr' else []))
    else:
        raise ValueError('未知数据分析操作')
    save(output / 'analysis.json', result)
    return clean(result)


def samples(config, prediction=False):
    import numpy as np
    import pandas as pd
    mapping = config['mapping']
    numeric_columns = config.get('features', {}).get('numeric', {}).get('columns', []) if mapping.get('kind', 'tabular') == 'tabular' else []
    frame = read_data(config, literal_columns=numeric_columns)
    frame['_sample'] = np.arange(len(frame))
    target = mapping.get('target')
    missing = 0
    if not prediction:
        if not target or target not in frame:
            raise ValueError('请指定实际存在的目标列；时序目标须在标签表中')
        missing = int(frame[target].isna().sum())
        if target in frame.attrs.get('data_state', {}).get('rules', {}):
            frame.attrs['label_state_counts'] = data_state_summary(frame)['columns'][target]['counts']
        frame = frame.loc[frame[target].notna()].copy()
        roles = {mapping.get(k) for k in ('target', 'id_column', 'group_column', 'time_column', 'prediction_time_column', 'available_time_column', 'label_available_time_column')}
        plan = config.get('features', {})
        active = set(plan.get('columns') or frame.columns) - set(plan.get('exclude', [])) - roles - {'_sample'}
        unusable = blocked_data_rows(frame, active, require_observed=True)
        affected = frame['_sample'].isin(unusable)
        if affected.any() and mapping.get('invalid_rows', 'error') != 'exclude':
            first = int(frame.loc[affected, '_sample'].iloc[0])
            raise ValueError('训练特征存在待确认或无效值：' + '；'.join(unusable[first]) + '；请明确 mapping.invalid_rows="exclude" 排除这些行，或修正字段规则')
        frame.attrs['excluded_data_rows'] = int(affected.sum())
        frame = frame.loc[~affected].copy()
        if len(frame) < 8:
            raise ValueError(f'至少需要 8 个可用有标签样本；按字段规则排除 {int(affected.sum())} 条后剩 {len(frame)} 条，请检查标签和实际选用的特征')
        if config.get('evaluation', {}).get('problem') == 'regression':
            frame[target] = pd.to_numeric(frame[target], errors='raise')
            if not np.isfinite(frame[target]).all():
                raise ValueError('回归标签包含无穷值')
    for key in ('id_column', 'group_column'):
        col = mapping.get(key)
        # Tabular prediction does not split by identity. Keep unknown identities
        # and blank records in place; source positions identify every output row.
        require_identity = not prediction or mapping.get('kind') == 'timeseries'
        if col and (col not in frame or (require_identity and frame[col].isna().any())):
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
    for index, (train, valid) in enumerate(folds):
        if len(train) < 2 or len(valid) < 2:
            raise ValueError('隔离组别／时间后样本不足，请补充数据或降低折数')
        if rule['problem'] == 'classification' and y.iloc[train].nunique() < 2:
            raise ValueError(f'第 {index + 1} 折的训练数据少于两类，当前划分不支持分类训练；请检查分组／时间与类别分布')
    return {'samples': frame['_sample'].tolist(), 'missing_labels': missing, 'folds': [[a.tolist(), b.tolist()] for a, b in folds],
            **({'label_state_counts': frame.attrs['label_state_counts'], 'unavailable_labels': missing} if 'label_state_counts' in frame.attrs else {}),
            'development': indices.tolist(), 'holdout': holdout.tolist(), 'evaluation_label': '保留测试集（尚未使用）' if len(holdout) else '仅验证集结果',
            **({'excluded_data_rows': frame.attrs.get('excluded_data_rows', 0), 'data_state': data_state_summary(frame)} if mapping.get('field_rules') else {})}


def features(config, prediction=False):
    import numpy as np
    import pandas as pd
    frame, _ = samples(config, prediction)
    mapping, plan = config['mapping'], config.get('features', {})
    metadata = {mapping.get(k) for k in ('target', 'id_column', 'group_column', 'time_column', 'prediction_time_column', 'available_time_column', 'label_available_time_column')}
    descriptions = []
    if mapping['kind'] == 'timeseries':
        if plan.get('numeric', {}).get('columns'):
            raise ValueError('训练数值整理当前适用于表格特征；请先整理时序测量表')
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
        numeric = plan.get('numeric', {})
        if numeric.get('columns'):
            x, _ = prepare_numeric(x, [c for c in numeric['columns'] if c not in mapping.get('field_rules', {})], numeric)
        x = x.replace([np.inf, -np.inf], np.nan)
        descriptions = [{'name': c, 'source': c, 'calculation': '原始字段；缺失填补及编码仅在训练折拟合'} for c in x]
        for item in descriptions:
            if item['name'] in numeric.get('columns', []):
                item.update(calculation='按固定规则整理数值；缺失填补及编码仅在训练折拟合', numeric_rules=numeric)
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


def export_features(config):
    """Export the same deterministic feature cache consumed by training."""
    x, frame, descriptions = cached_features(config)
    row_key = '__sample_index'
    while row_key in x or row_key in frame:
        row_key += '_'
    table = x.copy()
    table.insert(0, row_key, frame['_sample'].to_numpy())
    mapping = config['mapping']
    metadata = list(dict.fromkeys(mapping.get(k) for k in ('id_column', 'group_column',
        'prediction_time_column', 'label_available_time_column', 'target') if mapping.get(k) in frame))
    samples_table = frame[metadata].copy()
    samples_table.insert(0, row_key, frame['_sample'].to_numpy())
    folder = Path(config['output'])
    table.to_csv(folder / 'features.csv', index=False, encoding='utf-8-sig')
    samples_table.to_csv(folder / 'samples.csv', index=False, encoding='utf-8-sig')
    source_rows = len(read_table(config.get('labels') or config['source'], mapping))
    result = {'rows': len(x), 'columns': len(x.columns), 'features': descriptions,
        'row_key': row_key, 'source_rows': source_rows,
        'excluded': [{'reason': '目标标签缺失', 'rows': source_rows - len(frame)}],
        'preview': clean(table.iloc[:12, :11].to_dict('records')),
        'stage': 'before_fold_preprocessing',
        'note': '此表是确定性特征。缺失值填补、类别编码、标准化及特征筛选仍在每个训练折内拟合；标签只在样本表中。样本序号对应原始样本表从0开始的行序。'}
    save(folder / 'feature-summary.json', result)
    if mapping.get('field_rules'):
        result['data_state'] = data_state_summary(frame)
        result['excluded'] = [{'reason': '目标标签缺失或不可用', 'rows': source_rows - len(frame) - frame.attrs.get('excluded_data_rows', 0)},
                              {'reason': '特征待确认或无效', 'rows': frame.attrs.get('excluded_data_rows', 0)}]
        write_data_state(frame, folder)
        save(folder / 'feature-summary.json', result)
    return result


def preprocessing_module():
    # Keep the pickle module name identical in the package, CPU image and zip.
    name = 'modeling_preprocessing'
    if name not in sys.modules:
        path = Path(__file__).with_name(name + '.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def preprocessing_settings(config):
    requested = config.get('features', {}).get('preprocessing') or {}
    native = config.get('engine') == 'autogluon'
    missing = requested.get('numeric_missing', 'auto')
    categorical = requested.get('categorical_missing', 'auto')
    scaling = requested.get('scaling', 'auto')
    result = {'numeric_missing': ('native' if native else 'median') if missing == 'auto' else missing,
              'categorical_missing': ('native' if native else 'most_frequent') if categorical == 'auto' else categorical,
              'fill_value': requested.get('fill_value', 0),
              'scaling': ('none' if native else 'standard') if scaling == 'auto' else scaling,
              'outliers': requested.get('outliers', 'none'), 'outlier_columns': requested.get('outlier_columns', []),
              'iqr_factor': requested.get('iqr_factor', 1.5)}
    if result['numeric_missing'] not in {'native', 'median', 'mean', 'constant', 'error'} or result['categorical_missing'] not in {'native', 'most_frequent', 'preserve'} or result['scaling'] not in {'none', 'standard', 'robust'} or result['outliers'] not in {'none', 'iqr_clip'}:
        raise ValueError('不支持的训练预处理选项')
    return result


def numeric_pipeline(config):
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler, RobustScaler
    from sklearn.impute import SimpleImputer
    settings = preprocessing_settings(config)
    missing = settings['numeric_missing']
    if missing == 'error':
        fill = preprocessing_module().RequireCompleteNumeric()
    elif missing == 'native':
        fill = 'passthrough'
    else:
        fill = SimpleImputer(strategy=missing, fill_value=settings['fill_value'], keep_empty_features=True)
    scale = {'none': 'passthrough', 'standard': StandardScaler(), 'robust': RobustScaler()}[settings['scaling']]
    return Pipeline([('fill', fill), ('scale', scale)])


def outlier_steps(config):
    settings = preprocessing_settings(config)
    if settings['outliers'] == 'none':
        return []
    return [('outliers', preprocessing_module().IQRClipper(columns=settings['outlier_columns'], factor=settings['iqr_factor']))]


def categorical_pipeline(config):
    from sklearn.pipeline import Pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import OneHotEncoder
    fill = ('passthrough' if preprocessing_settings(config)['categorical_missing'] == 'preserve'
            else SimpleImputer(strategy='most_frequent', keep_empty_features=True))
    return Pipeline([('fill', fill), ('encode', OneHotEncoder(handle_unknown='ignore', sparse_output=False))])


def autogluon_preprocessor(config):
    from sklearn.compose import ColumnTransformer, make_column_selector
    from sklearn.pipeline import Pipeline
    settings = preprocessing_settings(config)
    preserve = settings['categorical_missing'] == 'preserve'
    if settings['numeric_missing'] == 'native' and settings['scaling'] == 'none' and settings['outliers'] == 'none' and not preserve:
        return None
    transforms = [('numeric', numeric_pipeline(config), make_column_selector(dtype_include='number'))]
    if preserve:
        transforms.append(('categorical', categorical_pipeline(config), make_column_selector(dtype_exclude='number')))
    prep = ColumnTransformer(transforms, remainder='passthrough', verbose_feature_names_out=preserve).set_output(transform='pandas')
    return Pipeline([*outlier_steps(config), ('preprocess', prep)])


def autogluon_prediction_input(config, x):
    import joblib
    path = Path(config['model']) / 'preprocessing.joblib'
    if path.is_file():
        return joblib.load(path).transform(x)
    if autogluon_preprocessor(config) is not None:
        raise ValueError('模型缺少已拟合的预处理文件 preprocessing.joblib；请使用完整模型包，不能在预测数据上重新拟合')
    return x


def fitted_preprocessing(fitted, rows):
    if fitted is None:
        return {'rows': rows, 'managed_by': 'autogluon'}
    prep = fitted.named_steps['preprocess']
    columns = next(list(columns) for name, _, columns in prep.transformers_ if name == 'numeric')
    numeric = prep.named_transformers_['numeric']
    clipper = fitted.named_steps.get('outliers')
    result = {'rows': rows, 'numeric_columns': columns, 'outlier_bounds': clipper.bounds_ if clipper else {},
              'warnings': getattr(clipper, 'warnings_', [])}
    for label, step, attributes in [('fill_values', 'fill', ('statistics_',)),
                                    ('center', 'scale', ('mean_', 'center_')), ('scale', 'scale', ('scale_',))]:
        for attribute in attributes:
            values = getattr(numeric.named_steps[step], attribute, None)
            if values is not None:
                result[label] = dict(zip(columns, values.tolist()))
                break
    return clean(result)


def pipeline(x, config, model):
    from sklearn.compose import ColumnTransformer
    from sklearn.pipeline import Pipeline
    from sklearn.feature_selection import SelectKBest, f_regression, f_classif
    steps = []
    if config.get('transformer'):
        spec = importlib.util.spec_from_file_location('project_transformer', config['transformer'])
        module = importlib.util.module_from_spec(spec)
        sys.modules['project_transformer'] = module
        spec.loader.exec_module(module)
        steps.append(('project_features', module.build_transformer()))
    steps.extend(outlier_steps(config))
    # Dtype selectors run at fit time, including after a project transformer.
    from sklearn.compose import make_column_selector
    prep = ColumnTransformer([
        ('numeric', numeric_pipeline(config), make_column_selector(dtype_include='number')),
        ('categorical', categorical_pipeline(config), make_column_selector(dtype_exclude='number')),
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


def classification_details(y, predictions, classes, names=None):
    from sklearn.metrics import precision_recall_fscore_support, confusion_matrix
    labels = list(dict.fromkeys([*classes, *y, *predictions]))
    precision, recall, f1, support = precision_recall_fscore_support(y, predictions, labels=labels, zero_division=0)
    matrix = confusion_matrix(y, predictions, labels=labels)
    predicted = matrix.sum(axis=0)
    return {'classes': [{'label': str(label), **({'name': names.get(str(label), '含义待确认')} if names else {}),
                        'precision': float(precision[i]) if predicted[i] else None,
                        'recall': float(recall[i]) if support[i] else None,
                        'f1': float(f1[i]) if support[i] + predicted[i] else None,
                        'samples': int(support[i]), 'predicted_samples': int(predicted[i])} for i, label in enumerate(labels)],
            'confusion_matrix': matrix.tolist(),
            'note': '混淆矩阵的行表示实际类别，列表示预测类别，顺序与类别表一致。宏平均 F1 使用固定类别范围，未定义项按 0 计入；无法计算的指标单独标明。'}


def validation_classification(y, predictions, baseline_predictions, classes, frame_y, split, names=None):
    """Describe only development/fold rows; held-out labels never enter diagnostics."""
    result = classification_details(y, predictions, classes, names)
    result['baseline'] = classification_details(y, baseline_predictions, classes, names)
    counts = frame_y.iloc[split['development']].value_counts()
    for row, label in zip(result['classes'], classes):
        row['development_samples'] = int(counts.get(label, 0))
    result['folds'] = []
    warnings = []
    rare = [f'{row["label"]}（{row["development_samples"]} 条）' for row in result['classes']
            if row['development_samples'] < len(split['folds'])]
    if rare:
        warnings.append('开发数据中以下类别的样本数少于验证折数：' + '、'.join(rare) + '；请检查每折类别分布，少量样本不足以判断稳定效果。')
    for index, (train, valid) in enumerate(split['folds']):
        training, validation = frame_y.iloc[train].value_counts(), frame_y.iloc[valid].value_counts()
        rows = [{'label': str(label), **({'name': names.get(str(label), '含义待确认')} if names else {}), 'train_samples': int(training.get(label, 0)),
                 'validation_samples': int(validation.get(label, 0))} for label in classes]
        result['folds'].append({'fold': index + 1, 'classes': rows})
        unseen = [row['label'] for row in rows if not row['train_samples'] and row['validation_samples']]
        absent = [row['label'] for row in rows if not row['validation_samples']]
        if unseen:
            warnings.append(f'第 {index + 1} 折验证数据包含训练时未见过的类别：' + '、'.join(unseen) + '。')
        if absent:
            warnings.append(f'第 {index + 1} 折验证数据缺少类别：' + '、'.join(absent) + '；该折无法计算这些类别的召回率。')
    return result, warnings


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
    preprocessing_folds = []
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
            preprocessing = autogluon_preprocessor(config)
            train_x = preprocessing.fit_transform(x.iloc[train], y.iloc[train]) if preprocessing is not None else x.iloc[train]
            valid_x = preprocessing.transform(x.iloc[valid]) if preprocessing is not None else x.iloc[valid]
            # Outer validation is never passed to fit; AG tunes only on training.
            fitted = TabularPredictor(**ag_options, path=str(folder / f'ag-fold-{index}'))
            fitted.fit(train_x.assign(**{target: y.iloc[train].to_numpy()}), hyperparameters=ag_parameters, presets='medium_quality', time_limit=max(2, config['trial_seconds'] / (len(split['folds']) + 1)), num_cpus=4, num_gpus=0, memory_limit=3, num_bag_folds=0, num_stack_levels=0)
            pred = fitted.predict(valid_x).to_numpy()
            proba = fitted.predict_proba(valid_x).reindex(columns=classes, fill_value=0).to_numpy() if classes is not None else None
        else:
            fitted = pipeline(x, config, estimator(model_name, config, trial))
            fitted.fit(x.iloc[train], y.iloc[train])
            pred = fitted.predict(x.iloc[valid])
            proba = None
            if classes is not None:
                proba = pd.DataFrame(fitted.predict_proba(x.iloc[valid]), columns=fitted.classes_).reindex(columns=classes, fill_value=0).to_numpy()
            preprocessing = fitted
        preprocessing_folds.append({'fold': index + 1, **fitted_preprocessing(preprocessing, len(train))})
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
        preprocessing = autogluon_preprocessor(config)
        development_x = preprocessing.fit_transform(x.iloc[development], y.iloc[development]) if preprocessing is not None else x.iloc[development]
        model = TabularPredictor(**ag_options, path=str(folder / 'autogluon'))
        model.fit(development_x.assign(**{target: y.iloc[development].to_numpy()}), hyperparameters=ag_parameters, presets='medium_quality', time_limit=max(2, config['trial_seconds'] / (len(split['folds']) + 1)), num_cpus=4, num_gpus=0, memory_limit=3, num_bag_folds=0, num_stack_levels=0)
        if preprocessing is not None:
            joblib.dump(preprocessing, folder / 'preprocessing.joblib')
        info = json.loads(json.dumps(model.info(), default=str))
        save(folder / 'autogluon-models.json', info)
        selected = model.model_best
        effective = {'class': 'autogluon.tabular.TabularPredictor', 'selected_model': selected,
                     'parameters': info.get('model_info', {}).get(selected, {}), 'fit_hyperparameters': ag_parameters}
    else:
        model = pipeline(x, config, estimator(model_name, config, trial))
        model.fit(x.iloc[development], y.iloc[development])
        preprocessing = model
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
    preprocessing_result = {'settings': preprocessing_settings(config), 'development': fitted_preprocessing(preprocessing, len(development))}
    save(folder / 'preprocessing.json', {**preprocessing_result, 'folds': preprocessing_folds})
    constant_folds = [d['fold'] for d in diagnostics if d['constant_prediction']]
    warnings = [f'第 {"、".join(map(str, constant_folds))} 折对不同实测值给出了相同预测；请检查样本量、特征和参数。此提示不代表算法方向无效。'] if constant_folds else []
    for part in preprocessing_folds:
        warnings.extend(f'第 {part["fold"]} 折预处理：{message}' for message in part.get('warnings', []))
    warnings.extend('最终开发集预处理：' + message for message in preprocessing_result['development'].get('warnings', []))
    classification = None
    if classes is not None:
        classification, class_warnings = validation_classification(truth, predictions, baseline_predictions, classes, y, split, category_labels(config))
        warnings.extend(class_warnings)
    return {'metrics': metric, 'baseline': baseline, 'fold_metrics': fold_scores, 'diagnostics': diagnostics, 'warnings': warnings, 'group_errors': sorted(group_errors, key=lambda r: r['error'], reverse=True)[:50],
            'classification': classification,
            'preprocessing': preprocessing_result,
            'acceptance': acceptance, 'effective_model': effective, 'importance': importance, 'validation_rows': len(rows), 'prediction_preview': out.head(30).to_dict('records'), 'feature_columns': list(x.columns), 'classes': classes.tolist() if classes is not None else None}


def trial_resources(config, folder):
    """Measure this isolated trial process and the final saved prediction files."""
    import resource
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    peak = peak * 1024 if sys.platform == 'linux' else peak if sys.platform == 'darwin' else None
    names = ['autogluon', 'preprocessing.joblib'] if config['engine'] == 'autogluon' else ['model.joblib']
    paths = [folder / name for name in names if (folder / name).exists()]
    files = [file for path in paths for file in (path.rglob('*') if path.is_dir() else [path]) if file.is_file()]
    return {'peak_rss_bytes': peak, 'memory_scope': 'trial_process',
            'model_bytes': sum(file.stat().st_size for file in files) if files else None,
            'model_paths': [path.name for path in paths], 'model_scope': 'saved_prediction_files'}


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
    write_data_state(frame, output)
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
    import numpy as np
    import pandas as pd
    x, frame, _ = features(config, prediction=True)
    columns = config['feature_columns']
    if set(columns) != set(x.columns):
        raise ValueError('预测字段与训练特征不匹配')
    x = x[columns]
    blocked = blocked_data_rows(frame, columns, require_observed=True)
    usable = ~frame['_sample'].isin(blocked)
    input_x = x.loc[usable]
    if config.get('transformer'):
        spec = importlib.util.spec_from_file_location('project_transformer', config['transformer'])
        module = importlib.util.module_from_spec(spec); sys.modules['project_transformer'] = module; spec.loader.exec_module(module)
    if Path(__file__).with_name('modeling_preprocessing.py').exists():
        preprocessing_module()
    if config['engine'] == 'autogluon':
        from autogluon.tabular import TabularPredictor
        model = TabularPredictor.load(str(Path(config['model']) / 'autogluon'))
        input_x = autogluon_prediction_input(config, input_x) if len(input_x) else input_x
    else:
        model = joblib.load(Path(config['model']) / 'model.joblib')
    predictions = model.predict(input_x) if len(input_x) else []
    identifier = config['mapping'].get('id_column')
    identifier_column = identifier or '_sample'
    # User identifiers may have the same name as generated result columns. Keep
    # their values under an explicit input name instead of silently replacing them.
    reserved = {'prediction', 'prediction_name', 'source_row', 'source_line', 'prediction_status', 'prediction_reason', 'decision'}
    if identifier and (identifier in reserved or identifier.startswith('probability_')):
        identifier_column = 'input_' + identifier
    output = frame[[identifier or '_sample']].rename(columns={identifier: identifier_column}).copy()
    output['prediction'] = pd.Series(index=frame.index, dtype=object)
    output.loc[usable, 'prediction'] = np.asarray(predictions)
    names = category_labels(config)
    if names:
        output['prediction_name'] = None
        output.loc[usable, 'prediction_name'] = [names.get(str(value), '含义待确认') for value in predictions]
    if config['mapping'].get('field_rules'):
        origins = {c['row']: c for c in frame.attrs['data_state']['cells']}
        output['source_row'] = [origins[int(i)]['source_row'] for i in frame['_sample']]
        output['source_line'] = [origins[int(i)]['source_line'] for i in frame['_sample']]
        output['prediction_status'] = np.where(usable, 'predicted', 'unavailable')
        output['prediction_reason'] = ['；'.join(blocked.get(int(i), [])) for i in frame['_sample']]
    probability_columns = {}
    if config.get('classes'):
        probabilities = model.predict_proba(input_x) if len(input_x) else np.empty((0, len(config['classes'])))
        classes = list(probabilities.columns) if hasattr(probabilities, 'columns') else list(getattr(model, 'classes_', config['classes']))
        probabilities = np.asarray(probabilities)
        for i, label in enumerate(classes):
            column = 'probability_' + str(label)
            output[column] = np.nan
            output.loc[usable, column] = probabilities[:, i]
            probability_columns[str(label)] = column
    acceptance = None
    policy_path = Path(config['model']) / 'acceptance.json'
    if policy_path.exists():
        policy = json.loads(policy_path.read_text())
        accepted, acceptance = acceptance_result(policy, list(predictions), probabilities, classes)
        output['decision'] = 'unavailable'
        output.loc[usable, 'decision'] = ['accept_prediction' if keep else 'review' for keep in accepted]
    output.to_csv(Path(config['output']) / 'predictions.csv', index=False)
    write_data_state(frame, config['output'])
    return clean({'rows': len(output), 'predicted_rows': int(usable.sum()), 'unavailable_rows': int((~usable).sum()),
                  'identifier_column': identifier_column, 'input_identifier_column': identifier,
                  'preview': output.head(30).to_dict('records'), 'probability_columns': probability_columns, 'acceptance': acceptance,
                  **({'data_state': data_state_summary(frame)} if config['mapping'].get('field_rules') else {})})


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
    if Path(__file__).with_name('modeling_preprocessing.py').exists():
        preprocessing_module()
    if config['engine'] == 'autogluon':
        from autogluon.tabular import TabularPredictor
        model = TabularPredictor.load(str(Path(config['model']) / 'autogluon'))
        x = autogluon_prediction_input(config, x)
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
        result['classification'] = classification_details(y, pred, classes, category_labels(config))
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
    if Path(__file__).with_name('modeling_preprocessing.py').exists():
        preprocessing_module()
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
            result['resources'] = trial_resources(config, folder)
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
    elif action == 'analyze':
        result = analyze(config)
    elif action == 'prepare':
        result = prepare(config)
        save(Path(config['output']) / 'split.json', result)
    elif action == 'features':
        result = export_features(config)
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
