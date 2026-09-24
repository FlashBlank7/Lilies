import csv
import asyncio
import io
import json
from pathlib import Path
import zipfile
import threading
import time

import pytest
from agent_platform import rolling_forecast as template, rolling_forecast_code as code
from tests.test_projects import configured, start, graph  # noqa: F401
from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401
from tests.test_official_workflows import install
from tests.test_example_projects import install as install_example


@pytest.fixture
def scenario(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,content in template.example_files().items():Path('requirement-package',name).write_text(content,encoding='utf-8')
    return {**{f['name']:f['default'] for f in template.workflow()['nodes'][0]['config']['inputs']},
            **template.example_defaults(),'source_path':'requirement-package/history.csv'}


def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))


def rewrite(path,records):code.save_csv(Path(path),list(records[0]),records)


def first(inputs):
    prepared=code.prepare(inputs)
    return prepared,code.window({'prepared':prepared,'job':prepared['jobs'][0]})


def test_future_changes_do_not_leak_and_old_snapshot_survives(scenario):
    p,w=first(scenario);train=Path(w['train_path']).read_bytes();predict=Path(w['predict_path']).read_bytes()
    records=rows(scenario['source_path'])
    for r in records:
        if r['time']>p['jobs'][0]['origin']:r['value']='999999'
    rewrite(scenario['source_path'],records);changed,new=first(scenario)
    assert changed['sha256']!=p['sha256']
    assert Path(new['train_path']).read_bytes()==train
    assert Path(new['predict_path']).read_bytes()==predict
    assert code.load(p)['source']['sha256']!=code.load(changed)['source']['sha256']
    assert json.loads(Path(new['window_path']).read_text())['truth'][0]['actual']==999999


def test_features_are_asof_sample_origin_and_labels_asof_training_origin(scenario):
    records=rows(scenario['source_path'])
    records.append({**records[20],'available':records[50]['time'],'value':'777'})
    records.append({**records[20],'available':'2026-01-01T00:00:00+00:00','value':'999'})
    rewrite(scenario['source_path'],records)
    p,w=first(scenario);training={r['sample_id']:r for r in rows(w['train_path'])}
    assert training['20:1']['lag_0']==records[20]['value']
    assert training['19:1']['target']=='777.0'
    assert all(r['label_available_time']<=p['jobs'][0]['origin'] for r in training.values())
    data=code.load(p);versions={r['source_record']:r for r in data['series']['示例序列']}
    for r in rows(str(Path(w['train_path']).with_name('sample-sources.csv'))):
        for ident in r['feature_source_records'].split(';'):
            if ident:assert versions[int(ident)]['available']<=r['origin_time']


def test_missing_period_not_next_row_delayed_latest_not_a_feature(scenario):
    records=rows(scenario['source_path']);del records[81];rewrite(scenario['source_path'],records)
    p,w=first(scenario);meta=json.loads(Path(w['window_path']).read_text())
    assert meta['truth'][0]['target_time']=='2025-03-23T00:00:00+00:00'
    assert meta['truth'][0]['actual'] is None and meta['truth'][1]['actual'] is not None
    p=code.prepare({**scenario,'source_path':'requirement-package/history-late.csv','forecast_last':True})
    w=code.window({'prepared':p,'job':p['jobs'][-1]});meta=json.loads(Path(w['window_path']).read_text())
    assert rows(w['predict_path'])[0]['lag_0']==''
    assert meta['truth'][0]['last_value_time']=='2025-03-30T00:00:00+00:00'
    assert all(r['actual'] is None for r in meta['truth'])


def test_sliding_window_and_seasonal_steps_beyond_one_cycle(scenario):
    p,w=first({**scenario,'horizon':9,'training_window':30,'seasonal_period':7})
    indices=[int(r['sample_id'].split(':')[0]) for r in rows(w['train_path'])]
    assert min(indices)==p['jobs'][0]['index']-30
    meta=json.loads(Path(w['window_path']).read_text());source=rows(scenario['source_path'])
    oi=p['jobs'][0]['index']
    for t in meta['truth']:
        h=t['horizon'];index=oi+h-7*((h+6)//7)
        assert t['seasonal_naive']==float(source[index]['value'])
    assert len({r['sample_id'] for r in rows(w['predict_path'])})==9


def test_negative_and_zero_values_are_valid(scenario):
    records=rows(scenario['source_path'])
    for i,r in enumerate(records):r['value']=str(-i)
    rewrite(scenario['source_path'],records);p,w=first(scenario)
    assert not code.load(p)['issues']
    assert rows(w['train_path'])[0]['lag_0']=='0.0'
    assert json.loads(Path(w['window_path']).read_text())['truth'][0]['actual']<0


@pytest.mark.parametrize('changes,message',[
    ({'time_column':'absent'},'列是否存在'),({'step_seconds':0},'整数'),({'lags':True},'有限数值'),
    ({'direction_tolerance':-1},'不能为负'),({'forecast_last':'false'},'请选择是或否'),
    ({'lower_boundary':2,'upper_boundary':1},'低边界'),({'timezone':'+15:00'},'超出范围'),
    ({'first_origin':'2025-03-01T12:00:00Z'},'时间网格'),({'training_window':2},'训练样本不足'),
])
def test_invalid_configuration_has_concrete_failure(scenario,changes,message):
    with pytest.raises(ValueError,match=message):first({**scenario,**changes})


def test_duplicate_revision_invalid_time_and_snapshot_change(scenario):
    records=rows(scenario['source_path']);rewrite(scenario['source_path'],records+[records[0]])
    with pytest.raises(ValueError,match='重复'):code.prepare(scenario)
    records[1]['available']='2024-01-01'
    rewrite(scenario['source_path'],records)
    with pytest.raises(ValueError,match='观测发生前'):code.prepare(scenario)
    records[1]['available']=records[1]['time'];records[2]['time']='2025-01-03T01:00:00Z'
    records[2]['available']=records[2]['time'];rewrite(scenario['source_path'],records)
    with pytest.raises(ValueError,match='时间网格'):code.prepare(scenario)
    p=code.prepare({**scenario,'source_path':'requirement-package/history-late.csv'})
    Path(p['snapshot_path']).write_text('{}')
    with pytest.raises(ValueError,match='快照已改变'):code.load(p)


def test_excel_multiple_sheets_and_readonly_source(scenario):
    from openpyxl import Workbook
    book=Workbook();book.active.title='history';book.create_sheet('notes')
    records=rows(scenario['source_path']);book['history'].append(list(records[0]))
    for r in records:book['history'].append(list(r.values()))
    path=Path('requirement-package/history.xlsx');book.save(path);original=path.read_bytes()
    with pytest.raises(ValueError,match='多张表'):code.prepare({**scenario,'source_path':str(path)})
    first({**scenario,'source_path':str(path),'sheet':'history'})
    assert path.read_bytes()==original


def test_metric_denominators_direction_boundaries_and_tied_ranks():
    cfg={'direction_tolerance':1,'lower_boundary':-10,'upper_boundary':3}
    data=[dict(actual=3,model_prediction=4,last_value=1),dict(actual=1,model_prediction=3,last_value=1)]
    result=code.metrics(data,'model_prediction',cfg)
    assert result['mae']==1.5 and result['bias']==1.5 and result['direction_accuracy']==.5
    assert result['upper_precision']==.5 and result['upper_recall']==1
    assert result['lower_precision'] is None and result['lower_recall'] is None
    assert code.rank_correlation([1,1,2],[2,2,3])==1
    assert code.rank_correlation([1,1],[1,2]) is None
    assert code.rank_correlation([1,2,3],[3,2,1])==-1


def test_report_common_support_future_exclusion_and_origin_bundle(scenario):
    p=code.prepare({**scenario,'origins':1,'forecast_last':True});results=[]
    for job in p['jobs']:
        w=code.window({'prepared':p,'job':job});meta=json.loads(Path(w['window_path']).read_text())
        # One unavailable baseline must reduce the common denominator for every method.
        meta['truth'][0]['seasonal_naive']=None
        Path(w['window_path']).write_text(json.dumps(meta))
        path=Path(w['predict_path']).with_name('test-double-prediction.csv')
        code.save_csv(path,['sample_id','prediction'],[dict(sample_id=r['sample_id'],prediction=r['actual'] or 0) for r in meta['truth']])
        prediction={'project_path':str(path),'model_version':{'study_id':str(job['index']),'candidate_id':'test-double','slot':0,'image':'test'}}
        results.append(code.collect({'window':w,'prediction':prediction,'training':{'trials':[]}}))
    result=code.report({'prepared':p,'results':results})
    assert result['rows']==6 and len(result['by_origin'])==3
    assert {r['rows'] for r in result['by_horizon'] if r['horizon']==1}=={0}
    assert {r['rows'] for r in result['by_horizon'] if r['horizon']==2}=={1}
    assert next(r for r in result['by_horizon'] if r['method']=='model_prediction' and r['horizon']==2)['mae']==0
    bundle=next(a for a in result['artifacts'] if a['file_path'].endswith('.zip'))
    with zipfile.ZipFile(bundle['file_path']) as archive:
        assert len(archive.namelist())==11
        assert 'origin-1/train.csv' in archive.namelist() and 'origin-2/predictions.csv' in archive.namelist()
    with pytest.raises(ValueError,match='实际模型版本'):
        code.collect({'window':w,'prediction':{'project_path':str(path)},'training':{}})


def test_real_native_iteration_training_predictions_and_reuse(real_compute):
    (client,app,project,settings),service=real_compute
    pid=install_example(client,'rolling-forecast');base='/api/v1/projects/'+pid;wid=pid
    upload=client.post(base+'/materials',files={'file':('history.csv',template.example_files()['history.csv'].encode(),'text/csv')}).json()
    inputs={**template.example_defaults(),'source_path':upload['path'],'origins':2,'horizon':2}
    first=wait_task(client,base,start(client,base,'rolling-real',workflow_id=wid,inputs=inputs),seconds=240)
    assert first['status']=='succeeded',first.get('error')
    result=first['outputs']['result'];assert result['rows']==4
    assert len({r['model_version']['study_id'] for r in result['origins']})==2
    assert all(r['trials'][0]['status']=='completed' for r in result['origins'])
    for artifact in result['artifacts']:
        response=client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path'])
        assert response.status_code==200
        if artifact['file_path'].endswith('predictions.csv'):
            data=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
            assert len(data)==4 and all(float(r['model_prediction'])==float(r['model_prediction']) for r in data)
    assert start(client,base,'rolling-real',workflow_id=wid,inputs=inputs)['id']==first['id']
    original=result['origins'][0];version=original['model_version']
    prediction_path=str(Path(original['window_path']).with_name('prediction-input.csv'))
    independent=asyncio.run(service.run_block({'project_id':pid,'task_id':'independent-check'},'model_predict',
        {'source_path':prediction_path,'study_id':version['study_id'],'candidate_id':version['candidate_id'],'slot':version['slot']},'independent-check','predict'))
    predicted=rows(settings.workspace_root/pid/independent['project_path'])
    expected=[r for r in rows(settings.workspace_root/pid/result['source_path']) if r['origin']==original['job']['origin']]
    assert [float(r['prediction']) for r in predicted]==pytest.approx([float(r['model_prediction']) for r in expected],abs=1e-10)
    draft=client.get('/api/v1/applications/'+wid+'/draft').json()['snapshot']['workflow']
    draft['nodes'][-1]['config']['outputs']['note']='只修改说明'
    graph(client,wid,draft['nodes'],draft['edges'])
    reused=wait_task(client,base,start(client,base,'rolling-report-only',workflow_id=wid,reuse_task_id=first['id'],inputs=first['inputs']),seconds=240)
    assert reused['status']=='succeeded',reused.get('error')
    assert reused['outputs']['note']=='只修改说明'
    assert reused['outputs']['result']['origins']==result['origins']


def test_stop_between_origins_resume_fixed_input_without_duplicate_training(real_compute,monkeypatch):
    (client,app,project,settings),service=real_compute
    pid=project['id'];base='/api/v1/projects/'+pid;wid=install(client,base,'rolling-forecast')
    upload=client.post(base+'/materials',files={'file':('history.csv',template.example_files()['history.csv'].encode(),'text/csv')}).json()
    reached=threading.Event();calls=0;original=service.run_block
    async def pause_second(*args,**kwargs):
        nonlocal calls
        if args[1]=='data_analysis':
            calls+=1
            if calls==2:
                reached.set();await asyncio.Event().wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(service,'run_block',pause_second)
    task=start(client,base,'stop-origin',workflow_id=wid,inputs={**template.example_defaults(),'source_path':upload['path'],'origins':2})
    assert reached.wait(90)
    studies=client.get(base+'/modeling/studies').json();assert len(studies)==1
    stopped=client.post(base+'/tasks/'+task['id']+'/stop');assert stopped.status_code==200,stopped.text
    old=client.get(base+'/tasks/'+task['id']).json();time.sleep(.15)
    assert client.get(base+'/tasks/'+task['id']).json()['status']==old['status']=='interrupted'
    # Resuming follows the saved input snapshot despite an external source edit.
    (settings.workspace_root/pid/upload['path']).write_text('invalid changed source')
    response=client.post(base+'/tasks/'+task['id']+'/resume',json={'message':'继续原运行'})
    assert response.status_code==202,response.text
    final=wait_task(client,base,task,seconds=240)
    assert final['status']=='succeeded',final.get('error')
    final_studies=client.get(base+'/modeling/studies').json()
    assert len(final_studies)==2 and all(s['trials_used']==1 for s in final_studies)
    assert studies[0]['id'] in {r['model_version']['study_id'] for r in final['outputs']['result']['origins']}
