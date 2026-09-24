import csv
from pathlib import Path

import pytest
from agent_platform import candidate_comparison_code as candidates
from agent_platform import parameter_intervals as template, parameter_intervals_code as code
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,content in template.example_files().items():Path('requirement-package',name).write_text(content,encoding='utf-8')
    return {**{f['name']:f['default'] for f in template.workflow()['nodes'][0]['config']['inputs']},
            **template.example_defaults(),'source_path':'requirement-package/scan.csv'}


def check(inputs):
    return candidates.compare({'prepared':candidates.prepare({**inputs,'id_column':inputs['parameter_column'],'mode':'按优先级逐项比较',
        'objectives':[dict(field=inputs['score_column'],direction=inputs['direction'])]})})


def run(inputs):return code.main({**inputs,'checked':check(inputs)})


def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def qualified(result):return [(r['group'],r['start'],r['end']) for r in rows(result['intervals_path']) if r['qualified']=='True']


def test_groups_failed_middle_best_isolated_alternatives_and_no_eligible(scenario):
    original=Path(scenario['source_path']).read_bytes();result=run(scenario)
    assert qualified(result)==[('A','1','3'),('A','5','6'),('B','3','4')]
    assert [g['status'] for g in result['groups']]==['intervals_found','best_point_has_no_interval','no_eligible_points']
    assert result['groups'][0]['best_parameters']==['2','6']
    assert result['groups'][1]['alternatives']==1 and result['groups'][1]['best_intervals']==0
    p=rows(result['source_path'])
    assert p[3]['interval_condition_status']=='fail' and p[3]['interval_comparison_score']==''
    assert '前一点' in p[4]['interval_break_before'] and '最大相邻' in p[7]['interval_break_before']
    assert '最佳点未形成' in result['markdown'] and '原分数' in result['markdown']
    assert Path(scenario['source_path']).read_bytes()==original


def test_new_file_width_and_source_snapshot_keep_history(scenario):
    first=run(scenario);old=Path(first['source_path']).read_bytes()
    strict=run({**scenario,'min_width':2})
    assert qualified(strict)==[('A','1','3')]
    updated=run({**scenario,'source_path':'requirement-package/scan-2.csv'})
    assert qualified(updated)==[('A','1','6'),('B','1','2')]
    assert updated['source']['sha256']!=first['source']['sha256']
    checked=check(scenario);Path(scenario['source_path']).write_text('changed')
    assert qualified(code.main({**scenario,'checked':checked}))==qualified(first)
    assert Path(first['source_path']).read_bytes()==old


def test_small_step_does_not_bridge_known_failure_unknown_or_bad_score(scenario):
    Path(scenario['source_path']).write_text('sample,setting,score,check\nA,0,1,通过\nA,0.001,1,不通过\nA,0.002,1,通过\nA,0.003,1,通过\nA,0.004,1,\nA,0.005,1,通过\nA,0.006,bad,通过\nA,0.007,1,通过\n')
    result=run({**scenario,'max_gap':'0.006','min_width':0})
    assert qualified(result)==[('A','0.002','0.003')]
    assert result['groups'][0]['eligible']==5
    assert rows(result['source_path'])[6]['interval_comparison_score']==''


def test_smoothing_stays_within_contiguous_eligible_runs(scenario):
    Path(scenario['source_path']).write_text('sample,setting,score,check\nA,1,1,通过\nA,2,3,通过\nA,3,1000,不通过\nA,4,7,通过\nA,6,500,通过\n')
    result=run({**scenario,'smoothing':'相邻3点均值','tolerance':1000})
    assert [r['interval_comparison_score'] for r in rows(result['source_path'])]==['2','2','','7','500']
    assert qualified(result)==[('A','1','2')]


def test_low_scores_precision_and_tolerance_boundary(scenario):
    Path(scenario['source_path']).write_text('sample,setting,score,check\nA,0.00001,0.1,通过\nA,0.00002,0.3,通过\nA,0.00003,0.3000000000000000000000000001,通过\n')
    result=run({**scenario,'direction':'越小越好','max_gap':'0.00001','min_width':'0.00001','tolerance':'0.2'})
    assert qualified(result)==[('A','0.00001','0.00002')]
    assert [r['interval_near_best'] for r in rows(result['source_path'])]==['True','True','False']
    assert result['groups'][0]['best_parameters']==['0.00001']


def test_maximum_width_windows_are_observed_maximal_and_ties_preserved(scenario):
    Path(scenario['source_path']).write_text('sample,setting,score,check\n'+''.join(f'A,{x},1,通过\n' for x in ['0','0.3','0.6','0.9']))
    result=run({**scenario,'max_gap':'0.3','max_width':'0.65','min_width':'0.5'})
    assert qualified(result)==[('A','0','0.6'),('A','0.3','0.9')]
    assert result['groups'][0]['best_parameters']==['0','0.3','0.6','0.9']
    assert result['groups'][0]['best_intervals']==2


def test_single_point_missing_conditions_and_minimum_count_not_hidden(scenario):
    Path(scenario['source_path']).write_text('sample,setting,score,check\nA,1,1,通过\nA,2,0.5,通过\n')
    result=run({**scenario,'min_width':0,'constraints':[]})
    assert qualified(result)==[] and result['groups'][0]['status']=='no_interval_meets_requirements'
    assert '未检查限制' in result['markdown'] and '采样点数不足' in result['markdown']
    result=run({**scenario,'tolerance':1,'min_points':3})
    assert qualified(result)==[]


@pytest.mark.parametrize('patch,message',[
    ({'max_gap':0},'间隔必须大于0'),({'tolerance':-1},'不能为负'),({'min_width':-1},'不能为负'),
    ({'max_width':0},'最大宽度'),({'max_width':0.5},'最大宽度'),({'min_points':1},'最少点数'),
    ({'min_points':2.5},'整数'),({'smoothing':'other'},'计算方式'),({'origin':'猜测'},'数值来源'),
    ({'max_gap':'NaN'},'有限数值'),({'score_column':'missing'},'字段不存在'),({'source_path':'../x.csv'},'当前项目')])
def test_configuration_errors_are_specific(scenario,patch,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**patch})


def test_duplicate_numeric_axis_and_invalid_parameter_rejected(scenario):
    Path(scenario['source_path']).write_text('sample,setting,score,check\nA,1,1,通过\nA,1.0,2,通过\n')
    with pytest.raises(ValueError,match='参数值重复'):run(scenario)
    Path(scenario['source_path']).write_text('sample,setting,score,check\nA,bad,1,通过\n')
    with pytest.raises(ValueError,match='第2条记录的参数'):run(scenario)


def test_excel_and_bounded_preview_all_results_download(scenario):
    from openpyxl import Workbook
    book=Workbook();sheet=book.active;sheet.append(['sample','setting','score','check'])
    for x in range(40):sheet.append(['A',x,1,'通过'])
    path='requirement-package/scan.xlsx';book.save(path)
    result=run({**scenario,'source_path':path,'max_width':1})
    assert len(result['interval_preview'])==20 and len(rows(result['intervals_path']))==39
    assert len(rows(result['source_path']))==40 and len(result['preview'])==12


def test_employee_project_runs_fixes_and_downloads_without_models(configured):
    client,app,_,settings=configured;pid=install(client,'parameter-intervals');base='/api/v1/projects/'+pid
    first=settled(client,base,start(client,base,'first',workflow_id=pid,inputs={}))
    assert first['status']=='succeeded',first.get('error')
    assert [g['qualified_intervals'] for g in first['outputs']['result']['groups']]==[2,1,0]
    for f in first['outputs']['result']['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+f['file_path']).status_code==200
    bad=settled(client,base,start(client,base,'bad',workflow_id=pid,inputs={'max_gap':0}))
    assert bad['status']=='failed' and '最大相邻间隔' in bad['error']
    fixed=settled(client,base,start(client,base,'fixed',workflow_id=pid,inputs={'max_gap':1,'min_width':2}))
    assert fixed['status']=='succeeded' and [g['qualified_intervals'] for g in fixed['outputs']['result']['groups']]==[1,0,0]
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
    assert client.get(base+'/modeling/studies').json()==[] and not app.state.services.local_agents.tasks
