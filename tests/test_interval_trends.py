import csv
import json
from pathlib import Path

import pytest

from agent_platform import interval_trends as template, interval_trends_code as code
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('requirement-package').mkdir()
    defaults = {f['name']: f['default'] for f in template.workflow()['nodes'][0]['config']['inputs']}
    defaults.update(source_path='requirement-package/history.csv', history_periods=2, windows=3,
                    first_origin='2025-02', last_origin='2025-02', threshold=1)
    Path(defaults['source_path']).write_text('period,value\n2025-01,10\n2025-02,14\n2025-03,14\n2025-04,13\n2025-05,10\n')
    return defaults


def rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def table(result, suffix):
    return rows(next(a['file_path'] for a in result['artifacts'] if a['file_path'].endswith(suffix)))


def test_original_mean_rule_boundary_provenance_and_feature_separation(scenario):
    result = code.main(scenario)
    labels = table(result, 'label-origins.csv')
    assert [r['target'] for r in labels] == ['上涨', '平稳', '下跌']
    assert [float(r['reference_mean']) for r in labels] == [12, 14, 13]
    assert [float(r['future_mean']) for r in labels] == [14, 13, 10]
    assert json.loads(labels[0]['feature_records']) == [2, 3]
    assert json.loads(labels[0]['future_records']) == [4]
    samples = rows(result['source_path'])
    assert result['rows'] == result['prediction_rows'] == 3
    assert result['class_counts'] == {'上涨': 1, '平稳': 1, '下跌': 1}
    assert all(float(r['lag_0']) == 14 and float(r['lag_1']) == 10 for r in samples)
    assert not {'future_mean', 'reference_mean', 'change'} & samples[0].keys()
    assert 'target' not in rows(result['prediction_path'])[0]
    assert samples[0]['origin_time'].startswith('2025-03-01')
    assert samples[2]['label_available_time'].startswith('2025-06-01')


def test_decimal_boundary_and_zero_negative_values(scenario):
    Path(scenario['source_path']).write_text('period,value\n2025-01,0.1\n2025-02,0.2\n2025-03,0.35\n2025-04,0\n2025-05,-0.2\n')
    result = code.main({**scenario, 'threshold': 0.2})
    assert [r['target'] for r in rows(result['source_path'])] == ['平稳', '下跌', '平稳']


def test_calendar_months_groups_and_source_versions(scenario):
    frame = rows(scenario['source_path'])
    for i, row in enumerate(frame):
        row.update(available=f'2025-{i+2:02d}-01', series='A')
    # Historical revision is not available at the original prediction time.
    frame.append(dict(period='2025-02', value=999, available='2025-07-01', series='A'))
    # A later future-label revision must move label availability as well.
    frame.append(dict(period='2025-03', value=20, available='2025-08-01', series='A'))
    other = [{**row, 'series': 'B', 'value': 100} for row in frame[:5]]
    with Path(scenario['source_path']).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(frame[0]))
        writer.writeheader(); writer.writerows(frame + other)
    result = code.main({**scenario, 'available_column': 'available', 'series_column': 'series'})
    samples = rows(result['source_path']); labels = table(result, 'label-origins.csv')
    assert [r['target'] for r in samples if r['series'] == 'B'] == ['平稳'] * 3
    assert all(float(r['lag_0']) == 14 for r in samples if r['series'] == 'A')
    assert labels[0]['label_available_time'].startswith('2025-08-01')
    assert labels[1]['label_available_time'].startswith('2025-08-01')
    assert labels[0]['window_start'] == '2025-03'


def test_missing_period_does_not_shift_future_window_or_fill_zero(scenario):
    Path(scenario['source_path']).write_text('period,value\n2025-01,10\n2025-02,14\n2025-04,13\n2025-05,10\n')
    result = code.main(scenario)
    assert [r['interval'] for r in rows(result['source_path'])] == ['3']
    assert result['excluded'] == 2
    assert '2025-03' in table(result, 'excluded.csv')[0]['reason']
    assert result['prediction_rows'] == 3


def test_unknown_future_produces_only_inputs_and_late_history_is_excluded(scenario):
    Path(scenario['source_path']).write_text('period,value,available\n2025-01,10,2025-02-01\n2025-02,14,2025-03-02\n2025-03,16,2025-04-01\n')
    result = code.main({**scenario, 'available_column': 'available', 'first_origin': '2025-02', 'last_origin': '2025-03'})
    assert result['rows'] == 0 and result['prediction_rows'] == 3
    assert result['excluded'] == 4
    assert '尚不可用' in table(result, 'excluded.csv')[0]['reason']
    assert all(r['origin_time'].startswith('2025-04-01') for r in rows(result['prediction_path']))


def test_stride_prediction_uses_last_selected_origin_and_quarters(scenario):
    Path(scenario['source_path']).write_text('period,value\n2024Q1,1\n2024Q2,2\n2024Q3,3\n2024Q4,4\n2025Q1,5\n')
    result = code.main({**scenario, 'frequency': '季', 'first_origin': '', 'last_origin': '', 'origin_stride': 2})
    assert all(r['origin_time'].startswith('2025-01-01') for r in rows(result['prediction_path']))
    assert table(result, 'label-origins.csv')[0]['window_start'] == '2024Q3'


def test_daily_leap_day_and_changed_future_preserve_historical_features(scenario):
    p = Path(scenario['source_path'])
    p.write_text('period,value\n2024-02-27,1\n2024-02-28,3\n2024-02-29,4\n2024-03-01,5\n2024-03-02,6\n')
    inputs = {**scenario, 'frequency': '日', 'first_origin': '2024-02-28', 'last_origin': '2024-02-28'}
    first = code.main(inputs)
    original_inputs = Path(first['prediction_path']).read_bytes()
    assert table(first, 'label-origins.csv')[0]['window_start'] == '2024-02-29'
    p.write_text(p.read_text().replace('2024-02-29,4', '2024-02-29,400'))
    changed = code.main(inputs)
    assert Path(changed['prediction_path']).read_bytes() == original_inputs
    assert table(first, 'label-origins.csv')[0]['future_mean'] != table(changed, 'label-origins.csv')[0]['future_mean']


@pytest.mark.parametrize('changes, expected', [
    ({'value_column': 'missing'}, '缺少'), ({'threshold': -1}, '阈值'),
    ({'window_periods': 0}, '区间的周期数'), ({'windows': 1.2}, '区间数量'),
    ({'first_origin': '2026-01', 'last_origin': '2025-01'}, '首个起点'),
    ({'source_path': '../outside.csv'}, '本项目'),
])
def test_bad_configuration_fails_without_results(scenario, changes, expected):
    with pytest.raises(ValueError, match=expected):
        code.main({**scenario, **changes})
    assert not Path('results').exists()


def test_duplicate_and_early_availability_report_record_then_repair(scenario):
    p = Path(scenario['source_path']); original = p.read_text()
    p.write_text(original + '2025-02,999\n')
    with pytest.raises(ValueError, match='重复'):
        code.main(scenario)
    p.write_text(original); first = code.main(scenario)
    before = Path(first['source_path']).read_bytes()
    changed = code.main({**scenario, 'threshold': 3})
    assert [r['target'] for r in rows(changed['source_path'])] == ['平稳'] * 3
    p.write_text('period,value,available\n2025-01,10,2025-01-20\n')
    with pytest.raises(ValueError, match='周期结束前'):
        code.main({**scenario, 'available_column': 'available'})
    assert Path(first['source_path']).read_bytes() == before
    snapshot = next(a['file_path'] for a in first['artifacts'] if a['label'] == '原输入快照')
    assert Path(snapshot).read_text() == original


def test_xlsx_sheets_and_empty_invalid_values(scenario):
    from openpyxl import Workbook
    p = Path('requirement-package/history.xlsx')
    book = Workbook(); sheet = book.active; sheet.title = 'history'
    sheet.append(['period', 'value'])
    for row in rows(scenario['source_path']):
        sheet.append([row['period'], float(row['value'])])
    book.create_sheet('other').append(['x']); book.save(p); book.close()
    with pytest.raises(ValueError, match='工作表'):
        code.main({**scenario, 'source_path': str(p)})
    assert code.main({**scenario, 'source_path': str(p), 'sheet': 'history'})['rows'] == 3
    Path(scenario['source_path']).write_text('period,value\n2025-01,10\n2025-02,14\n2025-03,invalid\n2025-04,13\n2025-05,10\n')
    assert code.main(scenario)['rows'] == 1


def test_employee_example_runs_edits_and_downloads_without_model(configured):
    client, app, _, _ = configured
    pid = install(client, 'interval-trends'); base = '/api/v1/projects/' + pid
    guide = client.get(base + '/example').json(); flow = guide['workflows'][0]['id']
    task = settled(client, base, start(client, base, 'first', workflow_id=flow))
    assert task['status'] == 'succeeded', task['error']
    result = task['outputs']['result']
    assert result['rows'] == 90 and result['prediction_rows'] == 3
    old_outputs = task['outputs']
    for artifact in result['artifacts']:
        response = client.get('/api/v1/applications/' + pid + '/workspace/files/' + artifact['file_path'] + '?download=1')
        assert response.status_code == 200 and response.content
    changed = settled(client, base, start(client, base, 'changed', workflow_id=flow, inputs={'threshold': 1}))
    assert changed['status'] == 'succeeded', changed['error']
    assert changed['outputs']['result']['source_path'] != result['source_path']
    assert changed['outputs']['result']['class_counts']['平稳'] > result['class_counts']['平稳']
    assert client.get(base + '/tasks/' + task['id']).json()['outputs'] == old_outputs
    assert client.get(base + '/skills/example-guide').status_code == 200
    assert not app.state.services.local_agents.tasks
