"""Examples are private editable projects, not precomputed demonstration runs."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest
from tests.test_projects import configured, start, settled  # noqa: F401
from test_users import platform, signup  # noqa: F401
from agent_platform.example_catalog import catalog
from agent_platform import example_processing as processing


def install(client, key, headers=None, request_key=None):
    response=client.post('/api/v1/example-projects/'+key+'/instantiate',headers=headers,
                         json={'request_key':request_key or 'example-'+key})
    assert response.status_code==201,response.text
    return response.json()['project_id']


def test_all_examples_install_as_complete_editable_projects(configured):
    client,app,_,settings=configured
    items=client.get('/api/v1/example-projects').json()
    assert len(items)==25
    assert [v['id'] for v in items[:4]]==['meeting','weekly','expenses','profile']
    for item in items:
        pid=install(client,item['id'])
        base='/api/v1/projects/'+pid
        guide=client.get(base+'/example').json()
        assert guide['question'] and guide['exercise'] and guide['manual_path']
        for file in guide['files']:
            assert (settings.workspace_root/pid/file['path']).is_file()
        for flow in guide['workflows']:
            draft=client.get('/api/v1/applications/'+flow['id']+'/draft').json()
            assert draft['snapshot']['workflow']['nodes']
            assert '@file:' not in json.dumps(draft)
            assert '@workflow:' not in json.dumps(draft)
        assert client.get(base+'/tasks').json()==[]
        assert client.get(base+'/skills/example-guide').status_code==200
        assert install(client,item['id'])==pid
    assert not app.state.services.local_agents.tasks


def test_employees_get_private_copies_and_retry_is_idempotent(platform):
    client,_=platform
    _,a=signup(client,'示例甲');_,b=signup(client,'示例乙')
    assert client.get('/api/v1/example-projects').status_code==401
    assert len(client.get('/api/v1/example-projects',headers=a).json())==25
    pa=install(client,'expenses',a,'same');pb=install(client,'expenses',b,'same')
    assert pa!=pb
    assert client.get('/api/v1/projects/'+pa+'/example',headers=b).status_code==404
    guide=client.get('/api/v1/projects/'+pa+'/example',headers=a).json()
    assert client.get('/api/v1/applications/'+pa+'/workspace/files/'+guide['files'][0]['path'],headers=b).status_code==404
    assert client.post('/api/v1/example-projects/meeting/instantiate',headers=a,json={'request_key':'same'}).status_code==409
    assert client.get('/api/v1/example-projects/missing',headers=a).status_code==404


def test_creation_failure_rolls_back_and_same_request_can_retry(configured,monkeypatch):
    from agent_platform import example_projects
    client,app,_,settings=configured
    before=client.get('/api/v1/projects').json()
    folders=set(settings.workspace_root.iterdir())
    real=example_projects.save_skill
    async def fail(*args,**kwargs):raise ValueError('模拟说明保存失败')
    monkeypatch.setattr(example_projects,'save_skill',fail)
    result=client.post('/api/v1/example-projects/knowledge/instantiate',json={'request_key':'retry'})
    assert result.status_code==422,result.text
    assert client.get('/api/v1/projects').json()==before
    assert {p for p in settings.workspace_root.iterdir() if not p.name.startswith('.')}=={p for p in folders if not p.name.startswith('.')}
    monkeypatch.setattr(example_projects,'save_skill',real)
    install(client,'knowledge',request_key='retry')


def test_concurrent_create_produces_one_project(configured):
    client,_,_,_=configured
    def create(_):return client.post('/api/v1/example-projects/profile/instantiate',json={'request_key':'concurrent'})
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(create,range(2)))
    assert all(r.status_code in (201,409) for r in results)
    pid=install(client,'profile',request_key='concurrent')
    assert sum(p['id']==pid for p in client.get('/api/v1/projects').json())==1


@pytest.mark.parametrize('key',['expenses','diff','profile','join','summary','composition'])
def test_deterministic_examples_execute_through_platform_and_download(configured,key):
    client,_,_,settings=configured
    pid=install(client,key)
    base='/api/v1/projects/'+pid
    task=settled(client,base,start(client,base,'run-example',mode='workflow',workflow_id=pid,inputs={}))
    assert task['status']=='succeeded',task
    assert task['outputs']
    files=list((settings.workspace_root/pid/'results/examples').rglob('report.md'))
    assert files
    response=client.get('/api/v1/applications/'+pid+'/workspace/files/'+files[0].relative_to(settings.workspace_root/pid).as_posix())
    assert response.status_code==200,response.text


@pytest.fixture
def sample_files(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    root=tmp_path/'requirement-package';root.mkdir()
    def write(name,text):
        path=root/name;path.write_text(text,encoding='utf-8');return 'requirement-package/'+name
    return write


def test_expenses_exact_arithmetic_currencies_and_duplicates(sample_files):
    item=next(x for x in catalog() if x['id']=='expenses')
    files={k:sample_files(k,v) for k,v in item['files'].items()}
    result=processing.main({'operation':'expenses','source_path':files['expenses.csv'],'second_path':files['expenses-2.csv']})
    assert result['suspected_duplicates']==1
    assert next(r for r in result['summary'] if r['currency']=='USD')['amount']=='5'
    assert next(r for r in result['summary'] if r['category']=='餐饮')['amount']=='71.40'
    first=result['artifacts'][0]['file_path']
    again=processing.main({'operation':'expenses','source_path':files['expenses-2.csv']})
    assert again['rows']==2 and again['suspected_duplicates']==0
    assert Path(first).exists() and first!=again['artifacts'][0]['file_path']


def test_failure_cases_and_local_repair(sample_files):
    left=sample_files('left.csv','id,value\na,1\nb,2\n')
    right=sample_files('right.csv','id,label\na,x\na,y\n')
    args={'operation':'join','source_path':left,'second_path':right,'key':'id'}
    with pytest.raises(ValueError,match='重复'):processing.main(args)
    sample_files('right.csv','id,label\na,x\n')
    assert processing.main(args)['unmatched']==1
    bad=sample_files('bad.csv','date,category,amount,currency,merchant\n2026-13-01,交通,NaN,CNY,示例\n')
    with pytest.raises(ValueError,match='日期'):processing.main({'operation':'expenses','source_path':bad})
    sample_files('bad.csv','date,category,amount,currency,merchant\n2026-09-01,交通,NaN,CNY,示例\n')
    with pytest.raises(ValueError,match='有效数字'):processing.main({'operation':'expenses','source_path':bad})
    with pytest.raises(ValueError,match='当前项目'):processing.document('../secret.txt')


def test_document_sources_and_comparison_ignore_filename(sample_files):
    first=sample_files('first.txt','每500毫秒更新。\n不自动停机。')
    second=sample_files('second.txt','每1000毫秒更新。\n不自动停机。')
    assert 'first.txt · 第 1 行' in processing.document(first)
    result=processing.main({'operation':'diff','source_path':first,'second_path':second})
    assert len(result['changes'])==1
    assert result['changes'][0]['before']=='每500毫秒更新。'


@pytest.mark.parametrize('key',['meeting','email','weekly','writing','learning','planning','extraction'])
def test_model_examples_read_actual_sources_and_save_response_with_transport_double(configured,monkeypatch,key):
    import httpx
    client,_,_,settings=configured
    pid=install(client,key);base='/api/v1/projects/'+pid
    client.put(base+'/agent-session',json={'provider':'api','base_url':'http://127.0.0.1:9001/v1','model':'example-test-model','api_key':'test-only','runtime_enabled':True}).raise_for_status()
    calls=[]
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'# 测试替身结果\n待确认。 [input.txt · 第 1 行]'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':12}})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**{**kwargs,'transport':httpx.MockTransport(respond)}))
    task=settled(client,base,start(client,base,'llm-example',workflow_id=pid))
    assert task['status']=='succeeded',task
    assert len(calls)==1 and 'input.txt · 第 1 行' in calls[0]['messages'][-1]['content']
    assert task['outputs']['markdown'].startswith('# 测试替身')
    artifact=task['outputs']['result']['artifacts'][0]['file_path']
    assert (settings.workspace_root/pid/artifact).read_text()==task['outputs']['markdown']
    assert task['outputs']['model_usage']['input_tokens']==10
    guide=client.get(base+'/example').json()
    alternate=next(f['path'] for f in guide['files'] if f['name']=='input-2.txt')
    changed=settled(client,base,start(client,base,'llm-changed',workflow_id=pid,inputs={'source_path':alternate}))
    assert changed['status']=='succeeded',changed
    assert len(calls)==2 and 'input-2.txt · 第 1 行' in calls[-1]['messages'][-1]['content']
    assert 'input.txt · 第 1 行' not in calls[-1]['messages'][-1]['content']
    assert client.get(base+'/tasks/'+task['id']).json()['outputs']==task['outputs']


def test_knowledge_example_requires_index_then_returns_citations(configured,monkeypatch):
    import httpx
    client,app,_,_=configured
    pid=install(client,'knowledge');base='/api/v1/projects/'+pid
    missing=settled(client,base,start(client,base,'no-index',workflow_id=pid))
    assert missing['status']=='failed' and '索引' in missing['error']
    async def embeddings(connection,texts):return [[1.,0.] for t in texts]
    monkeypatch.setattr(app.state.services.projects.knowledge,'embeddings',embeddings)
    connection={'provider':'api','base_url':'http://127.0.0.1:9001/v1','model':'test','api_key':'test-only','runtime_enabled':True}
    client.put(base+'/embedding-model',json=connection).raise_for_status()
    client.put(base+'/agent-session',json=connection).raise_for_status()
    resource=client.get(base+'/knowledge/example-knowledge').json()
    client.post(base+'/knowledge/example-knowledge/build',json={'expected_revision':resource['revision']}).raise_for_status()
    original=httpx.AsyncClient
    def respond(request):return httpx.Response(200,json={'choices':[{'message':{'content':'登记设备编号、借用人和预计归还日期。[1]'},'finish_reason':'stop'}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**{**kwargs,'transport':httpx.MockTransport(respond)}))
    result=settled(client,base,start(client,base,'with-index',workflow_id=pid))
    assert result['status']=='succeeded',result
    assert result['outputs']['knowledge']['results'][0]['citation']=='[1]'


@pytest.mark.parametrize('key',['classification','regression','group-training','process','prediction','rules'])
def test_ml_examples_with_real_docker_compute(configured,key):
    import os
    if os.environ.get('MODEL_TEST_DOCKER')!='1':pytest.skip('Opt in to the installed Docker modeling environment')
    from tests.test_modeling import wait_task
    client,app,_,_=configured
    pid=install(client,key);base='/api/v1/projects/'+pid
    first=wait_task(client,base,start(client,base,'real-example',workflow_id=pid))
    assert first['status']=='succeeded',first.get('error')
    assert first['outputs']['test']['rows']>0
    trials=first['outputs']['training']['trials']
    assert any(t['status']=='completed' for t in trials)
    if key not in ('prediction','rules'):
        guide=client.get(base+'/example').json()
        defaults=next(n for n in client.get('/api/v1/applications/'+pid+'/draft').json()['snapshot']['workflow']['nodes'] if n['type']=='start')['config']['inputs']
        inputs={}
        for field in defaults:
            if field['name'] in ('source_path','labels_path'):
                old=next(f for f in guide['files'] if f['path']==field['default'])
                inputs[field['name']]=next(f['path'] for f in guide['files'] if f['name']==old['name'].replace('.csv','-2.csv'))
        changed=wait_task(client,base,start(client,base,'changed-data',workflow_id=pid,inputs=inputs))
        assert changed['status']=='succeeded',changed.get('error')
        assert changed['outputs']['training']['id']!=first['outputs']['training']['id']
        assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
        return
    guide=client.get(base+'/example').json()
    wid=guide['workflows'][1]['id']
    missing=wait_task(client,base,start(client,base,'missing-model',workflow_id=wid))
    assert missing['status']=='failed' and '绑定' in missing['error']
    candidate=first['outputs']['training'];trial=next(t for t in trials if t['status']=='completed')
    client.put(base+'/models/example-model',json={'name':'示例模型','study_id':candidate['study_id'],'candidate_id':candidate['id'],'slot':trial['slot'],'expected_revision':1}).raise_for_status()
    predicted=wait_task(client,base,start(client,base,'bound-model',workflow_id=wid))
    assert predicted['status']=='succeeded',predicted.get('error')
    assert predicted['outputs']['result']['rows']==20
    another=next(t for t in trials if t['status']=='completed' and t['slot']!=trial['slot'])
    client.put(base+'/models/example-model',json={'name':'新模型版本','study_id':candidate['study_id'],'candidate_id':candidate['id'],'slot':another['slot'],'expected_revision':2}).raise_for_status()
    alternate=next(f['path'] for f in guide['files'] if f['name']=='new-data-2.csv')
    rebound=wait_task(client,base,start(client,base,'rebound-model',workflow_id=wid,inputs={'source_path':alternate}))
    assert rebound['status']=='succeeded',rebound.get('error')
    assert rebound['outputs']['result']['model_version']!=predicted['outputs']['result']['model_version']
    assert client.get(base+'/tasks/'+predicted['id']).json()['outputs']==predicted['outputs']
    if key=='rules':
        replay=wait_task(client,base,start(client,base,'replay',workflow_id=guide['workflows'][2]['id'],inputs={'source_path':predicted['outputs']['result']['prediction_input'],'threshold':0.95}))
        assert replay['status']=='succeeded',replay.get('error')
        assert replay['outputs']['result']['model_version']==predicted['outputs']['result']['model_version']


def test_editing_example_changes_new_run_and_keeps_history(configured):
    client,_,_,_=configured
    pid=install(client,'profile');base='/api/v1/projects/'+pid
    first=settled(client,base,start(client,base,'first',workflow_id=pid))
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()
    workflow=draft['snapshot']['workflow']
    workflow['nodes'][-1]['config']['outputs']['note']='员工修改后的报告'
    saved=client.put(base+'/workflows/'+pid+'/draft',json={'expected_revision':draft['revision'],'workflow':workflow})
    assert saved.status_code==200,saved.text
    guide=client.get(base+'/example').json()
    alternate=next(f['path'] for f in guide['files'] if f['name']=='data-2.csv')
    second=settled(client,base,start(client,base,'second',workflow_id=pid,inputs={'source_path':alternate}))
    assert second['status']=='succeeded',second
    assert second['outputs']['note']=='员工修改后的报告'
    assert second['outputs']['result']['duplicates']==1
    assert second['outputs']['result']['missing']['value']==1
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
