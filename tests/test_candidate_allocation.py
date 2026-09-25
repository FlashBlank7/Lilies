import csv
from decimal import Decimal
from pathlib import Path

import pytest
from agent_platform import candidate_allocation as recipe, candidate_allocation_code as code
from agent_platform import cutting_candidates as cutting, cutting_candidates_code as cutter, candidate_comparison_code as compare
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,text in recipe.example_files().items():Path('requirement-package',name).write_text(text,encoding='utf-8')
    return {**{f['name']:f['default'] for f in recipe.workflow()['nodes'][0]['config']['inputs']},
            **{k:v.replace('@file:','requirement-package/') if isinstance(v,str) else v for k,v in recipe.example_defaults().items()}}


def run(inputs):return code.allocate({'prepared':code.prepare(inputs)})


def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def selected(result):return [r['selected_candidate'] for r in result['selections'] if r['selected_candidate']]


def test_shared_limit_order_alternatives_and_explicit_not_global(scenario):
    first=run(scenario)
    assert selected(first)==['A优先']
    statuses={r['candidate_id']:r for r in rows(first['source_path'])}
    assert statuses['B唯一']['allocation_status']=='数量不足' and '当前剩余0' in statuses['B唯一']['allocation_reason']
    assert statuses['C失败']['allocation_status']=='排除'
    assert 'B唯一：共同份额需1，当前剩余0' in first['markdown'] and 'C失败：前序条件未通过' in first['markdown']
    assert rows(first['balances_path'])[0]['remaining']=='0'
    reversed_order=run({**scenario,'group_order_direction':'从大到小'})
    assert selected(reversed_order)==['B唯一','A节约']
    assert '顺序贪心' in first['markdown'] and '不证明全局无解' in first['markdown']
    assert selected(run({**scenario,'limits_path':'requirement-package/上限-增加.csv'}))==['A优先','B唯一']
    assert selected(run({**scenario,'group_order_column':'','group_order_direction':'从大到小'}))==['A优先']


def test_multiple_resources_exact_decimal_conservation(scenario):
    Path(scenario['source_path']).write_text('candidate_id,group_id,comparison_rank\nA,G1,1\nB,G2,1\nC,G2,2\nD,G3,1\n')
    Path(scenario['usage_path']).write_text('candidate_id,resource_id,quantity\nA,x,0.1\nA,y,1\nB,x,0.2\nB,y,2\nC,x,0.2\nC,y,1\nD,x,0.000001\n')
    Path(scenario['limits_path']).write_text('resource_id,capacity\nx,0.3\ny,2\nunused,10\n')
    result=run({**scenario,'group_order_column':''});assert selected(result)==['A','C']
    balance={r['resource_id']:r for r in rows(result['balances_path'])}
    assert balance['x']['remaining']=='0.0' and balance['y']['remaining']=='0' and balance['unused']['used']=='0'
    for r in balance.values():assert Decimal(r['capacity'])==Decimal(r['used'])+Decimal(r['remaining'])
    consumption=rows(Path(result['source_path']).with_name('consumption.csv'))
    for r in consumption:assert Decimal(r['before'])-Decimal(r['quantity'])==Decimal(r['after'])>=0


def test_prior_unknown_fail_blank_rank_and_ties(scenario):
    Path(scenario['source_path']).write_text('candidate_id,group_id,comparison_rank,comparison_status\nA优先,A,1,unknown\nA节约,A,1,pass\nB唯一,A,1,unchecked\nC失败,C,,pass\n')
    result=run({**scenario,'group_order_column':''});assert selected(result)==['A节约']
    assert rows(result['source_path'])[-1]['allocation_status']=='排除'
    assert '没有明确' in rows(result['source_path'])[-1]['allocation_reason']
    Path(scenario['source_path']).write_text('candidate_id,group_id,comparison_rank\nB唯一,A,1\nA节约,A,1\nA优先,A,2\nC失败,C,\n')
    assert selected(run({**scenario,'group_order_column':''}))==['B唯一']


def test_snapshots_and_updated_limits_keep_old_files(scenario):
    prepared=code.prepare(scenario);first=code.allocate({'prepared':prepared});old=Path(first['source_path']).read_bytes()
    Path(scenario['limits_path']).write_text(recipe.example_files()['上限-增加.csv'])
    second=run(scenario);assert second['selected_groups']==2
    assert first['sources']['limits']['sha256']!=second['sources']['limits']['sha256']
    assert selected(code.allocate({'prepared':prepared}))==['A优先']
    assert Path(first['source_path']).read_bytes()==old
    Path(prepared['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.allocate({'prepared':prepared})


@pytest.mark.parametrize('kind,content,message',[
    ('limits','resource_id,capacity\nx,1\nx,1\n','重复'),
    ('limits','resource_id,capacity\n共同份额,-1\n','不能为负'),
    ('limits','resource_id,capacity\n共同份额,NaN\n','有限数值'),
    ('usage','candidate_id,resource_id,quantity\nA优先,unknown,1\n','缺少明确上限'),
    ('usage','candidate_id,resource_id,quantity\nmissing,共同份额,1\n','不在本次候选'),
    ('usage','candidate_id,resource_id,quantity\nA优先,共同份额,1\nA优先,共同份额,1\n','资源重复'),
    ('usage','candidate_id,resource_id,quantity\nA优先,共同份额,1\n','缺少用量记录'),
    ('source','candidate_id,group_id,comparison_rank,group_priority\nx,G,0,1\n','正整数'),
    ('source','candidate_id,group_id,comparison_rank,group_priority\nx,G,1,1\nx,G,2,1\n','标识为空/重复'),
    ('source','candidate_id,group_id,comparison_rank,group_priority\nx,G,1,1\ny,G,2,2\n','处理顺序数值不一致'),
])
def test_bad_files_then_local_repair(scenario,kind,content,message):
    key=kind+'_path';old=Path(scenario[key]).read_bytes();Path(scenario[key]).write_text(content)
    with pytest.raises(ValueError,match=message):run(scenario)
    assert not Path('results').exists()
    Path(scenario[key]).write_bytes(old);assert run(scenario)['selected_groups']==1


def test_xlsx_sheet_path_and_config(scenario):
    with pytest.raises(ValueError,match='当前项目'):run({**scenario,'usage_path':'../other.csv'})
    with pytest.raises(ValueError,match='重名'):run({**scenario,'quantity_column':'candidate_id'})
    with pytest.raises(ValueError,match='方向'):run({**scenario,'group_order_direction':'guess'})
    from openpyxl import Workbook
    book=Workbook();book.active.title='候选'
    for row in csv.reader(recipe.example_files()['候选.csv'].splitlines()):book.active.append(row)
    book.create_sheet('说明');book.save('requirement-package/候选.xlsx')
    with pytest.raises(ValueError,match='多张表'):run({**scenario,'source_path':'requirement-package/候选.xlsx'})
    assert selected(run({**scenario,'source_path':'requirement-package/候选.xlsx','candidates_sheet':'候选'}))==['A优先']


def test_existing_cutting_and_comparison_outputs_need_no_regeneration(scenario):
    for name,content in cutting.example_files().items():Path('requirement-package',name).write_text(content)
    config={f['name']:f['default'] for f in cutting.workflow()['nodes'][0]['config']['inputs']}
    generated=cutter.generate({'prepared':cutter.prepare({**config,'stock_path':'requirement-package/物料.csv','demand_path':'requirement-package/需求.csv','kerf':1,'max_pieces':3})})
    compared=compare.compare({'prepared':compare.prepare({**generated['comparison_inputs'],'objectives':[dict(field='piece_count',direction='越大越好'),dict(field='remaining_length',direction='越小越好')]})})
    inputs={**scenario,'source_path':compared['source_path'],'usage_path':generated['patterns_path'],'limits_path':'requirement-package/需求.csv',
            'group_column':'stock_id','resource_column':'demand_id','capacity_column':'quantity','group_order_column':'stock_length','group_order_direction':'从大到小'}
    result=run(inputs)
    assert result['selected_groups']==2
    chosen=rows(result['groups_path']);assert chosen[0]['group']=='料一' and chosen[1]['group']=='料二'
    balance={r['resource_id']:r for r in rows(result['balances_path'])}
    assert balance['需求一']['used']=='2' and balance['需求二']['used']=='1'
    old=Path(generated['patterns_path']).read_bytes()
    assert run({**inputs,'group_order_direction':'从小到大'})['selected_groups']==2
    assert Path(generated['patterns_path']).read_bytes()==old


def test_platform_discover_execute_repair_and_download(configured):
    client,app,_,settings=configured;pid=install(client,'candidate-allocation');base='/api/v1/projects/'+pid
    response=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'list'}})
    assert response.status_code==200 and pid in response.text
    response=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'inspect','workflow_id':pid}})
    assert response.status_code==200 and 'usage_path' in response.text
    response=client.post(base+'/agent-tools',json={'name':'workflow_run','arguments':{'action':'start','workflow_id':pid,'inputs':{}}})
    assert response.status_code==200,response.text
    first=response.json();assert first['status']=='succeeded',first.get('error')
    assert first['outputs']['result']['selected_groups']==1
    for a in first['outputs']['result']['artifacts']:assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+a['file_path']).status_code==200
    bad=settled(client,base,start(client,base,'missing',workflow_id=pid,inputs={'capacity_column':'missing'}));assert bad['status']=='failed' and '缺少字段' in bad['error']
    fixed=settled(client,base,start(client,base,'fixed',workflow_id=pid,inputs={'capacity_column':'capacity','group_order_direction':'从大到小'}))
    assert fixed['status']=='succeeded' and fixed['outputs']['result']['selected_groups']==2
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
    assert client.get(base+'/modeling/studies').json()==[] and not app.state.services.local_agents.tasks
