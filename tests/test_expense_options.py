import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from agent_platform import example_processing as processing
from tests.test_example_projects import install, sample_files  # noqa: F401
from tests.test_projects import configured, settled, start  # noqa: F401


@pytest.mark.parametrize('group_by,field', [('按月、类别和币种', 'category'), ('按月、商户和币种', 'merchant')])
@pytest.mark.parametrize('mark_duplicates', [True, False])
def test_expense_options_preserve_currency_refunds_and_all_rows(sample_files, group_by, field, mark_duplicates):
    path = sample_files('expenses.csv', 'date,category,amount,currency,merchant\n'
        '2026-09-01,餐饮,10,CNY,A\n2026-09-01,餐饮,10,CNY,A\n'
        '2026-09-02,餐饮,5,CNY,B\n2026-09-03,资料,-2,CNY,A\n'
        '2026-09-01,餐饮,10,USD,A\n')
    original = Path(path).read_bytes()
    result = processing.main({'operation': 'expenses', 'source_path': path,
        'group_by': group_by, 'mark_duplicates': mark_duplicates})
    assert result['rows'] == 5
    assert len(result['summary']) == 3
    assert all(field in row for row in result['summary'])
    cny_groups = {row[field]: Decimal(row['amount']) for row in result['summary'] if row['currency'] == 'CNY'}
    assert cny_groups == ({'餐饮': Decimal(25), '资料': Decimal(-2)} if field == 'category'
        else {'A': Decimal(18), 'B': Decimal(5)})
    for currency, expected in [('CNY', Decimal(23)), ('USD', Decimal(10))]:
        assert sum(Decimal(row['amount']) for row in result['summary'] if row['currency'] == currency) == expected
    assert result['suspected_duplicates'] == (1 if mark_duplicates else None)
    assert len(result['duplicate_records']) == (1 if mark_duplicates else 0)
    assert group_by in result['markdown']
    if not mark_duplicates:
        assert '未检查重复' in result['markdown']
        assert {row['suspected_duplicate'] for row in result['preview']} == {'not_checked'}
    folder = Path(result['artifacts'][0]['file_path']).parent
    assert json.loads((folder / 'result.json').read_text())['group_by'] == group_by
    with (folder / 'details.csv').open(encoding='utf-8-sig') as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 5
    assert exported[3]['amount'] == '-2'
    assert [Decimal(row['amount']) for row in exported] == [Decimal(v) for v in ['10','10','5','-2','10']]
    assert Path(path).read_bytes() == original


@pytest.mark.parametrize('value,numeric', [
    ('-5', True), ('-0.00', True), ('+12.30', True), ('-.5', True), ('+1.', True),
    ('-1.2500e+3', True), ('+2E-4', True), ('-999999999999999999999999.000001', True),
    (' -5.00 ', True), ('-1e10000', True),
    ('=SUM(A1:A2)', False), ('+SUM(A1:A2)', False), ('-SUM(A1:A2)', False), ('@SUM(A1:A2)', False),
    ('-5+1', False), ('+1e', False), ('-1_000', False), ('--5', False), ('-1,000', False),
    ('-NaN', False), ('+Infinity', False), ('-sNaN', False), ('-退款', False),
    ('-1e999999999999999999999999', False), ('\t=1+1', False), (' \r-1+2', False),
])
def test_csv_export_preserves_finite_numeric_text_and_protects_formulas(tmp_path, monkeypatch, value, numeric):
    monkeypatch.chdir(tmp_path)
    rows = [{'value': value}]
    result = processing.export({'markdown': '# 导出', 'preview': rows}, rows)
    folder = Path(result['artifacts'][0]['file_path']).parent
    with (folder / 'details.csv').open(encoding='utf-8-sig', newline='') as stream:
        actual = next(csv.DictReader(stream))['value']
    assert actual == (value if numeric else "'" + value)
    if numeric:
        assert Decimal(actual).is_finite()
        assert Decimal(actual) == Decimal(value)
    assert rows == [{'value': value}]
    assert json.loads((folder / 'result.json').read_text())['preview'] == rows


def test_expense_form_choices_reach_saved_workflow_and_code(configured):
    client, _, _, settings = configured
    pid = install(client, 'expenses')
    base = '/api/v1/projects/' + pid
    draft = client.get('/api/v1/applications/' + pid + '/draft').json()
    fields = {f['name']: f for f in draft['snapshot']['workflow']['nodes'][0]['config']['inputs']}
    assert fields['group_by']['options'] == ['按月、类别和币种', '按月、商户和币种']
    assert fields['mark_duplicates']['type'] == 'boolean'
    default = settled(client, base, start(client, base, 'defaults', workflow_id=pid))
    assert default['status'] == 'succeeded', default
    assert default['outputs']['result']['suspected_duplicates'] == 1
    assert all('category' in row for row in default['outputs']['result']['summary'])
    changed = settled(client, base, start(client, base, 'changed-options', workflow_id=pid,
        inputs={'group_by': '按月、商户和币种', 'mark_duplicates': False}))
    assert changed['status'] == 'succeeded', changed
    result = changed['outputs']['result']
    assert all('merchant' in row for row in result['summary'])
    assert result['mark_duplicates'] is False and result['suspected_duplicates'] is None
    assert '未检查重复' in changed['outputs']['markdown']
    assert '按月、商户和币种' in (settings.workspace_root / pid / result['artifacts'][0]['file_path']).read_text()
    assert client.get(base + '/tasks/' + default['id']).json()['outputs'] == default['outputs']


def test_old_expense_inputs_keep_original_default_behavior(sample_files):
    path = sample_files('old.csv', 'date,category,amount,currency,merchant\n'
        '2026-09-01,交通,12.3,CNY,A\n2026-09-01,交通,12.3,CNY,A\n')
    old = processing.main({'operation': 'expenses', 'source_path': path})
    explicit = processing.main({'operation': 'expenses', 'source_path': path,
        'group_by': '按月、类别和币种', 'mark_duplicates': True})
    assert old['summary'] == explicit['summary'] == [{'month': '2026-09', 'category': '交通', 'currency': 'CNY', 'amount': '24.6'}]
    assert old['suspected_duplicates'] == explicit['suspected_duplicates'] == 1


@pytest.mark.parametrize('options', [{'group_by': '合并所有币种'}, {'mark_duplicates': 'false'}])
def test_expense_options_reject_unsupported_values(sample_files, options):
    path = sample_files('expenses.csv', 'date,category,amount,currency,merchant\n2026-09-01,交通,12.3,CNY,A\n')
    with pytest.raises(ValueError):
        processing.main({'operation': 'expenses', 'source_path': path, **options})
