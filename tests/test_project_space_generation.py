"""Shared project resources and private conversation-to-workflow creation."""
import json
from datetime import datetime
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
    uploaded_file = next(f for f in scene['files'] if f['path'] == path)
    assert abs(datetime.fromisoformat(uploaded_file['modified_at']).timestamp() - (settings.workspace_root / pid / path).stat().st_mtime) < 0.000001
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


def test_generation_reuses_observed_output_structure_without_values_or_file_history(configured, monkeypatch):
    from tests.test_projects import settled
    client, app, p, _ = configured
    pid = p['id']; base = '/api/v1/projects/' + pid
    output = {'summary': [{'group': 'private-customer-value', 'count': 2, 'sum': '30'}],
              'artifacts': [{'file_path': 'results/private-name.csv'}], 'empty': [], 'enabled': True}
    graph(client, pid, [node('s', 'start'), node('e', 'end', outputs=output)], [edge('s', 'e')])
    started = client.post(base + '/tasks', json={'request_key': 'existing', 'workflow_id': pid}).json()
    done = settled(client, base, started)
    assert done['status'] == 'succeeded'
    upload = client.post(base + '/materials', files={'file': ('data.csv', b'x\n1\n', 'text/csv')}).json()
    annotate = app.state.services.projects.store.annotate_files

    async def with_history(project_id, files):
        result = await annotate(project_id, files)
        for file in result:
            file['related_run'] = {'workflow_name': 'unrelated-history-name', 'details': 'history' * 1000}
        return result

    monkeypatch.setattr(app.state.services.projects.store, 'annotate_files', with_history)
    cid = client.post(base + '/conversations', json={}).json()['id']
    seen = []
    provider(monkeypatch, app.state.services, echo_graph(), seen)
    body = {'instruction': '复用参考流程', 'reference_workflow_ids': [pid], 'file_paths': [upload['path']]}
    response = client.post(base + f'/conversations/{cid}/workflow-generation', json=body)
    assert response.status_code == 200, response.text
    reference = seen[0]['reference_workflows'][0]
    observed = reference['observed_output']
    assert observed['run_id'] == done['runs'][0]['id']
    fields = observed['shape']['properties']
    assert fields['summary']['items']['properties'] == {
        'group': {'type': 'string'}, 'count': {'type': 'integer'}, 'sum': {'type': 'string'}}
    assert fields['empty']['items'] == {} and fields['enabled']['type'] == 'boolean'
    assert 'private-customer-value' not in json.dumps(observed)
    assert 'private-name.csv' not in json.dumps(observed)
    context = seen[0]['project_context']
    assert context['selected_files'][0]['path'] == upload['path']
    assert any(f['path'] == upload['path'] for f in context['files'])
    assert 'unrelated-history-name' not in json.dumps(context)
    # A changed draft must not be described by a previous version's output.
    graph(client, pid, [node('s', 'start'), node('e', 'end', outputs={'different': 1})], [edge('s', 'e')])
    again = client.post(base + f'/conversations/{cid}/workflow-generation', json=body)
    assert again.status_code == 200, again.text
    assert 'observed_output' not in seen[1]['reference_workflows'][0]
    assert [task['id'] for task in client.get(base + '/tasks').json()] == [done['id']]


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


def test_selected_file_name_is_bound_before_save_and_default_run(configured, monkeypatch):
    from tests.test_projects import settled
    client, app, p, _ = configured
    pid=p['id'];base='/api/v1/projects/'+pid
    cid=client.post(base+'/conversations',json={}).json()['id']
    upload=client.post(base+'/materials',files={'file':('data.csv',b'group,value\nA,7\n','text/csv')}).json()
    flow={'nodes':[node('s','start',inputs=[{'name':'source_path','type':'file','required':True,'default':'data.csv'},
                                        {'name':'note','type':'string','default':'data.csv'}]),
                   node('e','end',outputs={'selected_file':ref('s','source_path'),'note':ref('s','note')})],
          'edges':[edge('s','e')]}
    seen=[];provider(monkeypatch,app.state.services,flow,seen)
    created=client.post(base+f'/conversations/{cid}/workflow-generation',json={
        'instruction':'使用已选资料创建汇总流程','name':'可直接运行','file_paths':[upload['path']]})
    assert created.status_code==200,created.text
    wid=created.json()['workflow_id']
    fields=created.json()['draft']['snapshot']['workflow']['nodes'][0]['config']['inputs']
    assert fields[0]['default']==upload['path'] and fields[1]['default']=='data.csv'
    assert client.get(base+'/tasks').json()==[]
    response=client.post(base+'/tasks',json={'request_key':'use-default-file','mode':'workflow','workflow_id':wid,'inputs':{}})
    assert response.status_code==202,response.text
    task=settled(client,base,response.json())
    assert task['status']=='succeeded',task
    assert task['outputs']=={'selected_file':upload['path'],'note':'data.csv'}


def test_file_binding_handles_nested_and_table_inputs_without_guessing_or_editing_text():
    from agent_platform.generation_file_bindings import bind_selected_files
    from copy import deepcopy
    files=[{'path':'requirement-package/one/data.csv'},{'path':'requirement-package/two/data.csv'},
           {'path':'results/one/unique.csv'}]
    inner={'nodes':[node('s','start',inputs=[{'name':'document','type':'string','default':'unique.csv'},
        {'name':'ambiguous','type':'file','default':'data.csv'},{'name':'future','type':'file','default':''},
        {'name':'foreign','type':'file','default':'requirement-package/elsewhere/unique.csv'},
        {'name':'sources','type':'array','columns':[{'name':'path','type':'file'},{'name':'note','type':'string'}],
         'default':[{'path':'unique.csv','note':'unique.csv'}]}]),
        node('python','code',code='data.csv',inputs={'plain_text':'unique.csv'})],'edges':[]}
    flow={'nodes':[node('loop','loop',workflow=inner)],'edges':[]}
    before=deepcopy(flow)
    bound=bind_selected_files(flow,files)
    fields=bound['nodes'][0]['config']['workflow']['nodes'][0]['config']['inputs']
    assert fields[0]['default']=='results/one/unique.csv'
    assert fields[1]['default']=='data.csv' and fields[2]['default']==''
    assert fields[3]['default']=='requirement-package/elsewhere/unique.csv'
    assert fields[4]['default']==[{'path':'results/one/unique.csv','note':'unique.csv'}]
    assert bound['nodes'][0]['config']['workflow']['nodes'][1]==inner['nodes'][1]
    assert flow==before
