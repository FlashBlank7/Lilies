import csv
import json
from pathlib import Path

import pytest
from agent_platform import candidate_comparison as template, candidate_comparison_code as code
from tests.test_projects import configured,start,settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,content in template.example_files().items():Path('requirement-package',name).write_text(content,encoding='utf-8')
    return {**{f['name']:f['default'] for f in template.workflow()['nodes'][0]['config']['inputs']},
            **template.example_defaults(),'source_path':'requirement-package/candidates.csv'}


def run(inputs):return code.main({'operation':'compare','prepared':code.main({'operation':'prepare',**inputs})})


def rows(result):
    with Path(result['source_path']).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def test_hard_conditions_ties_and_no_fallback(scenario):
    result=run(scenario);byid={r['candidate_id']:r for r in rows(result)}
    assert result['counts']=={'pass':3,'fail':2,'unknown':1}
    assert result['groups'][0]['top_candidates']==['C','D']
    assert byid['A']['comparison_rank']=='3'
    assert byid['B']['comparison_rank']=='' and byid['E']['comparison_rank']==''
    assert 'used 不大于 available' in byid['B']['comparison_reason']
    assert '缺少' in byid['E']['comparison_reason']
    assert result['groups'][1]['top_candidates']==[] and result['groups'][1]['status']=='no_candidate_passes'
    assert '没有可确认满足全部' in result['markdown']
    assert 'output（越大越好）' in result['markdown'] and 'waste（越小越好）' in result['markdown']


def test_new_inputs_constraints_and_snapshot_isolation(scenario):
    original=run(scenario);before=Path(original['source_path']).read_bytes()
    updated=run({**scenario,'source_path':'requirement-package/candidates-2.csv'})
    assert [g['top_candidates'] for g in updated['groups']]==[['B'],['F']]
    stricter=run({**scenario,'constraints':scenario['constraints']+[dict(field='waste',operator='不大于',value='6')]})
    assert stricter['counts']['pass']==2
    assert Path(original['source_path']).read_bytes()==before
    prepared=code.prepare(scenario);Path(scenario['source_path']).write_text('changed')
    assert code.compare({'prepared':prepared})['groups'][0]['top_candidates']==['C','D']
    Path(prepared['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.compare({'prepared':prepared})


def test_weighted_scores_use_fixed_scale_and_never_overrule_conditions(scenario):
    inputs={**scenario,'mode':'按明确权重评分'}
    first=run(inputs);byid={r['candidate_id']:r for r in rows(first)}
    assert byid['A']['comparison_score']=='-6' and byid['C']['comparison_score']=='-1'
    assert byid['B']['comparison_score']=='9' and not byid['B']['comparison_rank']
    assert first['groups'][0]['top_candidates']==['C','D']
    p=Path(scenario['source_path']);p.write_text(p.read_text()+'X,任务一,1,100,可配,1,99\n')
    after={r['candidate_id']:r for r in rows(run(inputs))}
    assert all(after[k]['comparison_score']==v['comparison_score'] for k,v in byid.items())
    Path(scenario['source_path']).write_text('candidate_id,case_id,x,y\nA,one,0.1,0.2\nB,one,0.3,0\n')
    tied=run({**inputs,'constraints':[],'objectives':[dict(field=f,direction='越大越好',weight=1,scale=1) for f in ['x','y']]})
    assert tied['groups'][0]['top_candidates']==['A','B']
    assert {r['comparison_score'] for r in rows(tied)}=={'0.3'}


def test_no_criteria_and_missing_objectives_do_not_invent_recommendations(scenario):
    unchecked=run({**scenario,'constraints':[]})
    assert unchecked['counts']=={'unchecked':6} and '未检查限制' in unchecked['markdown']
    no_targets=run({**scenario,'objectives':[]})
    assert no_targets['groups'][0]['status']=='no_objectives' and not no_targets['groups'][0]['top_candidates']
    Path(scenario['source_path']).write_text('candidate_id,case_id,used,available,pairable,output,waste\nA,g,1,2,可配,,3\nB,g,1,2,可配,bad,3\n')
    missing=run(scenario)
    assert missing['counts']=={'pass':2} and missing['groups'][0]['status']=='missing_objective_values'
    assert all(not r['comparison_rank'] and r['comparison_objective_issue'] for r in rows(missing))


@pytest.mark.parametrize('op,left,right,expected',[
    ('不大于','0','0','pass'),('小于','0','0','fail'),('不小于','0','0','pass'),('大于','0','0','fail'),
    ('数值等于','1.00','1','pass'),('数值不等于','1.00','1','fail'),('文字等于','1.00','1','fail'),('文字不等于','1.00','1','pass'),
    ('不大于','NaN','1','unknown'),('大于','','1','unknown'),('文字不等于','','x','unknown')])
def test_exact_comparisons(op,left,right,expected):
    assert code.check({'v':left},dict(field='v',operator=op,value=right,other_field=''))[0]==expected


@pytest.mark.parametrize('patch,message',[
    ({'id_column':'missing'},'标识列'),({'constraints':[dict(field='missing',operator='不大于',value='1')]},'字段不存在'),
    ({'constraints':[dict(field='used',operator='不大于',value='1',other_field='available')]},'恰好填写一个'),
    ({'constraints':[dict(field='used',operator='不大于')]},'恰好填写一个'),
    ({'mode':'按明确权重评分','objectives':[dict(field='waste',direction='越小越好',weight=1,scale=0)]},'必须大于0'),
    ({'objectives':[dict(field='waste',direction='未知')]},'请选择越大或越小'),
    ({'source_path':'../private.csv'},'当前项目')])
def test_actionable_configuration_errors(scenario,patch,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**patch})


def test_identifiers_excel_and_same_name_new_bytes(scenario):
    from openpyxl import Workbook
    p=Path(scenario['source_path']);original=p.read_text();p.write_text(original+'A,任务一,1,100,可配,1,1\n')
    with pytest.raises(ValueError,match='同组内重复'):run(scenario)
    # Same candidate name is valid in a different problem group.
    p.write_text(original+'A,任务三,1,100,可配,1,1\n');assert run(scenario)['rows']==7
    p.write_text(original.replace('A,任务一',',任务一',1))
    with pytest.raises(ValueError,match='标识或分组为空'):run(scenario)
    book=Workbook();ws=book.active;ws.title='候选'
    for row in csv.reader(original.splitlines()):ws.append(row)
    book.create_sheet('说明');book.save('requirement-package/candidates.xlsx')
    inputs={**scenario,'source_path':'requirement-package/candidates.xlsx'}
    with pytest.raises(ValueError,match='多张表'):run(inputs)
    assert run({**inputs,'sheet':'候选'})['groups'][0]['top_candidates']==['C','D']


def test_platform_editable_tables_run_repair_download_no_model(configured):
    client,app,_,settings=configured;pid=install(client,'candidate-comparison');base='/api/v1/projects/'+pid
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()['snapshot']['workflow']
    config=next(f for f in draft['nodes'][0]['config']['inputs'] if f['name']=='constraints')
    assert config['columns'][1]['options'][0]=='不大于'
    first=settled(client,base,start(client,base,'first',workflow_id=pid,inputs={}))
    assert first['status']=='succeeded',first.get('error')
    assert first['outputs']['result']['groups'][0]['top_candidates']==['C','D']
    for file in first['outputs']['result']['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+file['file_path']).status_code==200
    bad=settled(client,base,start(client,base,'bad',workflow_id=pid,inputs={'objectives':[dict(field='unknown',direction='越大越好')]}))
    assert bad['status']=='failed' and '字段不存在' in bad['error']
    fixed=settled(client,base,start(client,base,'fixed',workflow_id=pid,inputs={'objectives':[]}))
    assert fixed['status']=='succeeded' and fixed['outputs']['result']['groups'][0]['status']=='no_objectives'
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
    assert client.get(base+'/modeling/studies').json()==[] and not app.state.services.local_agents.tasks
