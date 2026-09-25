import csv
import itertools
from pathlib import Path

import pytest
from agent_platform import local_feedback as recipe, local_feedback_code as code
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); Path('requirement-package').mkdir()
    for name, value in recipe.example_files().items():
        Path('requirement-package',name).write_text(value,encoding='utf-8')
    return {**{f['name']:f['default'] for f in recipe.workflow()['nodes'][0]['config']['inputs']},
            **{k:v.replace('@file:','requirement-package/') if isinstance(v,str) else v for k,v in recipe.example_defaults().items()}}


def run(inputs):
    return code.evaluate({'prepared':code.prepare(inputs)})


def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def test_known_update_order_and_scope(scenario):
    first=run(scenario);center=rows(first['source_path'])[4]
    assert float(center['adjusted_score'])==pytest.approx(.82)
    assert first['before']['cuts']==[] and first['before']['kept_length']==90
    assert first['after']['cuts']==[20,70] and first['after']['kept_length']==40
    assert first['after']['cost']<=first['previous_plan_cost_on_adjusted_scores']
    assert first['changed_points']==5
    assert '不是贝叶斯' in first['markdown'] and '不含端部' in first['markdown']
    second=run({**scenario,'observations_path':'requirement-package/补充观测.csv'})
    third=run({**scenario,'observations_path':'requirement-package/反序观测.csv'})
    assert float(rows(second['source_path'])[4]['adjusted_score'])==pytest.approx(.164)
    assert float(rows(third['source_path'])[4]['adjusted_score'])==pytest.approx(.804)
    assert second['after']['cuts']==[] and third['after']['cuts']==[20,70]
    assert rows(first['source_path'])[4]==center


@pytest.mark.parametrize('step',[.25,10])
def test_two_state_cost_against_exhaustive_assignments(step):
    for scores in ([0,1,0,1],[.1,.9,.8,.1,.7],[.5]*5,[0]):
        for switching in (0,1,100):
            cfg={'grid_step':step,'keep_cost':3,'discard_cost':1,'switch_cost':switching}
            positions=[(i+.5)*step for i in range(len(scores))]
            plan=code.segment(positions,scores,cfg)
            costs=[]
            for actions in itertools.product((0,1),repeat=len(scores)):
                value=sum(step*(3*p if a==0 else 1) for a,p in zip(actions,scores))+switching*sum(a!=b for a,b in zip(actions,actions[1:]))
                costs.append(value)
            assert plan['cost']==pytest.approx(min(costs))
            assert plan['kept_length']+plan['discarded_length']==pytest.approx(step*len(scores))
            assert sum(s['points'] for s in plan['segments'])==len(scores)
    tied=code.segment([.5,1.5,2.5],[1,1,1],{'grid_step':1,'keep_cost':1,'discard_cost':1,'switch_cost':0})
    assert tied['labels']==[0,0,0]


def test_no_observations_zero_strength_clip_and_far_field(scenario):
    none=run({**scenario,'observations_path':'','score_floor':.2,'score_ceiling':.7})
    assert none['before']==none['after'] and none['observations']==0
    assert all(float(r['adjusted_score'])==.1 for r in rows(none['source_path']))
    zero=run({**scenario,'strength':0})
    assert zero['before']==zero['after']
    clipped=run({**scenario,'score_floor':.2,'score_ceiling':.7})
    assert float(rows(clipped['source_path'])[4]['adjusted_score'])==.7
    assert all(.2<=float(r['adjusted_score'])<=.7 for r in rows(clipped['source_path']))
    narrow=run({**scenario,'influence_width':.001})
    assert float(rows(narrow['source_path'])[3]['adjusted_score'])==.1
    assert float(rows(narrow['source_path'])[4]['adjusted_score'])==pytest.approx(.82)
    Path(scenario['observations_path']).write_text('observation_id,position,high_risk\n')
    assert run(scenario)['observations']==0


def test_source_snapshots_new_parameters_and_old_results(scenario):
    prepared=code.prepare(scenario);first=code.evaluate({'prepared':prepared});old=Path(first['source_path']).read_bytes()
    changed=run({**scenario,'switch_cost':100})
    assert changed['after']['cuts']==[] and changed['config']['switch_cost']==100
    Path(scenario['source_path']).write_text(recipe.example_files()['原风险.csv'].replace(',0.1,',',0.9,'))
    fresh=run(scenario)
    assert fresh['sources']['curve']['sha256']!=first['sources']['curve']['sha256']
    assert code.evaluate({'prepared':prepared})['after']==first['after']
    assert Path(first['source_path']).read_bytes()==old
    Path(prepared['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.evaluate({'prepared':prepared})


@pytest.mark.parametrize('patch,message',[
    ({'grid_step':0},'网格宽度'),({'influence_width':0},'影响宽度'),({'strength':1.1},'力度'),
    ({'keep_cost':-1},'单位代价'),({'switch_cost':'NaN'},'切换代价'),({'discard_cost':float('inf')},'单位代价'),
    ({'score_floor':.9,'score_ceiling':.2},'下限必须小于'),({'unit':''},'单位'),
    ({'position_column':'score'},'坐标列不能'),({'score_column':'missing'},'缺少字段'),
    ({'source_path':'../private.csv'},'当前项目'),({'grid_step':5},'网格宽度')])
def test_bad_config_specific_without_partial_results(scenario,patch,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**patch})
    assert not Path('results').exists()


@pytest.mark.parametrize('contents,message',[
    ('observation_id,position,high_risk\nx,45,unknown\n','必须明确'),
    ('observation_id,position,high_risk\nx,45,1\nx,55,0\n','重复'),
    ('observation_id,position,high_risk\nx,95,1\n','覆盖区间'),
    ('observation_id,position,high_risk,unit\nx,45,1,cm\n','单位'),
    ('observation_id,position\nx,45\n','缺少字段')])
def test_observation_errors_and_repair(scenario,contents,message):
    Path(scenario['observations_path']).write_text(contents)
    with pytest.raises(ValueError,match=message):run(scenario)
    Path(scenario['observations_path']).write_text(recipe.example_files()['观测.csv'])
    assert run(scenario)['changed_points']==5


def test_curve_gaps_duplicates_invalid_scores_and_excel(scenario):
    original=recipe.example_files()['原风险.csv'];path=Path(scenario['source_path'])
    for text, message in [(original.replace('15,0.1,mm\n',''),'网格宽度'),(original.replace('15,','5,'),'无重复'),
                          (original.replace('0.1','NaN',1),'原分数'),('position,score\n','没有数据'),
                          (original.replace('0.1','1.2',1),'原分数')]:
        path.write_text(text)
        with pytest.raises(ValueError,match=message):run(scenario)
    path.write_text(original)
    from openpyxl import Workbook
    book=Workbook();book.active.title='曲线'
    for r in csv.reader(original.splitlines()):book.active.append(r)
    book.create_sheet('说明');book.save('requirement-package/曲线.xlsx')
    with pytest.raises(ValueError,match='多张表'):run({**scenario,'source_path':'requirement-package/曲线.xlsx'})
    assert run({**scenario,'source_path':'requirement-package/曲线.xlsx','curve_sheet':'曲线'})['changed_points']==5


def test_platform_discovery_call_repair_and_download(configured):
    client,app,_,settings=configured;pid=install(client,'local-feedback');base='/api/v1/projects/'+pid
    listed=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'list'}})
    assert listed.status_code==200 and pid in listed.text
    inspected=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'inspect','workflow_id':pid}})
    assert inspected.status_code==200 and 'observations_path' in inspected.text
    response=client.post(base+'/agent-tools',json={'name':'workflow_run','arguments':{'action':'start','workflow_id':pid,'inputs':{}}})
    assert response.status_code==200,response.text
    first=response.json();assert first['status']=='succeeded',first.get('error')
    result=first['outputs']['result'];assert result['changed_points']==5
    for artifact in result['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path']).status_code==200
    bad=settled(client,base,start(client,base,'bad',workflow_id=pid,inputs={'influence_width':0}))
    assert bad['status']=='failed' and '影响宽度' in bad['error']
    files=client.get(base+'/example').json()['files']
    changed_path=next(f['path'] for f in files if f['path'].endswith('补充观测.csv'))
    fixed=settled(client,base,start(client,base,'repair',workflow_id=pid,inputs={'influence_width':15,'observations_path':changed_path}))
    assert fixed['status']=='succeeded' and fixed['outputs']['result']['after']['cuts']==[]
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
    assert client.get(base+'/modeling/studies').json()==[] and not app.state.services.local_agents.tasks


def test_decimal_grid_requires_actual_equal_spacing(scenario):
    Path(scenario['source_path']).write_text('position,score\n0.0000000000001,0.1\n0.0000000000003,0.2\n')
    with pytest.raises(ValueError,match='网格宽度'):run({**scenario,'grid_step':1e-13,'observations_path':''})
    Path(scenario['source_path']).write_text('position,score\n900000000.1,0.1\n900000000.2,0.2\n')
    assert run({**scenario,'grid_step':.1,'observations_path':''})['rows']==2
    with pytest.raises(ValueError,match='数值精度'):run({**scenario,'grid_step':1e-13,'observations_path':''})
