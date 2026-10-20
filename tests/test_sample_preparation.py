import csv
import json
from pathlib import Path

import pytest
from agent_platform import sample_preparation as template, sample_preparation_code as code
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,content in template.example_files().items():Path('requirement-package',name).write_text(content,encoding='utf-8')
    return {**{f['name']:f['default'] for f in template.workflow()['nodes'][0]['config']['inputs']},
            **template.example_defaults(),'source_path':'requirement-package/samples.csv'}


def run(inputs):return code.report(code.transform({'prepared':code.prepare(inputs)}))


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def artifact(result,name):return next(f['file_path'] for f in result['artifacts'] if f['file_path'].endswith('/'+name))


def test_sample_dispositions_formulas_conflicts_and_original_preserved(scenario):
    original=Path(scenario['source_path']).read_bytes();result=run(scenario);rows=csv_rows(result['source_path'])
    assert (result['rows'],result['retained'],result['excluded'])==(10,5,5)
    assert result['exact_duplicate_extras']==1 and result['conflicting_records']==2
    assert [r['sample_id'] for r in rows]==['A','B','C','F','F']
    assert rows[0]['length_gap']=='2' and rows[0]['load_per_length']=='2'
    assert rows[1]['load_per_length']=='' and rows[2]['length_a']=='' and rows[2]['length_gap']==''
    assert result['labels_after']=={'pass':3,'review':2}
    assert 'after_check' not in rows[0] and not any(f.startswith('preparation_') for f in rows[0])
    excluded=csv_rows(artifact(result,'excluded.csv'))
    assert len(excluded)==5 and excluded[0]['preparation_source_record']=='3'
    assert excluded[0]['sample_id']=='A' and excluded[0]['after_check']=='合格'
    status=csv_rows(artifact(result,'row-status.csv'))
    assert len(status)==10 and [r['output_record'] for r in status if r['output_record']]==['2','3','4','5','6']
    assert all('同一标识' in r['notes'] for r in status if r['source_record'] in ['8','9'])
    assert any('分母为0' in r['reason'] for r in csv_rows(artifact(result,'issues.csv')))
    assert Path(scenario['source_path']).read_bytes()==original


def test_changed_policy_and_saved_method_new_data_are_isolated(scenario):
    first=run(scenario);saved=Path(first['source_path']).read_bytes()
    strict=run({**scenario,'invalid_policy':'排除有转换问题的记录'})
    assert strict['retained']==3 and strict['excluded']==7 and strict['method_sha256']!=first['method_sha256']
    new=run({**scenario,'source_path':'requirement-package/new-samples.csv','method_path':first['method_path'],
             'purpose':'新数据预测前处理','features':[],'label_column':'not-used'})
    rows=csv_rows(new['source_path'])
    assert new['retained']==2 and new['method_sha256']==first['method_sha256']
    assert rows[0]['load_per_length']=='2' and rows[1]['load_per_length']==''
    assert 'quality' not in new['output_fields'] and 'after_check' not in new['output_fields']
    assert new['method_from']==first['method_path'] and '其余方法表单参数未使用' in new['markdown']
    assert Path(first['source_path']).read_bytes()==saved
    # Same-name changes do not change a running task's prepared input.
    prepared=code.prepare(scenario);Path(scenario['source_path']).write_text('changed')
    assert code.transform({'prepared':prepared})['retained']==5
    Path(prepared['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.transform({'prepared':prepared})
    method=Path(first['method_path']);data=json.loads(method.read_text());data['method']['features']=[];method.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='方法文件已改变'):run({**scenario,'source_path':'requirement-package/new-samples.csv','method_path':str(method),'purpose':'新数据预测前处理'})


def test_exact_label_mapping_does_not_truncate_or_infer_classes(scenario):
    Path(scenario['source_path']).write_text('id,x,label\na,1,1.9\nb,2,1\nc,3,1.0\nd,4,\ne,5,-1\n')
    base={**scenario,'required_columns':'id','numeric_columns':'x','id_columns':'id','label_column':'label','drop_columns':'','features':[],
          'label_map':[{'from':'1','to':'known'}]}
    result=run(base);rows=csv_rows(result['source_path'])
    assert result['retained']==1 and rows[0]['id']=='b' and rows[0]['label']=='known'
    assert result['exclusion_reasons']['标签未列入对应关系']==3
    unmapped=run({**base,'label_map':[]})
    assert [r['label'] for r in csv_rows(unmapped['source_path'])]==['1.9','1','1.0','-1']


@pytest.mark.parametrize('op,a,b,c,expected',[
    ('两列相加','0.1','0.2','','0.3'),('第一列减第二列','10','3','','7'),('两列差的绝对值','3','10','','7'),
    ('两列相乘','3','-2','','-6'),('第一列除以第二列','9','3','','3'),('乘固定数','3','','2.5','7.5'),('加固定数','3','','-3','0')])
def test_row_formula_semantics(op,a,b,c,expected):
    assert code.calculate({'a':a,'b':b},dict(left='a',right='b',constant=c,operation=op))==expected


def test_sequential_features_no_cross_row_fit_or_missing_value_imputation(scenario):
    Path(scenario['source_path']).write_text('id,x,y\na,1,2\nb,,3\n')
    base={**scenario,'label_column':'','label_map':[],'required_columns':'id','numeric_columns':'x\ny','id_columns':'id','drop_columns':'',
        'features':[dict(output='sum',left='x',right='y',constant='',operation='两列相加'),dict(output='twice',left='sum',right='',constant='2',operation='乘固定数')]}
    first=run(base);r=csv_rows(first['source_path']);assert r[0]['twice']=='6' and r[1]['x']=='' and r[1]['twice']==''
    with Path(scenario['source_path']).open('a') as f:f.write('future,1000000,2000000\n')
    more=run(base);assert csv_rows(more['source_path'])[:2]==r and more['method_sha256']==first['method_sha256']


@pytest.mark.parametrize('patch,message',[
    ({'numeric_columns':'quality'},'标签列不应'),({'required_columns':'unknown'},'缺少处理方法要求'),
    ({'features':[dict(output='x',left='quality',operation='乘固定数',constant='2')]},'标签'),
    ({'features':[dict(output='x',left='after_check',operation='乘固定数',constant='2')]},'被排除'),
    ({'features':[dict(output='length_a',left='load',operation='乘固定数',constant='2')]},'新的、不重名'),
    ({'features':[dict(output='x',left='load',operation='乘固定数',constant='NaN')]},'有效数值'),
    ({'label_map':[dict(**{'from':'a','to':'1'}),dict(**{'from':'a','to':'2'})]},'标签原值重复'),
    ({'duplicate_policy':'guess'},'重复处理方式'),({'source_path':'../secret.csv'},'当前项目')])
def test_configuration_errors_are_specific(scenario,patch,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**patch})


def test_no_labels_no_rules_empty_rows_and_explicit_excel_sheet(scenario):
    from openpyxl import Workbook
    book=Workbook();ws=book.active;ws.title='数据';ws.append(['id','text']);ws.append(['a','1']);ws.append(['b',None]);book.create_sheet('说明');book.save('requirement-package/t.xlsx')
    base={f['name']:f['default'] for f in template.workflow()['nodes'][0]['config']['inputs']};base['source_path']='requirement-package/t.xlsx'
    with pytest.raises(ValueError,match='多张表'):run(base)
    r=run({**base,'sheet':'数据'});assert r['retained']==2 and csv_rows(r['source_path'])[1]['text']==''
    Path('requirement-package/empty.csv').write_text('id,x\n,\n,\n')
    empty=run({**base,'source_path':'requirement-package/empty.csv'})
    assert empty['retained']==0 and empty['excluded']==2 and '没有剩余样本' in empty['markdown']


def test_reused_method_refuses_changed_processor_but_fresh_method_can_run(scenario,monkeypatch):
    original=run(scenario)
    monkeypatch.setattr(code,'__workflow_code_sha256__','changed-code',raising=False)
    with pytest.raises(ValueError,match='处理代码与保存方法时不同'):run({**scenario,'method_path':original['method_path']})
    changed=run(scenario)
    assert changed['processor_sha256']=='changed-code' and changed['method_sha256']==original['method_sha256']
    assert changed['source_path']!=original['source_path']


def test_platform_install_run_repair_and_handoff_to_existing_analysis(configured):
    client,app,_,settings=configured;pid=install(client,'sample-preparation');base='/api/v1/projects/'+pid
    task=settled(client,base,start(client,base,'prepare',workflow_id=pid,inputs={}))
    assert task['status']=='succeeded',task.get('error')
    result=task['outputs']['result'];assert result['retained']==5 and result['excluded']==5
    for file in result['artifacts']:assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+file['file_path']).status_code==200
    guide=client.get(base+'/example').json();new=next(f['path'] for f in guide['files'] if f['name']=='new-samples.csv')
    bad=settled(client,base,start(client,base,'no-label',workflow_id=pid,inputs={'source_path':new}))
    assert bad['status']=='failed' and 'quality' in bad['error']
    fixed=settled(client,base,start(client,base,'new',workflow_id=pid,inputs={'source_path':new,'method_path':result['method_path'],'purpose':'新数据预测前处理'}))
    assert fixed['status']=='succeeded' and fixed['outputs']['result']['retained']==2
    # Reuse an existing analysis block; no parallel data/ML service.
    from agent_platform.official_workflows import graph,node,ref
    workflow=graph([node('start','start','输入'),node('analyze','data_analysis','分析处理后的数据',source_path=result['source_path'],mapping={'target':'quality'}),node('end','end','分析',outputs={'data':ref('analyze','output')})])
    added=client.post(base+'/space/workflows',json={'name':'分析处理后的表','workflow':workflow});assert added.status_code==201,added.text
    analysis=settled(client,base,start(client,base,'handoff',workflow_id=added.json()['workflow_id'],inputs={}))
    assert analysis['status']=='succeeded',analysis.get('error')
    assert client.get(base+'/tasks/'+task['id']).json()['outputs']==task['outputs']
    assert not app.state.services.local_agents.tasks
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()
    workflow=draft['snapshot']['workflow']
    processor=next(n for n in workflow['nodes'] if n['id']=='transform')
    processor['config']['code']=processor['config']['code'].replace('return str(value)','return str(value + 1)')
    updated=client.put(base+'/workflows/'+pid+'/draft',json={'expected_revision':draft['revision'],'workflow':workflow})
    assert updated.status_code==200,updated.text
    stale=settled(client,base,start(client,base,'changed-code-old-method',workflow_id=pid,inputs={'method_path':result['method_path']}))
    assert stale['status']=='failed' and '处理代码与保存方法时不同' in stale['error']
    fresh=settled(client,base,start(client,base,'changed-code-new-method',workflow_id=pid,inputs={}))
    assert fresh['status']=='succeeded',fresh.get('error')
    assert fresh['outputs']['result']['processor_sha256']!=result['processor_sha256']
    assert fresh['outputs']['result']['preview'][0]['length_gap']=='3'
