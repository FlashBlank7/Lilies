import csv
import json
import math
from pathlib import Path

import pytest
from agent_platform import prediction_feedback as template, prediction_feedback_code as code
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root=Path('requirement-package');root.mkdir()
    for name,content in template.example_files().items():(root/name).write_text(content,encoding='utf-8')
    inputs={f['name']:f.get('default') for f in template.workflow()['nodes'][0]['config']['inputs']}
    return {**inputs,'source_path':'requirement-package/predictions.csv','actual_path':'requirement-package/measurements.csv',
            'group_columns':'batch','baseline_column':'baseline'}


def run(inputs):
    return code.main({'operation':'evaluate','prepared':code.main({'operation':'prepare',**inputs})})


def read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def test_join_coverage_zero_actual_groups_and_baseline(scenario):
    result=run({**scenario,'absolute_tolerance':'2'})
    m=result['metrics']
    assert result['counts']=={'evaluated':3,'no_actual_record':1,'invalid_values':1}
    assert m['n']==3 and result['coverage']==.6 and result['unmatched_actual_rows']==1
    assert m['mae']==pytest.approx(7/3) and m['rmse']==3 and m['bias']==pytest.approx(7/3)
    assert m['mape_n']==2 and m['mape']==pytest.approx((1/9+1/11)/2*100)
    assert m['within_tolerance']==pytest.approx(2/3)
    assert result['baseline_comparison']['n']==3 and result['baseline_comparison']['baseline']['mae']==2
    groups={g['batch']:g for g in result['groups']}
    assert groups['A']['eval_n']==2 and groups['A']['eval_mae']==1
    assert groups['B']['eval_rows']==3 and groups['B']['eval_n']==1 and groups['B']['eval_mae']==5
    rows=read(result['source_path'])
    assert [r['sample_id'] for r in rows]==['s1','s2','s3','s4','s5']
    assert rows[2]['eval_actual']=='0' and rows[2]['eval_actual_record']=='2'
    assert rows[3]['eval_error']=='' and '没有匹配' in rows[3]['eval_reason']
    assert '预测结果缺失' in rows[4]['eval_reason']
    assert '不判断是否合格' in run(scenario)['markdown']


def test_new_actuals_new_threshold_and_snapshot_isolation(scenario):
    first=run(scenario); original=Path(first['source_path']).read_bytes()
    second=run({**scenario,'actual_path':'requirement-package/measurements-2.csv','absolute_tolerance':'0'})
    assert second['metrics']['n']==4 and second['metrics']['mae']==0 and second['metrics']['within_tolerance']==1
    assert first['source_path']!=second['source_path'] and Path(first['source_path']).read_bytes()==original
    snapshot=code.prepare(scenario)
    Path(scenario['actual_path']).write_text('sample_id,actual\ns1,1000\n')
    assert code.evaluate({'prepared':snapshot})['metrics']['mae']==pytest.approx(7/3)
    assert run(scenario)['metrics']['mae']==990
    Path(snapshot['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.evaluate({'prepared':snapshot})


def test_classification_unknown_and_missing_labels(scenario):
    result=run({**scenario,'source_path':'requirement-package/review-labels.csv','actual_path':'','mode':'类别判断'})
    m=result['metrics']
    assert m['n']==4 and m['accuracy']==.5 and m['macro_f1']==pytest.approx((.5+2/3+0)/3)
    classes={c['label']:c for c in m['classes']}
    assert classes['未知']==dict(label='未知',support=0,predicted=1,precision=0,recall=0,f1=0)
    assert classes['复核']['support']==2 and classes['复核']['recall']==.5
    assert result['counts']=={'evaluated':4,'invalid_values':1}
    assert result['baseline_comparison']['baseline']['accuracy']==.5
    assert result['baseline_comparison']['baseline']['macro_f1']==pytest.approx(2/9)
    assert [c['label'] for c in result['baseline_comparison']['baseline']['classes']]==[c['label'] for c in m['classes']]
    assert result['groups'][0]['eval_macro_f1']==pytest.approx(2/9)
    assert len([a for a in result['artifacts'] if a['file_path'].endswith('.csv')])==5
    Path(scenario['source_path']).write_text('sample_id,prediction,actual\nx,1,1.0\ny,1, 1 \n')
    exact=run({**scenario,'actual_path':'','mode':'类别判断','baseline_column':'','group_columns':''})
    assert exact['metrics']['accuracy']==.5


def test_baseline_only_compares_same_subset(scenario):
    Path(scenario['source_path']).write_text('sample_id,batch,prediction,baseline\ns1,A,10,\ns2,A,12,10\ns3,B,5,NaN\n')
    result=run(scenario)
    assert result['metrics']['mae']==pytest.approx(7/3)
    assert result['baseline_comparison']['n']==1
    assert result['baseline_comparison']['prediction']['mae']==result['baseline_comparison']['baseline']['mae']==1


def test_empty_evaluation_and_constant_actual_are_not_perfect(scenario):
    Path(scenario['actual_path']).write_text('sample_id,actual\nother,0\n')
    result=run(scenario)
    assert result['metrics']['n']==0 and result['metrics']['mae'] is None and result['metrics']['r2'] is None
    assert result['counts']=={'no_actual_record':5}
    assert '0 条具备有效预测与实测' in result['markdown']
    Path(scenario['actual_path']).write_text('sample_id,actual\ns1,0\ns2,0\n')
    constant=run(scenario)['metrics']
    assert constant['r2'] is None and constant['mape'] is None and constant['mape_n']==0
    assert constant['mae']==11


@pytest.mark.parametrize('change,message',[
    ({'prediction_column':'missing'},'预测表缺少字段'),({'actual_column':'missing'},'实测表缺少字段'),
    ({'key_columns':''},'请指定样本标识'),({'key_columns':'sample_id,sample_id'},'列重复'),
    ({'absolute_tolerance':'-1'},'容差应为'),({'absolute_tolerance':'inf'},'容差应为'),
    ({'mode':'类别判断','absolute_tolerance':'2'},'清空容差'),({'mode':'wrong'},'评价方式'),
    ({'source_path':'../private.csv'},'当前项目')])
def test_configuration_errors(scenario,change,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**change})


def test_duplicates_empty_keys_and_output_collisions(scenario):
    path=Path(scenario['actual_path']); original=path.read_text()
    path.write_text(original+'s1,20\n')
    with pytest.raises(ValueError,match='实测表.*标识重复'):run(scenario)
    path.write_text(original+',20\n')
    with pytest.raises(ValueError,match='实测表.*标识为空'):run(scenario)
    path.write_text(original); predictions=Path(scenario['source_path'])
    predictions.write_text(predictions.read_text()+'s1,A,10,8\n')
    with pytest.raises(ValueError,match='预测表.*标识重复'):run(scenario)
    predictions.write_text('sample_id,prediction,actual,eval_error\na,1,1,0\n')
    with pytest.raises(ValueError,match='已有eval_'):run({**scenario,'actual_path':'','group_columns':'','baseline_column':''})


def test_excel_composite_keys_and_multiple_sheets(scenario):
    from openpyxl import Workbook
    book=Workbook();ws=book.active;ws.title='feedback'
    for row in [('id','version','prediction','actual'),('same',1,0,0),('same',2,3,1)]:ws.append(row)
    book.create_sheet('notes');book.save('requirement-package/input.xlsx')
    inputs={**scenario,'source_path':'requirement-package/input.xlsx','actual_path':'','key_columns':'id,version',
            'baseline_column':'','group_columns':'','absolute_tolerance':'0'}
    with pytest.raises(ValueError,match='多张表'):run(inputs)
    result=run({**inputs,'prediction_sheet':'feedback'})
    assert result['metrics']['n']==2 and result['metrics']['mae']==1 and result['metrics']['within_tolerance']==.5
    assert len(read(result['source_path']))==2


def test_fixed_metric_reference_and_numerical_limits():
    # Standard four-observation regression example with independently calculated values.
    pairs=[(2.5,3),(0,-.5),(2,2),(8,7)]
    m=code.metrics(pairs,'数值预测',None)
    assert m['mae']==.5 and m['rmse']==pytest.approx(math.sqrt(.375))
    assert m['r2']==pytest.approx(1-1.5/29.1875)
    m=code.metrics([(0,0),(1,0),(2,1),(2,2)],'类别判断',None)
    assert m['accuracy']==.5 and m['macro_f1']==pytest.approx(4/9)
    # Near-zero actuals do not turn a serializable report into Infinity/NaN.
    extreme=code.metrics([(1e150,1e-320)],'数值预测',None)
    assert extreme['mape'] is None
    json.dumps(extreme,allow_nan=False)


def test_platform_install_run_download_failure_repair_and_no_training(configured):
    client,app,_,settings=configured
    pid=install(client,'prediction-feedback');base='/api/v1/projects/'+pid
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()['snapshot']['workflow']
    assert next(f for f in draft['nodes'][0]['config']['inputs'] if f['name']=='mode')['options']==['数值预测','类别判断']
    task=settled(client,base,start(client,base,'first',workflow_id=pid,inputs={}))
    assert task['status']=='succeeded',task.get('error')
    result=task['outputs']['result']
    assert result['metrics']['mae']==pytest.approx(7/3)
    for artifact in result['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path']).status_code==200
    failed=settled(client,base,start(client,base,'missing',workflow_id=pid,inputs={'key_columns':'unknown'}))
    assert failed['status']=='failed' and 'unknown' in failed['error']
    repaired=settled(client,base,start(client,base,'fixed',workflow_id=pid,inputs={'absolute_tolerance':'2'}))
    assert repaired['status']=='succeeded' and repaired['outputs']['result']['metrics']['within_tolerance']==pytest.approx(2/3)
    assert client.get(base+'/tasks/'+task['id']).json()['outputs']==task['outputs']
    assert client.get(base+'/modeling/studies').json()==[]
    assert not app.state.services.local_agents.tasks
    skill=client.get(base+'/skills/example-guide').json()
    assert '不重新预测' in json.dumps(skill,ensure_ascii=False) or '不重训' in json.dumps(skill,ensure_ascii=False)
