from agent_platform.project_assistant_tools import assistant_tool_specs
from tests.test_projects import configured as configured, graph, node, edge, ref  # noqa: F401
from tests.test_users import platform as platform, signup, project  # noqa: F401


def call(client, pid, tool_name, **args):
    return client.post(f'/api/v1/projects/{pid}/assistant-tools', json={'name': tool_name, 'arguments': args})


def test_build_catalog_and_installed_calls_are_distinct(configured):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/'+pid
    specs = client.get(base+'/assistant-tools').json()['tools']
    assert specs == assistant_tool_specs()
    assert {'block_catalog','workflow_draft','project_calls'} <= {s['name'] for s in specs}
    assert not {'project_code','project_modeling'} & {s['name'] for s in specs}
    assert call(client,pid,'block_catalog').status_code == 200
    created = call(client,pid,'project_workflows',action='create',name='由助手搭建').json()
    wid=created['id']
    graph(client,wid,[node('start','start'),node('end','end',outputs={'answer':42})],[edge('start','end')])
    assert wid not in {w['id'] for w in call(client,pid,'project_calls').json()['capabilities']}
    assert call(client,pid,'project_calls',action='start',workflow_id=wid).status_code == 422
    assert call(client,pid,'project_workflows',action='classify',workflow_id=wid,purpose='business').status_code == 422
    assert client.put(base+'/space/workflows/'+wid+'/assistant-callable',json={'enabled':True}).status_code == 200
    result=call(client,pid,'project_calls',action='start',workflow_id=wid,request_key='one')
    assert result.status_code==200,result.text
    assert result.json()['status']=='succeeded',result.text
    assert result.json()['outputs']['answer']==42
    duplicate=call(client,pid,'project_calls',action='start',workflow_id=wid,request_key='one')
    assert duplicate.json()['id']==result.json()['id']
    assert call(client,pid,'project_code',code='print(1)').status_code==422
    client.put(base+'/space/workflows/'+wid+'/assistant-callable',json={'enabled':False}).raise_for_status()
    assert call(client,pid,'workflow_run',action='start',workflow_id=wid).status_code==422
    # Historical results remain readable after removing the invocation capability.
    assert call(client,pid,'project_calls',action='inspect',task_id=result.json()['id']).json()['status']=='succeeded'


def test_nested_calls_cannot_bypass_installation(configured):
    client, app, project, _=configured
    pid=project['id'];base='/api/v1/projects/'+pid
    child=client.post(base+'/members',json={'name':'child'}).json()['id']
    graph(client,child,[node('s','start'),node('e','end',outputs={'ok':True})],[edge('s','e')])
    graph(client,pid,[node('s','start'),node('call','tool',tool_name='workflow:'+child,input={}),node('e','end',outputs={'result':ref('call','output')})],[edge('s','call'),edge('call','e')])
    client.put(base+'/space/workflows/'+child+'/assistant-callable',json={'enabled':False}).raise_for_status()
    denied=call(client,pid,'project_calls',action='start',workflow_id=pid)
    assert denied.status_code==422 and '子工作流' in denied.text
    assert client.get(base+'/tasks').json()==[]
    foreign=client.post('/api/v1/projects',json={'name':'foreign'}).json()['id']
    assert call(client,pid,'project_calls',action='interface',workflow_id=foreign).status_code==422


def test_editing_draft_removes_assistant_call_access(configured):
    client, app, project, _=configured
    pid=project['id']
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()
    result=call(client,pid,'workflow_draft',operation={'op':'add_node','expected_revision':draft['revision'],'idempotency_key':'edit','data':{'node':node('extra','end',outputs={'ok':True})}})
    assert result.status_code==200,result.text
    assert call(client,pid,'project_calls',action='start',workflow_id=pid).status_code==422


def test_human_adds_task_agent_without_model_calls(configured, monkeypatch):
    client,app,project,_=configured
    pid=project['id'];base='/api/v1/projects/'+pid
    response=client.post(base+'/space/task-agents',json={'name':'标签解释助手','instructions':'只解释用户明确提供的标签定义，不猜测缺失值或未知类别，返回依据及待确认问题。'})
    assert response.status_code==201,response.text
    wid=response.json()['workflow_id']
    available=call(client,pid,'project_calls').json()['capabilities']
    entry=next(w for w in available if w['id']==wid)
    assert entry['kind']=='task_agent' and '不猜测缺失值' in entry['description']
    draft=client.get('/api/v1/applications/'+wid+'/draft').json()['snapshot']
    agent=next(iter(draft['agents'].values()))
    assert agent['tools']==[] and not agent['allow_subagents']
    assert app.state.services.tools.definitions_for(__import__('agent_platform.models', fromlist=['AgentSpec']).AgentSpec.model_validate(agent)) == []
    assert client.get(base+'/tasks').json()==[]
    from agent_platform.workflow_runtime import _NODE_EXECUTORS
    invoked=[]
    async def run_agent(runtime, run):
        invoked.append(run.config.agent_id)
        return {'text':'标签含义待确认'}
    monkeypatch.setitem(_NODE_EXECUTORS, 'claude_agent', run_agent)
    client.put(base+'/agent-session',json={'provider':'api','model':'test','base_url':'https://example.test/v1','api_key':'test','runtime_enabled':True}).raise_for_status()
    result=call(client,pid,'project_calls',action='start',workflow_id=wid,inputs={'task':'解释未知标签'})
    assert result.status_code==200 and result.json()['status']=='succeeded',result.text
    assert invoked==[agent['id']] and result.json()['outputs']['result']['text']=='标签含义待确认'


def test_only_admin_configures_assistant_and_codex_is_independent_of_block_flag(platform):
    client,app=platform
    _,owner=signup(client,'owner');pid=project(client,owner);base='/api/v1/projects/'+pid
    source={'task':'official','allowed':True,'generation':'inherit'}
    assert client.put(base+'/assistant-source',headers=owner,json=source).status_code==403
    assert client.put(base+'/agent-session',headers=owner,json={'provider':'api','model':'test'}).status_code==403
    assert client.put(base+'/generation-model',headers=owner,json={'provider':'api','model':'test'}).status_code==403
    result=client.put(base+'/assistant-source',headers={'Authorization':'Bearer admin-boot'},json=source)
    assert result.status_code==200,result.text
    assert not client.get(base,headers=owner).json()['agent_modules_enabled']


def test_api_assistant_uses_installed_call_interface(configured, monkeypatch):
    import json
    from tests.test_project_conversation import TestSession, configure_agent, agent_settled
    seen=[]
    class Session(TestSession):
        async def start(self, tools, instructions, thread_id=None):
            assert tools==assistant_tool_specs()
            return await super().start(tools,instructions,thread_id)
        async def turn(self,message,on_event,on_tool,**kwargs):
            catalog=await on_tool('block_catalog',{})
            assert any(b['type']=='data_analysis' for b in catalog)
            installed=await on_tool('project_calls',{'action':'list'})
            assert len(installed['capabilities'])==1
            wid=installed['capabilities'][0]['id']
            outcome=await on_tool('project_calls',{'action':'start','workflow_id':wid,'request_key':'api-contract'})
            seen.append(outcome)
            await on_event('item/completed',{'item':{'type':'agentMessage','text':json.dumps(outcome)}})
            return {'status':'completed'}
    client,app,project,settings,base=configure_agent(configured,monkeypatch,Session)
    graph(client,project['id'],[node('s','start'),node('e','end',outputs={'answer':42})],[edge('s','e')])
    client.post(base+'/conversation/messages',json={'message':'调用已有流程'}).raise_for_status()
    state=agent_settled(client,base)
    assert state['status']=='idle',state['error']
    assert seen[0]['status']=='succeeded' and seen[0]['outputs']['answer']==42


from tests.test_official_agent import official as official, FakeAgent, enable, chat, wait  # noqa: E402,F401


def test_codex_adapter_uses_same_installed_call_interface(official):
    client,app,service=official
    seen=[]
    class Session(FakeAgent):
        async def start(self,tools,instructions,thread_id=None):
            assert tools==assistant_tool_specs()
            return await super().start(tools,instructions,thread_id)
        async def turn(self,message,on_event,on_tool,**kwargs):
            installed=await on_tool('project_calls',{'action':'list'})
            wid=installed['capabilities'][0]['id']
            outcome=await on_tool('project_calls',{'action':'start','workflow_id':wid,'request_key':'codex-contract'})
            seen.append(outcome)
            return {'status':'completed'}
    service.client_factory=Session
    _,owner=signup(client,'Owner');pid=project(client,owner)
    admin={'Authorization':'Bearer admin-boot'}
    client.headers.update(admin)
    graph(client,pid,[node('s','start'),node('e','end',outputs={'answer':42})],[edge('s','e')])
    enable(client,pid)
    path=chat(client,pid,owner)
    client.post(path+'/messages',headers=owner,json={'message':'调用已有流程'}).raise_for_status()
    state=wait(client,path,owner)
    assert state['status']=='idle',state['error']
    assert seen[0]['status']=='succeeded' and seen[0]['outputs']['answer']==42


def test_failed_edit_and_replay_do_not_remove_human_selection(configured):
    client,app,project,_=configured
    pid=project['id'];base='/api/v1/projects/'+pid
    graph(client,pid,[node('s','start'),node('e','end',outputs={'ok':True})],[edge('s','e')])
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()
    op={'expected_revision':draft['revision'],'expected_content_hash':draft['content_hash'],'idempotency_key':'change','operations':[{'op':'add_node','data':{'node':node('extra','end',outputs={'ok':True})}}]}
    assert call(client,pid,'workflow_draft',batch={**op,'expected_revision':999}).status_code!=200
    assert any(w['id']==pid for w in call(client,pid,'project_calls').json()['capabilities'])
    assert call(client,pid,'workflow_draft',batch=op).status_code==200
    client.put(base+'/space/workflows/'+pid+'/assistant-callable',json={'enabled':True}).raise_for_status()
    assert call(client,pid,'workflow_draft',batch=op).status_code==200
    assert any(w['id']==pid for w in call(client,pid,'project_calls').json()['capabilities'])


def test_copy_retry_preserves_later_human_installation(configured):
    client,app,project,_=configured
    pid=project['id'];base='/api/v1/projects/'+pid
    graph(client,pid,[node('s','start'),node('e','end',outputs={'ok':True})],[edge('s','e')])
    revision=client.get('/api/v1/applications/'+pid+'/draft').json()['revision']
    args={'action':'copy','workflow_id':pid,'expected_revision':revision,'name':'可复用副本','request_key':'one-copy'}
    response=call(client,pid,'project_workflows',**args)
    assert response.status_code==200,response.text
    copied=response.json()
    assert not copied['assistant_callable']
    client.put(base+'/space/workflows/'+copied['id']+'/assistant-callable',json={'enabled':True}).raise_for_status()
    replay=call(client,pid,'project_workflows',**args)
    assert replay.status_code==200,replay.text
    assert replay.json()['id']==copied['id'] and replay.json()['assistant_callable']
    assert call(client,pid,'project_calls',action='start',workflow_id=copied['id']).json()['status']=='succeeded'
