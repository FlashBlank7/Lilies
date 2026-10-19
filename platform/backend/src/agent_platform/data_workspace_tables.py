"""Bounded, paged views over the same table reader used for ML computation."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from pydantic import Field

from .modeling_models import Contract, DataMapping, TableFilter
from .modeling_worker import clean, data_state_summary, read_data, read_table
from .project_store import encode
from .table_selection import select_table


class Selection(Contract):
    columns: list[str] = Field(default_factory=list, max_length=500)
    filters: list[TableFilter] = Field(default_factory=list, max_length=30)
    row_indices: list[int] | None = None


class Preview(Contract):
    object_id: str
    version: str = ''
    mapping: DataMapping | None = None
    selection: Selection = Field(default_factory=Selection)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)
    x: str = ''
    y: str = ''


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class TableViews:
    def __init__(self):
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def read(self, config):
        files = {key: (Path(config[key]).stat().st_mtime_ns, Path(config[key]).stat().st_size)
                 for key in ('source', 'state', 'origins') if config.get(key)}
        key = digest([config, files])
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            frame = read_data(config)
            if len(set(frame.columns)) != len(frame.columns) or not all(isinstance(c, str) for c in frame.columns):
                raise ValueError('请使用唯一的文本列名')
            # Literal display is separate from parsed computation; NA and blank rows stay visible.
            raw = read_table(config['source'], config['mapping'], literal_columns=list(frame.columns))
            cells = frame.attrs.get('data_state', {}).get('cells', [])
            for cell in cells:
                raw.at[cell['row'], cell['column']] = cell['raw']
            self.cache[key] = (frame, raw)
            while len(self.cache) > 3 or (len(self.cache) > 1 and sum(f.size for f, _ in self.cache.values()) > 10_000_000):
                self.cache.popitem(last=False)
            return frame, raw

    def preview(self, config, request):
        import numpy as np
        import pandas as pd
        frame, raw = self.read(config)
        selected = select_table(frame, request.selection.model_dump())
        page = selected.iloc[request.offset:request.offset + request.limit]
        raw_page = raw.loc[page.index, page.columns]
        cells = frame.attrs.get('data_state', {}).get('cells', [])
        origins = frame.attrs.get('source_rows', {})
        by_row = {c['row']: c for c in cells if c['row'] in page.index}
        rows = json.loads(page.to_json(orient='records', date_format='iso'))
        raw_rows = json.loads(raw_page.to_json(orient='records', date_format='iso'))
        records = []
        for i, row in enumerate(page.index):
            source = by_row.get(row) or origins.get(str(row)) or {
                'source_row': int(row), 'source_line': int(row) + config['mapping'].get('header_row', 0) + config['mapping'].get('skip_after_header', 0) + 2}
            records.append({'row_id': int(row), 'source_row': source['source_row'], 'source_line': source['source_line'],
                            'values': rows[i], 'raw': raw_rows[i]})
        stats = []
        for column in selected:
            series = selected[column]
            numeric = pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)
            rule = config['mapping'].get('field_rules', {}).get(column, {})
            item = {'name': column, 'dtype': str(series.dtype), 'numeric': bool(numeric),
                    'unavailable': int(series.isna().sum()), 'rule': rule}
            if numeric and series.notna().any():
                values = series.dropna().astype(float)
                values = values[np.isfinite(values)]
                if len(values):
                    counts, edges = np.histogram(values, bins=12)
                    item['distribution'] = [{'label': f'{a:.4g}–{b:.4g}', 'count': int(c)} for a, b, c in zip(edges[:-1], edges[1:], counts)]
                    item.update(min=float(values.min()), max=float(values.max()), mean=float(values.mean()))
            else:
                counts = series.fillna('（不可用）').astype(str).value_counts().head(20)
                item['distribution'] = [{'label': rule.get('category_labels', {}).get(k, k), 'count': int(v)} for k, v in counts.items()]
                item['categories_truncated'] = int(series.nunique()) > 20
            stats.append(item)
        points = []
        population = 0
        if request.x and request.y:
            if request.x not in selected or request.y not in selected:
                raise ValueError('散点图字段不在所选数据中')
            if request.x == request.y:
                raise ValueError('请选择两个不同的散点图字段')
            xy = selected[[request.x, request.y]].apply(pd.to_numeric, errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
            population = len(xy)
            if len(xy) > 1000:
                xy = xy.sample(1000, random_state=42).sort_index()
            points = [{'row_id': int(i), 'x': float(xy.at[i, request.x]), 'y': float(xy.at[i, request.y])} for i in xy.index]
        sheets = []
        if Path(config['source']).suffix.lower() == '.xlsx':
            with pd.ExcelFile(config['source']) as workbook:
                sheets = workbook.sheet_names
        return clean({'rows': records, 'total_rows': len(frame), 'selected_rows': len(selected),
                      'offset': request.offset, 'limit': request.limit, 'columns': list(frame.columns),
                      'selected_columns': list(selected.columns), 'statistics': stats, 'points': points,
                      'points_sampled': population > 1000, 'points_population': population,
                      'states': data_state_summary(selected),
                      'cells': [c for c in cells if c['row'] in page.index and c['column'] in page],
                      'sheets': sheets, 'mapping': deepcopy(config['mapping'])})
