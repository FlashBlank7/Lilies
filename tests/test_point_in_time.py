import csv
import json
from pathlib import Path

import pytest
from agent_platform import point_in_time as template, point_in_time_code as code
from tests.test_projects import configured, start, settled  # noqa: F401
from tests.test_example_projects import install


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root=Path('requirement-package');root.mkdir()
    for name,content in template.example_files().items():(root/name).write_text(content)
    inputs={f['name']:f.get('default') for f in template.workflow()['nodes'][0]['config']['inputs']}
    inputs.update(source_path='requirement-package/samples.csv',records_path='requirement-package/records.csv')
    return inputs


def run(inputs):
    return code.main({'operation':'align','prepared':code.main({'operation':'prepare',**inputs})})


def read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def test_publication_revision_entity_and_changed_input(scenario):
    original=Path(scenario['source_path']).read_bytes()
    result=run(scenario)
    assert [r['asof_quality'] for r in read(result['source_path'])]==['','11','12','20']
    assert result['counts']=={'no_available_record':1,'matched':3}
    details=read(next(a['file_path'] for a in result['artifacts'] if a['file_path'].endswith('matches.csv')))
    assert next(d for d in details if d['sample_id']=='later')['source_record']=='4'
    assert all(d['available_time']<=d['prediction_time'] for d in details if d['status']=='matched')
    assert not any('source_record' in r for r in read(result['source_path']))
    updated=run({**scenario,'records_path':'requirement-package/records-2.csv'})
    assert [r['asof_quality'] for r in read(updated['source_path'])]==['','110','120','200']
    assert updated['source_path']!=result['source_path']
    assert read(result['source_path'])[1]['asof_quality']=='11'
    assert Path(scenario['source_path']).read_bytes()==original


def test_expiry_missing_and_exact_boundary(scenario):
    file=Path(scenario['records_path'])
    file.write_text(file.read_text().replace(',11\n',',\n'))
    result=run(scenario)
    assert result['counts']['missing_value']==1
    assert read(result['source_path'])[1]['asof_quality']==''  # Do not fall back to value 10.
    assert run({**scenario,'max_age_hours':24})['counts']['stale']==3
    file.write_text('entity,feature,observed_time,available_time,value\nA,quality,2026-02-02T02:00:00Z,2026-02-02T02:00:00Z,7\n')
    assert read(run(scenario)['source_path'])[0]['asof_quality']=='7'
    file.write_text(file.read_text().replace('02:00:00Z,7','02:00:00.000001Z,7'))
    assert read(run(scenario)['source_path'])[0]['asof_quality']==''


@pytest.mark.parametrize('change,message',[
    ({'available_time':'unknown'},'缺少字段'),({'utc_offset':'+14:30'},'偏移'),
    ({'max_age_hours':-1},'非负'),({'features':'does-not-exist'},'没有所选'),
    ({'source_path':'../secret.csv'},'当前项目')])
def test_specific_configuration_errors(scenario,change,message):
    with pytest.raises(ValueError,match=message):run({**scenario,**change})


def test_duplicate_versions_and_dates_are_not_silently_guessed(scenario):
    file=Path(scenario['records_path']);old=file.read_text()
    file.write_text(old+old.splitlines()[1]+'\n')
    with pytest.raises(ValueError,match='重复'):run(scenario)
    file.write_text(old.replace('2026-02-03T09:00:00+08:00','2026-02-03'))
    with pytest.raises(ValueError,match='实际时分'):run(scenario)
    file.write_text(old)
    prepared=code.prepare(scenario)
    # A paused/reused intermediate value cannot silently refer to changed bytes.
    Path(prepared['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='发生改变'):code.align({'prepared':prepared})


def test_excel_and_shared_global_series(scenario):
    from openpyxl import Workbook
    records=read(scenario['records_path'])
    book=Workbook();book.active.append(list(records[0]))
    for row in records:book.active.append(list(row.values()))
    path=Path('requirement-package/records.xlsx');book.save(path)
    assert read(run({**scenario,'records_path':str(path)})['source_path'])[2]['asof_quality']=='12'
    path=Path(scenario['records_path'])
    path.write_text('feature,observed_time,available_time,value\nq,2026-01-01,2026-02-01T00:00:00+08:00,0\n')
    result=run({**scenario,'entity':''})
    assert all(r['asof_q']=='0' for r in read(result['source_path']))  # Real zero remains a zero.


def test_unsorted_sparse_histories_match_independent_lookup(scenario):
    import random
    from datetime import datetime,timedelta,timezone
    rng=random.Random(42);origin=datetime(2026,1,1,tzinfo=timezone.utc)
    records=[]
    for i in range(120):
        observed=origin+timedelta(hours=rng.randrange(96))
        available=origin+timedelta(hours=rng.randrange(96),seconds=i)
        records.append([rng.choice(['A','B']),'q'+str(i%2),observed.isoformat(),available.isoformat(),str(i)])
    rng.shuffle(records)
    samples=[[f's{i}',rng.choice(['A','B','C']),(origin+timedelta(hours=rng.randrange(96))).isoformat(),1] for i in range(25)]
    for key,fields,rows in [('source_path',['sample_id','entity','prediction_time','target'],samples),
                            ('records_path',['entity','feature','observed_time','available_time','value'],records)]:
        with Path(scenario[key]).open('w',newline='') as f:
            writer=csv.writer(f);writer.writerow(fields);writer.writerows(rows)
    result=read(run(scenario)['source_path'])
    for sample,actual in zip(samples,result):
        for feature in ['q0','q1']:
            eligible=[r for r in records if r[0]==sample[1] and r[1]==feature and
                      datetime.fromisoformat(r[2])<=datetime.fromisoformat(sample[2]) and
                      datetime.fromisoformat(r[3])<=datetime.fromisoformat(sample[2])]
            expected=max(eligible,key=lambda r:(datetime.fromisoformat(r[2]),datetime.fromisoformat(r[3])))[4] if eligible else ''
            assert actual['asof_'+feature]==expected


def test_platform_install_run_download_repair_and_reuse(configured):
    client,app,_,settings=configured
    pid=install(client,'point-in-time');base='/api/v1/projects/'+pid
    task=settled(client,base,start(client,base,'first',workflow_id=pid,inputs={}))
    assert task['status']=='succeeded',task.get('error')
    output=task['outputs']['result']
    assert output['counts']=={'no_available_record':1,'matched':3}
    for artifact in output['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path']).status_code==200
    failed=settled(client,base,start(client,base,'bad-field',workflow_id=pid,inputs={'available_time':'missing'}))
    assert failed['status']=='failed' and 'missing' in failed['error']
    repaired=settled(client,base,start(client,base,'repaired',workflow_id=pid,inputs={'max_age_hours':24}))
    assert repaired['status']=='succeeded' and repaired['outputs']['result']['counts']['stale']==3
    assert client.get(base+'/tasks/'+task['id']).json()['outputs']==task['outputs']
    assert not client.get(base+'/modeling/studies').json()
    # An existing analysis recipe consumes the output, without internal dataset IDs.
    from agent_platform.example_catalog import code_graph, field
    added=client.post(base+'/space/workflows',json={'name':'检查制备后的样本',
        'workflow':code_graph('profile',[field('source_path','资料表')])}).json()['workflow_id']
    downstream=settled(client,base,start(client,base,'downstream',workflow_id=added,
        inputs={'source_path':output['source_path']}))
    assert downstream['status']=='succeeded',downstream.get('error')
    assert downstream['outputs']['result']['rows']==4
    assert downstream['outputs']['result']['missing']['asof_quality']==1
    rows=read(settings.workspace_root/pid/output['source_path'])
    assert len(rows)==4 and rows[1]['target']=='2'
