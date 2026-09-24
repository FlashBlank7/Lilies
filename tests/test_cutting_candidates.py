import csv
import itertools
import json
from decimal import Decimal
from pathlib import Path

import pytest
from agent_platform import cutting_candidates as recipe, cutting_candidates_code as code, candidate_comparison_code as comparison
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,content in recipe.example_files().items():Path('requirement-package',name).write_text(content,encoding='utf-8')
    return {**{f['name']:f['default'] for f in recipe.workflow()['nodes'][0]['config']['inputs']},
        'stock_path':'requirement-package/物料.csv','demand_path':'requirement-package/需求.csv','kerf':1,'max_pieces':3}


def run(inputs):return code.generate({'prepared':code.prepare(inputs)})


def read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def test_patterns_exact_lengths_and_handoff(scenario):
    result=run(scenario)
    assert [s['candidates'] for s in result['stocks']]==[4,2,0,0]
    assert result['rows']==6 and result['comparison_inputs']['objectives']==[]
    assert '长度与切缝' in result['stocks'][2]['reason'] and '同类型' in result['stocks'][3]['reason']
    for r in read(result['source_path']):
        assert Decimal(r['product_length'])+Decimal(r['kerf_loss'])+Decimal(r['remaining_length'])+Decimal(r['end_allowance'])==Decimal(r['stock_length'])
    targets=[dict(field='piece_count',direction='越大越好'),dict(field='remaining_length',direction='越小越好')]
    after=comparison.compare({'prepared':comparison.prepare({**result['comparison_inputs'],'objectives':targets})})
    assert [g['rows'] for g in after['groups']]==[4,2]
    selected=[r for r in read(after['source_path']) if r['comparison_rank']=='1']
    assert [r['combination'] for r in selected]==['需求一 × 1；需求二 × 1','需求二 × 1']


@pytest.mark.parametrize('mode',['每段各计一道切缝','仅相邻段间计切缝'])
def test_enumeration_against_independent_cartesian_product(scenario,mode):
    Path(scenario['stock_path']).write_text('stock_id,material,length\nS,x,1.03\n')
    Path(scenario['demand_path']).write_text('demand_id,material,length,quantity\nA,x,0.1,5\nB,x,0.3,2\nC,x,0.47,2\n')
    lengths=[Decimal('.1'),Decimal('.3'),Decimal('.47')];kerf=Decimal('.01');allowance=Decimal('.02')
    for maxpieces in [2,4]:
        for types in [1,2,3]:
            result=run({**scenario,'kerf':'.01','end_allowance':'.02','max_pieces':maxpieces,'max_types':types,'kerf_mode':mode})
            expected=set()
            for counts in itertools.product(range(6),range(3),range(3)):
                n=sum(counts);cuts=n if mode=='每段各计一道切缝' else max(0,n-1)
                if 0<n<=maxpieces and sum(bool(v) for v in counts)<=types and sum(c*l for c,l in zip(counts,lengths))+cuts*kerf+allowance<=Decimal('1.03'):expected.add(counts)
            patterns={}
            for r in read(result['patterns_path']):patterns.setdefault(r['candidate_id'],{})[r['demand_id']]=int(r['quantity'])
            actual={tuple(p.get(k,0) for k in 'ABC') for p in patterns.values()}
            assert actual==expected


def test_explicit_loss_modes_and_no_rounding_into_feasibility(scenario):
    Path(scenario['stock_path']).write_text('stock_id,material,length\nS,x,0.3\n')
    Path(scenario['demand_path']).write_text('demand_id,material,length,quantity\nA,x,0.1,3\n')
    assert max(int(r['piece_count']) for r in read(run({**scenario,'kerf':0})['source_path']))==3
    assert max(int(r['piece_count']) for r in read(run({**scenario,'kerf':'0.000001'})['source_path']))==2
    Path(scenario['stock_path']).write_text('stock_id,material,length\nS,x,0.31\n')
    Path(scenario['demand_path']).write_text('demand_id,material,length,quantity\nA,x,0.15,2\n')
    assert run({**scenario,'kerf':'.01','kerf_mode':'每段各计一道切缝'})['rows']==1
    assert run({**scenario,'kerf':'.01','kerf_mode':'仅相邻段间计切缝'})['rows']==2


def test_changed_inputs_and_frozen_source(scenario):
    first=run(scenario);old=Path(first['source_path']).read_bytes();prepared=code.prepare(scenario)
    Path(scenario['demand_path']).write_text(recipe.example_files()['需求-变更.csv'])
    second=run(scenario);assert [s['candidates'] for s in second['stocks']]==[4,2,1,0]
    assert code.generate({'prepared':prepared})['rows']==6
    assert first['sources']['demand']['sha256']!=second['sources']['demand']['sha256']
    assert Path(first['source_path']).read_bytes()==old
    Path(prepared['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.generate({'prepared':prepared})


@pytest.mark.parametrize('patch,message',[
    ({'kerf':''},'单次切缝'),({'kerf':'NaN'},'单次切缝'),({'kerf':'0.0000001'},'6位'),
    ({'max_pieces':0},'1至12'),({'max_types':7},'1至6'),({'search_limit':99},'100至200000'),
    ({'kerf_mode':'猜测'},'计数方式'),({'stock_path':'../outside.csv'},'当前项目')])
def test_configuration_errors_before_writing(scenario,patch,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**patch})
    assert not Path('results').exists()


def test_search_limit_keeps_input_without_partial_candidate_table(scenario):
    Path(scenario['stock_path']).write_text('stock_id,material,length\nS,x,10000\n')
    Path(scenario['demand_path']).write_text('demand_id,material,length,quantity\n'+'\n'.join(f'D{i},x,1,12' for i in range(6)))
    prepared=code.prepare({**scenario,'max_pieces':12,'max_types':6,'search_limit':100})
    with pytest.raises(ValueError,match='未完整生成'):code.generate({'prepared':prepared})
    assert Path(prepared['snapshot_path']).is_file() and not list(Path('results').glob('cutting-candidates-*'))
    fixed=run({**scenario,'max_pieces':1,'max_types':1,'search_limit':100})
    assert fixed['rows']==6 and fixed['complete_within_declared_limits']


def test_units_duplicates_missing_fields_excel_and_no_candidate(scenario):
    p=Path(scenario['stock_path']);original=p.read_text()
    for content,message in [(original.replace(',mm',',cm',1),'单位'),(original+'料一,类型A,100,mm\n','重复'),(original.replace('stock_id','id'),'缺少字段'),(original.replace('料一,类型A','料一,'),'物料类型')]:
        p.write_text(content)
        with pytest.raises(ValueError,match=message):run(scenario)
    p.write_text(original)
    from openpyxl import Workbook
    book=Workbook();sheet=book.active;sheet.title='物料'
    for row in csv.reader(original.splitlines()):sheet.append(row)
    book.create_sheet('说明');book.save('requirement-package/物料.xlsx')
    with pytest.raises(ValueError,match='多张表'):run({**scenario,'stock_path':'requirement-package/物料.xlsx'})
    assert run({**scenario,'stock_path':'requirement-package/物料.xlsx','stock_sheet':'物料'})['rows']==6
    result=run({**scenario,'end_allowance':200})
    assert result['rows']==0 and result['comparison_inputs'] is None
    assert all(s['reason']=='预留后没有可用长度' for s in result['stocks']) and not read(result['source_path'])


def test_platform_workflows_create_run_fix_and_compare(configured):
    client,app,_,settings=configured;pid=install(client,'cutting-candidates');base='/api/v1/projects/'+pid
    first=settled(client,base,start(client,base,'cut',workflow_id=pid,inputs={}))
    assert first['status']=='succeeded',first.get('error')
    result=first['outputs']['result'];assert result['rows']==6
    for file in result['artifacts']:assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+file['file_path']).status_code==200
    discovered=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'list'}})
    assert discovered.status_code==200
    compare_id=next(m['id'] for m in discovered.json()['members'] if m['name']=='候选方案约束检查与比较')
    inspected=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'inspect','workflow_id':compare_id}})
    assert inspected.status_code==200 and 'source_path' in inspected.text
    compared_response=client.post(base+'/agent-tools',json={'name':'workflow_run','arguments':{'action':'start','workflow_id':compare_id,'inputs':{**result['comparison_inputs'],'objectives':[dict(field='piece_count',direction='越大越好')]}}})
    assert compared_response.status_code==200,compared_response.text
    compared=compared_response.json()
    assert compared['status']=='succeeded',compared.get('error')
    bad=settled(client,base,start(client,base,'bad',workflow_id=pid,inputs={'kerf':-1}));assert bad['status']=='failed' and '单次切缝' in bad['error']
    fixed=settled(client,base,start(client,base,'fixed',workflow_id=pid,inputs={'kerf':0}));assert fixed['status']=='succeeded' and fixed['outputs']['result']['rows']==7
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
    assert client.get(base+'/modeling/studies').json()==[] and not app.state.services.local_agents.tasks
