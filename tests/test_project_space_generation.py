"""Shared project resources and private conversation-to-workflow creation."""
import json
from types import SimpleNamespace

from agent_platform.conversation_scope import conversation_scope
from agent_platform.connected_model import completion_events
from tests.test_projects import configured, graph, node, edge, ref  # noqa: F401
from tests.test_users import platform, signup, project  # noqa: F401


def echo_graph():
    return {'nodes': [node('s', 'start', inputs=[{'name': 'file', 'type': 'string', 'required': True}]),
                      node('e', 'end', outputs={'selected_file': ref('$inputs', 'file')})], 'edges': [edge('s', 'e')]}


def provider(monkeypatch, services, workflow, seen):
    async def stream(**kwargs):
        seen.append(json.loads(kwargs['messages'][0].content[0].text))
        assert kwargs['tools'] == []
        for event in completion_events([{'type': 'text', 'text': json.dumps({'workflow': workflow})}], stop_reason='end_turn'):
            yield event
    monkeypatch.setattr(services.local_agents.connections, 'provider', lambda *a, **k: SimpleNamespace(stream=stream))


def test_added_workflow_and_uploaded_file_are_discoverable_and_callable(configured):
    client, app, p, settings = configured
    pid=p['id']; base='/api/v1/projects/'+pid
    uploaded=client.post(base+'/materials',files={'file':('measurements.csv',b'x,y\n1,2\n','text/csv')})
    assert uploaded.status_code==201,uploaded.text
    path=uploaded.json()['path']
    response=client.post(base+'/space/workflows',json={'name':'共享数据流程','workflow':echo_graph()})
    assert response.status_code==201,response.text
    wid=response.json()['workflow_id']
    scene=client.get(base+'/space').json()
    assert any(w['id']==wid and w['inputs'][0]['name']=='file' for w in scene['workflows'])
    assert any(f['path']==path for f in scene['files'])
    tools=base+'/agent-tools'
    inspected=client.post(tools,json={'name':'project_workflows','arguments':{'action':'inspect','workflow_id':wid}})
    assert inspected.status_code==200
    read=client.post(tools,json={'name':'project_file','arguments':{'action':'read','path':path}})
    assert read.status_code==200 and 'x,y' in read.text
    run=client.post(tools,json={'name':'workflow_run','arguments':{'action':'start','workflow_id':wid,'inputs':{'file':path}}})
    assert run.status_code==200,run.text
    assert run.json()['status']=='succeeded' and run.json()['outputs']['selected_file']==path
    other=client.post('/api/v1/projects',json={'name':'另一个空间'}).json()['id']
    assert not client.get('/api/v1/projects/'+other+'/space').json()['files']
    denied=client.post('/api/v1/projects/'+other+'/agent-tools',json={'name':'workflow_run','arguments':{'action':'start','workflow_id':wid,'inputs':{'file':path}}})
    assert denied.status_code in {404,422}


def test_agent_discovers_other_space_workflows_and_file_names_during_an_active_item(configured, monkeypatch):
    from tests.test_project_conversation import TestSession, configure_agent, put_progress, item, agent_settled
    seen = []
    class Capturing(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            seen.append(json.loads(message))
            return {'status': 'completed'}
    client, app, p, _, base = configure_agent(configured, monkeypatch, Capturing)
    put_progress(client, base, [item(workflow_ids=[p['id']])])
    added = client.post(base+'/space/workflows', json={'name': '可复用流程', 'workflow': echo_graph()}).json()['workflow_id']
    uploaded = client.post(base+'/materials', files={'file': ('sample.csv', b'private-file-body', 'text/csv')}).json()
    response = client.post(base+'/conversation/messages', json={'message': '看看空间中可以调用什么', 'item_id': 'allocate'})
    assert response.status_code == 202, response.text
    state = agent_settled(client, base)
    assert state['status'] == 'idle', state['error']
    assert any(w['id'] == added for w in seen[0]['workflows'])
    assert any(f['path'] == uploaded['path'] for f in seen[0]['project_files']['files'])
    assert 'private-file-body' not in json.dumps(seen)


def test_generation_uses_own_chat_and_references_without_changing_or_running_them(configured,monkeypatch):
    client,app,p,settings=configured;pid=p['id'];base='/api/v1/projects/'+pid;services=app.state.services
    graph(client,pid,**echo_graph())
    before=client.get('/api/v1/applications/'+pid+'/draft').json()
    chats=[client.post(base+'/conversations',json={'title':title}).json()['id'] for title in ('分析','其他会话')]
    for cid,text in zip(chats,('本会话的分组要求','其他会话秘密')):
        with conversation_scope(pid,cid): services.local_agents.event(pid,'user',text)
    upload=client.post(base+'/materials',files={'file':('data.csv',b'x,y\n1,2\n','text/csv')}).json()
    result_graph={'nodes':[node('s','start'),node('p','model_predict',model_ref='later',dataset_id=''),node('e','end')], 'edges':[edge('s','p'),edge('p','e')]}
    seen=[];provider(monkeypatch,services,result_graph,seen)
    response=client.post(base+f'/conversations/{chats[0]}/workflow-generation',json={'instruction':'依据分析创建预测工作流，模型稍后绑定','name':'新预测','reference_workflow_ids':[pid],'file_paths':[upload['path']]})
    assert response.status_code==200,response.text
    created=response.json();assert created['workflow_id']!=pid and created['model_calls']==1
    assert len(seen)==1 and seen[0]['reference_workflows'][0]['workflow']==before['snapshot']['workflow']
    assert seen[0]['project_context']['selected_files'][0]['path']==upload['path']
    assert '本会话的分组要求' in json.dumps(seen,ensure_ascii=False)
    assert '其他会话秘密' not in json.dumps(seen,ensure_ascii=False)
    assert client.get('/api/v1/applications/'+pid+'/draft').json()==before
    assert client.get(base+'/tasks').json()==[]
    own=client.get(base+'/conversations/'+chats[0]).json()['events']
    assert any(e.get('workflow',{}).get('id')==created['workflow_id'] for e in own)
    assert not any('workflow' in e for e in client.get(base+'/conversations/'+chats[1]).json()['events'])
    assert services.local_agents.load(pid)['events']==[]
    assert any(w['id']==created['workflow_id'] for w in client.get(base+'/space').json()['workflows'])


def test_foreign_references_files_and_forbidden_blocks_fail_before_spending(configured,monkeypatch):
    client,app,p,_=configured;pid=p['id'];base='/api/v1/projects/'+pid
    cid=client.post(base+'/conversations',json={}).json()['id']
    foreign=client.post('/api/v1/projects',json={'name':'其他项目'}).json()['id']
    seen=[];provider(monkeypatch,app.state.services,echo_graph(),seen)
    url=base+f'/conversations/{cid}/workflow-generation'
    for fields in [{'reference_workflow_ids':[foreign]},{'file_paths':['../secret']},{'dataset_id':'foreign'}]:
        assert client.post(url,json={'instruction':'创建新流程',**fields}).status_code in {404,422}
    assert seen==[]
    initial=len(client.get(base+'/members').json())
    for workflow in [ {'nodes':[node('s','subagent_spawn')],'edges':[]},
                      {'nodes':[node('s','start'),node('call','tool',tool_name='workflow:'+foreign,input={}),node('e','end')],'edges':[edge('s','call'),edge('call','e')]} ]:
        response=client.post(base+'/space/workflows',json={'name':'不应加入','workflow':workflow})
        assert response.status_code==422,response.text
    assert len(client.get(base+'/members').json())==initial


def test_space_is_shared_but_generation_cannot_read_another_members_conversation(platform,monkeypatch):
    client,app=platform;_,alice=signup(client,'Alice');_,bob=signup(client,'Bob');pid=project(client,alice)
    base='/api/v1/projects/'+pid
    assert client.get(base+'/space',headers=bob).status_code==404
    client.post(base+'/access-members',headers=alice,json={'name':'Bob'})
    cid=client.post(base+'/conversations',headers=alice,json={}).json()['id']
    assert client.get(base+'/space',headers=bob).status_code==200
    seen=[];provider(monkeypatch,app.state.services,echo_graph(),seen)
    response=client.post(base+f'/conversations/{cid}/workflow-generation',headers=bob,json={'instruction':'创建流程'})
    assert response.status_code==404 and seen==[]
