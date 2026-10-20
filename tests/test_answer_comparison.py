import json
from pathlib import Path

import httpx
import pytest
from agent_platform import answer_comparison as recipe, answer_comparison_code as code
from agent_platform.model_connections import project_model_override,project_model_role
from tests.test_projects import configured,graph,node,edge,ref,start,settled  # noqa:F401
from tests.test_modeling import wait_task
from tests.test_example_projects import install


@pytest.fixture
def files(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);Path('requirement-package').mkdir()
    for name,text in recipe.example_files().items():Path('requirement-package',name).write_text(text)
    return {'question':'怎样使用这些数据？','source_path':'requirement-package/试用说明.txt'}


def answer(model='test',fingerprint='same',usage=None):
    return {'model':model,'input_sha256':fingerprint,'text':'原始回答\n```example\n不执行\n```','seconds':.25,
            'usage':usage or {'input_tokens':0,'output_tokens':0,'cost_usd':0,'field_support':{'input_tokens':'reported'}}}


def test_report_keeps_unknown_usage_and_raw_answers_without_an_extra_judge(files):
    prepared=code.prepare(files)
    out=code.finish({'prepared':prepared,'A':answer(),'B':answer('different','changed')})
    assert out['answered']==2 and out['same_input'] is False and out['same_model'] is False
    assert out['answers'][0]['metrics']['input_tokens']==0
    assert out['answers'][0]['metrics']['output_tokens'] is None and out['answers'][0]['metrics']['cost_usd'] is None
    assert '不能把差异只归因于模型' in out['markdown'] and '未报告' in out['markdown']
    assert Path(out['artifacts'][2]['file_path']).read_text()==answer()['text']
    original=Path(out['artifacts'][0]['file_path']).read_bytes()
    Path(files['source_path']).write_text('同名文件现在内容不同')
    replay=code.report({'source_result_path':out['source_result_path']})
    assert replay['source']==out['source'] and Path(out['artifacts'][0]['file_path']).read_bytes()==original
    assert all(Path(a['file_path']).is_file() for a in replay['artifacts'])
    changed=code.prepare(files);assert changed['sha256']!=prepared['sha256']
    Path(prepared['input_path']).write_text('{}')
    with pytest.raises(ValueError,match='输入记录已改变'):code.report({'source_result_path':out['source_result_path']})


def test_no_answers_and_same_model_are_distinguished_from_model_quality(files):
    prepared=code.prepare(files)
    none=code.finish({'prepared':prepared,'A':{'error':'未配置模型'},'B':{'error':'模型不可用'}})
    assert none['answered']==0 and none['failed']==2 and none['same_input'] is None
    assert '两条调用均未产生可用回答' in none['markdown']
    same=code.finish({'prepared':prepared,'A':answer(),'B':answer()})
    assert same['same_model'] and same['same_input'] and '同一个模型名称' in same['markdown']


def test_bad_or_oversized_input_fails_before_inference(files):
    with pytest.raises(ValueError,match='填写'):code.prepare({**files,'question':''})
    with pytest.raises(ValueError,match='当前项目'):code.prepare({**files,'source_path':'../private.txt'})
    Path(files['source_path']).write_text('x'*16001)
    with pytest.raises(ValueError,match='尚未调用模型'):code.prepare(files)
    p=Path('requirement-package/input.pdf');p.write_bytes(b'%PDF')
    with pytest.raises(ValueError,match='整理成文字'):code.prepare({**files,'source_path':str(p)})
    assert code.prepare({'question':'没有资料也可以讨论方法'})['prompt']


def mock_http(monkeypatch,protocol='openai',failed=None):
    requests=[]
    def respond(request):
        body=json.loads(request.content);requests.append(body)
        if body['model']==failed:return httpx.Response(404,json={'error':{'message':'unknown model'}})
        text='回答来自请求模型 '+body['model']
        if protocol=='anthropic':return httpx.Response(200,json={'content':[{'type':'text','text':text}],'stop_reason':'end_turn','usage':{'input_tokens':7,'output_tokens':0}})
        usage={'prompt_tokens':7,'completion_tokens':0,'prompt_tokens_details':{'cached_tokens':0}} if body['model']!='b-model' else {'prompt_tokens':5}
        return httpx.Response(200,json={'choices':[{'message':{'content':text},'finish_reason':'stop'}],'usage':usage})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(**{**kw,'transport':httpx.MockTransport(respond)}))
    return requests


@pytest.mark.parametrize('protocol',['openai','anthropic'])
@pytest.mark.parametrize('role',['main','vision'])
def test_explicit_node_model_reaches_same_connection_and_does_not_change_defaults(configured,monkeypatch,protocol,role):
    client,app,project,_=configured;pid=project['id'];base='/api/v1/projects/'+pid
    cfg={'provider':'api','protocol':protocol,'model':'project-default','base_url':'http://127.0.0.1:9001/v1','api_key':'test-only','runtime_enabled':True}
    client.put(base+('/agent-session' if role=='main' else '/vision-model'),json=cfg).raise_for_status()
    calls=mock_http(monkeypatch,protocol)
    graph(client,pid,[node('start','start'),node('chosen','llm',model='chosen-model',model_role=role,prompt='same'),
        node('default','llm',model_role=role,prompt='same'),node('end','end',outputs={'chosen':ref('chosen'),'default':ref('default')})],
        [edge('start','chosen'),edge('chosen','default'),edge('default','end')])
    task=settled(client,base,start(client,base,'explicit-model',workflow_id=pid))
    assert task['status']=='succeeded',task.get('error')
    assert [v['model'] for v in calls]==['chosen-model','project-default']
    assert task['outputs']['chosen']['model']=='chosen-model' and task['outputs']['default']['model']=='project-default'
    assert task['outputs']['chosen']['input_sha256']==task['outputs']['default']['input_sha256']
    assert task['outputs']['chosen']['seconds']>=0
    assert app.state.services.local_agents.connections.load(pid,role).model=='project-default'
    assert project_model_override.get() is None and project_model_role.get()=='main'


@pytest.mark.parametrize('missing_b',[False,True])
def test_platform_comparison_partial_failure_export_replay_and_changed_input(configured,monkeypatch,missing_b):
    client,_,_,_=configured;pid=install(client,'answer-comparison');base='/api/v1/projects/'+pid
    cfg={'provider':'api','model':'project-default','base_url':'http://127.0.0.1:9001/v1','api_key':'test-only','runtime_enabled':True}
    client.put(base+'/agent-session',json=cfg).raise_for_status();calls=mock_http(monkeypatch,failed='b-model' if missing_b else None)
    url='/api/v1/applications/'+pid+'/draft';draft=client.get(url).json();flow=draft['snapshot']['workflow']
    for n in flow['nodes']:
        if n['id'] in ['a','b']:n['config']['model']=n['id']+'-model'
    client.post(url,json={'expected_revision':draft['revision'],'idempotency_key':'different-models','op':'replace_workflow','data':{'workflow':flow}}).raise_for_status()
    first=wait_task(client,base,start(client,base,'first',workflow_id=pid),seconds=40)
    assert first['status']=='succeeded',first.get('error')
    result=first['outputs']['result'];assert result['answered']==(1 if missing_b else 2)
    assert [c['model'] for c in calls]==['a-model','b-model']
    assert calls[0]['messages']==calls[1]['messages'] and calls[0]['max_tokens']==2048
    if missing_b:assert 'HTTP 404' in result['answers'][1]['error']
    else:
        assert result['same_input'] is True and result['same_model'] is False
        assert result['answers'][0]['metrics']['output_tokens']==0
        assert result['answers'][0]['metrics']['cache_read_input_tokens']==0
        assert result['answers'][1]['metrics']['output_tokens'] is None
    for artifact in result['artifacts']:
        assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+artifact['file_path']).status_code==200
    client.put(base+'/agent-session',json={**cfg,'runtime_enabled':False}).raise_for_status()
    replay=wait_task(client,base,start(client,base,'replay',workflow_id=pid,inputs={'source_result_path':result['source_result_path']}),seconds=40)
    assert replay['status']=='succeeded' and len(calls)==2
    assert replay['outputs']['result']['answers']==result['answers']
    assert client.get(base+'/tasks/'+first['id']).json()['outputs']==first['outputs']
    client.put(base+'/agent-session',json=cfg).raise_for_status()
    changed=wait_task(client,base,start(client,base,'changed-question',workflow_id=pid,inputs={'question':'换一个问题，如何验证结果？'}),seconds=40)
    assert changed['status']=='succeeded' and len(calls)==4
    assert changed['outputs']['result']['prompt_sha256']!=result['prompt_sha256']
    assert project_model_override.get() is None
