"""Examples are private editable projects, not precomputed demonstration runs."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import csv
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
    assert len(items)==35
    states = [v['readiness']['status'] == 'configured' for v in items]
    assert states == sorted(states, reverse=True)
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


@pytest.mark.parametrize('key,fields,absent', [
    ('rolling-forecast', ['series', 'time', 'available', 'value'], ['temperature', 'pressure', 'material', 'batch/furnace']),
    ('prediction-feedback', ['sample_id', 'prediction', 'actual', 'baseline', 'batch'], ['temperature', 'furnace']),
    ('interval-trends', ['series', 'period', 'value'], ['temperature', 'pressure', 'furnace']),
    ('classification', ['temperature', 'pressure', 'material', 'target', 'batch'], ['furnace']),
    ('regression', ['temperature', 'pressure', 'material', 'target', 'batch'], ['furnace']),
    ('process', ['furnace', 'time', 'temperature', 'pressure', 'prediction_time', 'target'], ['batch']),
])
def test_example_manual_and_skill_describe_its_actual_fields(configured, key, fields, absent):
    client, _, _, settings = configured
    pid = install(client, key); base = '/api/v1/projects/'+pid
    guide = client.get(base+'/example').json()
    manual = (settings.workspace_root/pid/guide['manual_path']).read_text()
    skill = client.get(base+'/skills/example-guide').json()['content']
    assert manual == guide['current_manual']
    assert guide['field_notes'] in manual and manual in skill
    for name in fields:
        assert f'`{name}`' in guide['field_notes']
    for name in absent:
        assert name not in guide['field_notes']
    for file in guide['files']:
        assert file['path'] in manual
    for workflow in guide['workflows']:
        assert workflow['id'] in manual


def test_existing_forecast_get_corrects_guidance_without_rewriting_project(configured):
    client, app, _, settings = configured
    pid = install(client, 'rolling-forecast'); base = '/api/v1/projects/'+pid
    store = app.state.services.projects.store
    saved = asyncio.run(store.get_record(pid, 'example', 'guide'))
    legacy = '温度 temperature 与压力 pressure 为合成连续特征，material 为类别特征；target 是生成的标签，batch/furnace 是隔离分组。\n员工备注：实际使用部门的上传数据。'
    uploaded = client.post(base+'/materials', files={'file':('部门说明.md', legacy.encode(), 'text/markdown')}).json()
    # Existing projects may point at a manually edited document of any name.
    value = {**saved['value'], 'manual_path': uploaded['path']}
    asyncio.run(store.put_record(pid, 'example', 'guide', value, saved['revision']))
    skill = client.get(base+'/skills/example-guide').json()
    client.put(base+'/skills/example-guide', json={
        'name': skill['name'], 'content': legacy, 'expected_revision': skill['revision'],
    }).raise_for_status()
    task = settled(client, base, start(client, base, 'historical-invalid', workflow_id=pid, inputs={'horizon': 0}))
    assert task['status'] == 'failed'
    before_record = asyncio.run(store.get_record(pid, 'example', 'guide'))
    before_skill = client.get(base+'/skills/example-guide').json()
    before_draft = client.get('/api/v1/applications/'+pid+'/draft').json()
    before_files = {p.relative_to(settings.workspace_root/pid): p.read_bytes()
                    for p in (settings.workspace_root/pid/'requirement-package').rglob('*') if p.is_file()}
    for _ in range(2):
        guide = client.get(base+'/example').json()
        assert guide['manual_path'] == uploaded['path']
        assert 'available' in guide['field_notes'] and '当时已可用' in guide['field_notes']
        assert 'temperature' not in guide['current_manual']
        assert 'pressure' not in guide['current_manual']
        for file in guide['files']:
            assert file['path'] in guide['current_manual']
        assert pid in guide['current_manual']
    assert asyncio.run(store.get_record(pid, 'example', 'guide')) == before_record
    assert client.get(base+'/skills/example-guide').json() == before_skill
    assert client.get('/api/v1/applications/'+pid+'/draft').json() == before_draft
    assert client.get(base+'/tasks/'+task['id']).json() == task
    assert {p.relative_to(settings.workspace_root/pid): p.read_bytes()
            for p in (settings.workspace_root/pid/'requirement-package').rglob('*') if p.is_file()} == before_files


def test_examples_explain_missing_resources_before_running_and_refresh_configuration(configured, monkeypatch):
    client, app, _, _ = configured
    services = app.state.services
    from agent_platform import workflow_readiness
    async def available(*args):
        return True
    monkeypatch.setattr(workflow_readiness, 'environment_ready', available)
    items = client.get('/api/v1/example-projects').json()
    assert items[0]['id'] == 'expenses'
    meeting = next(item for item in items if item['id'] == 'meeting')
    assert {'model:main','egress'} <= {v['code'] for v in meeting['readiness']['issues']}
    pid = install(client, 'meeting')
    base = '/api/v1/projects/'+pid
    check = base+'/space/workflows/'+pid+'/readiness'
    assert client.get(check).json()['status'] == 'needs_setup'
    # Saving and creation are allowed; the read-only check starts no tasks.
    assert client.get(base+'/tasks').json() == []
    client.put(base+'/agent-session', json={'provider':'api','base_url':'http://127.0.0.1:9001/v1',
        'model':'offline-double','api_key':'test-only','runtime_enabled':True}).raise_for_status()
    # Existing runtime permits loopback API endpoints while external egress is off.
    assert client.get(check).json()['status'] == 'configured'
    # This test changes only a settings flag; there is no provider request.
    services.settings.model_egress_enabled = True
    assert client.get(check).json()['status'] == 'configured'
    assert client.get(base+'/example').json()['readiness']['status'] == 'configured'
    other = install(client, 'expenses')
    assert client.get(base+'/space/workflows/'+other+'/readiness').status_code == 422
    assert client.get(base+'/tasks').json() == []


def test_expense_duplicates_outside_preview_include_original_file_and_row(sample_files):
    header = 'date,category,amount,currency,merchant\n'
    first = sample_files('first.csv', header+''.join(f'2026-09-01,交通,{i},CNY,示例{i}\n' for i in range(25)))
    second = sample_files('second.csv', header+'2026-09-01,交通,24.00,CNY,示例24\n')
    result = processing.main({'operation':'expenses','source_path':first,'second_path':second})
    assert result['suspected_duplicates'] == 1
    assert not any(row['suspected_duplicate']=='yes' for row in result['preview'])
    assert result['duplicate_records'][0]['source_file'] == second
    assert result['duplicate_records'][0]['source_row'] == 2
    assert result['summary'][0]['amount'] == '324.00'
    assert '需要复核：1 条疑似重复费用' in result['markdown']


def test_employees_get_private_copies_and_retry_is_idempotent(platform):
    client,_=platform
    _,a=signup(client,'示例甲');_,b=signup(client,'示例乙')
    assert client.get('/api/v1/example-projects').status_code==401
    assert len(client.get('/api/v1/example-projects',headers=a).json())==35
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


@pytest.mark.parametrize('suffix,delimiter', [('csv', ','), ('tsv', '\t')])
def test_profile_duplicates_use_source_lines_and_agree_in_all_reports(sample_files, suffix, delimiter):
    lines = ['id,value', 'a,"line one\nline two"', '', 'a,"line one\nline two"',
             'b,3', 'a,"line one\nline two"', ',', ',', 'c,1', 'd,2']
    text = '\n'.join(lines).replace(',', delimiter) + '\n'
    path = sample_files('profile.' + suffix, text)
    before = Path(path).read_bytes()
    result = processing.main({'operation': 'profile', 'source_path': path})
    assert result['rows'] == 8
    assert result['duplicates'] == 3
    duplicates = [issue for issue in result['issues'] if issue['reason'] == '完全重复']
    assert [(issue['row'], issue['first_row']) for issue in duplicates] == [(5, 2), (8, 2), (11, 10)]
    assert all(issue['source_file'] == path and issue['sheet'] == '' for issue in result['issues'])
    assert [issue['row'] for issue in result['issues'] if issue['reason'] == '缺失字段'] == [10, 11]
    assert '记录起始物理行' in result['markdown']
    assert path in result['markdown']
    assert '| 5 | 2 |' in result['markdown'] and '| 8 | 2 |' in result['markdown']
    folder = Path(result['artifacts'][0]['file_path']).parent
    assert json.loads((folder / 'result.json').read_text())['issues'] == result['issues']
    assert (folder / 'report.md').read_text() == result['markdown']
    with (folder / 'details.csv').open(encoding='utf-8-sig', newline='') as stream:
        downloaded = list(csv.DictReader(stream))
    assert downloaded == [{key: str(value) for key, value in issue.items()} for issue in result['issues']]
    assert Path(path).read_bytes() == before


def test_profile_excel_preserves_sheet_rows_and_empty_records(sample_files):
    from openpyxl import Workbook
    path = Path(sample_files('source.xlsx', ''))
    book = Workbook()
    book.active.append(['ignored'])
    sheet = book.create_sheet('检测数据')
    book.active = 1
    for row in [('id', 'value'), ('a', 'two\nlines'), (None, None), ('a', 'two\nlines'),
                (None, None), ('a', 'two\nlines')]:
        sheet.append(row)
    book.save(path)
    before = path.read_bytes()
    result = processing.main({'operation': 'profile', 'source_path': str(path)})
    assert result['rows'] == 5 and result['duplicates'] == 3
    duplicates = [issue for issue in result['issues'] if issue['reason'] == '完全重复']
    assert [(issue['row'], issue['first_row']) for issue in duplicates] == [(4, 2), (5, 3), (6, 2)]
    assert all(issue['sheet'] == '检测数据' for issue in result['issues'])
    assert '工作表：检测数据' in result['markdown'] and '工作表行号' in result['markdown']
    assert path.read_bytes() == before


def test_profile_keeps_duplicate_missing_and_outlier_rows_in_one_download(sample_files):
    path = sample_files('numbers.csv', 'id,value\na,1\na,1\nb,2\nc,3\nd,100\ne,\n')
    result = processing.main({'operation': 'profile', 'source_path': path})
    assert result['duplicates'] == 1
    assert {(issue['row'], issue['reason'], issue['first_row']) for issue in result['issues']} == {
        (3, '完全重复', 2), (7, '缺失字段', ''), (6, '数值超出四分位距范围（需人工判断）', '')}
    folder = Path(result['artifacts'][0]['file_path']).parent
    with (folder / 'details.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert len(list(csv.DictReader(stream))) == 3


def test_profile_keeps_all_duplicate_locations_when_report_preview_is_full(sample_files):
    path = sample_files('repeated.csv', 'id,value\n' + 'same,1\n' * 106)
    result = processing.main({'operation': 'profile', 'source_path': path})
    assert result['duplicates'] == 105
    assert len(result['issues']) == 105
    assert result['issues'][-1]['row'] == 107 and result['issues'][-1]['first_row'] == 2
    assert '当前显示前 100 条，共 105 条' in result['markdown']
    assert '| 102 | 2 |' in result['markdown'] and '| 103 | 2 |' not in result['markdown']
    folder = Path(result['artifacts'][0]['file_path']).parent
    assert len(json.loads((folder / 'result.json').read_text())['issues']) == 105
    with (folder / 'details.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert len(list(csv.DictReader(stream))) == 105


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
    duplicate=next(issue for issue in second['outputs']['result']['issues'] if issue['reason']=='完全重复')
    assert (duplicate['row'],duplicate['first_row'],duplicate['source_file'])==(3,2,alternate)
    assert '| 3 | 2 |' in second['outputs']['markdown']
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
