"""Tasks, editable drafts and model resources are independent public behaviors."""
import asyncio
import json
import time
from pathlib import Path
import pytest
from tests.test_projects import configured, graph, node, edge, ref, start, settled  # noqa: F401
from tests.test_modeling import modeling, real_compute, wait_task  # noqa: F401


def prediction_graph():
    return {'nodes':[node('start','start',inputs=[{'name':'dataset_id','type':'string','required':True}]),
        node('predict','model_predict',model_ref='quality',dataset_id=ref('$inputs','dataset_id')),
        node('end','end',outputs={'result':ref('predict','output')})], 'edges':[edge('start','predict'),edge('predict','end')]}


def test_unbound_draft_edit_undo_and_conflict(configured):
    client,app,p,settings=configured; pid=p['id'];base='/api/v1/projects/'+pid
    draft=client.get('/api/v1/applications/'+pid+'/draft').json()
    assert client.put(base+'/models/quality',json={'name':'质量预测'}).status_code==200
    result=client.put(base+f'/workflows/{pid}/draft',json={'expected_revision':draft['revision'],'workflow':prediction_graph()})
    assert result.status_code==200,result.text
    saved=result.json()
    task=settled(client,base,start(client,base,'missing',inputs={'dataset_id':'later'}))
    assert task['status']=='failed' and '尚未绑定' in task['error']
    assert client.put(base+f'/workflows/{pid}/draft',json={'expected_revision':draft['revision'],'workflow':{'nodes':[],'edges':[]}}).status_code==409
    undo=client.put(base+f'/workflows/{pid}/draft',json={'expected_revision':saved['draft']['revision'],'workflow':saved['previous_workflow']})
    assert undo.status_code==200,undo.text
    assert client.get(base+'/tasks/'+task['id']).json()['status']=='failed'


def test_project_skills_and_tools_without_confirmation(configured):
    client,app,p,settings=configured;pid=p['id'];base='/api/v1/projects/'+pid
    listing=client.get(base+'/skills').json();assert all('content' not in s for s in listing)
    result=client.put(base+'/skills/workflows',json={'name':'已有流程','description':'调用说明','content':'使用质量预测流程','references':{'fields.md':'字段含义'}})
    assert result.status_code==200,result.text
    read=client.post(base+'/agent-tools',json={'name':'project_skills','arguments':{'action':'read','skill_id':'workflows'}}).json()
    assert read['content']=='使用质量预测流程' and read['references']==['fields.md']
    assert client.post(base+'/agent-tools',json={'name':'project_skills','arguments':{'action':'read','skill_id':'workflows','reference':'fields.md'}}).json()['content']=='字段含义'
    created=client.post(base+'/agent-tools',json={'name':'project_workflows','arguments':{'action':'create','name':'无需阶段锁'}})
    assert created.status_code==200,created.text
    draft=client.get('/api/v1/applications/'+created.json()['id']+'/draft').json()
    result=client.post(base+'/agent-tools',json={'name':'workflow_draft','arguments':{'workflow_id':created.json()['id'],'operation':{'op':'replace_workflow','data':{'workflow':prediction_graph()},'expected_revision':draft['revision'],'idempotency_key':'direct'}}})
    assert result.status_code==200,result.text
    other=client.post('/api/v1/projects',json={'name':'另一个项目'}).json()['id']
    assert client.get('/api/v1/projects/'+other+'/skills/workflows').json()['content']!='使用质量预测流程'
    # A real conversation may enter operate when invoking an existing workflow.
    # It must still be able to save the analysis produced after that call.
    manager=app.state.services.local_agents
    state=manager.load(pid);state.update(conversation_enabled=True,phase='operate');manager.save(pid,state)
    written=client.post(base+'/agent-tools',json={'name':'project_file','arguments':{
        'action':'write','path':'results/analysis.md','content':'预测完成后的分析报告'}})
    assert written.status_code==200,written.text
    assert (settings.workspace_root/pid/'results/analysis.md').read_text()=='预测完成后的分析报告'
    direct=client.post(base+'/agent-tools',json={'name':'project_action','arguments':{
        'action':'trial','workflow_id':created.json()['id'],'inputs':{'dataset_id':'later'}}})
    assert direct.status_code==200,direct.text
    assert direct.json()['status']=='failed' and '尚未绑定' in direct.json()['error']
    assert client.get(base+'/progress').json()['value']['items']==[]


def test_generation_one_response_no_task_or_approval(configured,monkeypatch):
    from agent_platform.models import StreamEvent
    client,app,p,settings=configured;pid=p['id'];base='/api/v1/projects/'+pid
    calls=[]
    class Provider:
        async def stream(self,**kwargs):
            calls.append(kwargs)
            yield StreamEvent(type='content_block_start',data={'index':0,'content_block':{'type':'text','text':json.dumps({'workflow':prediction_graph()})}})
            yield StreamEvent(type='message_delta',data={'delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':100}})
    monkeypatch.setattr(app.state.services.local_agents.connections,'provider',lambda *a,**k:Provider())
    result=client.post(base+'/workflow-generation',json={'instruction':'预测结果导出','name':'质量预测'})
    assert result.status_code==200,result.text
    assert len(calls)==1 and calls[0]['tools']==[]
    assert client.get(base+'/tasks').json()==[]
    assert result.json()['draft']['snapshot']['workflow']['nodes'][1]['config']['model_ref']=='quality'


def test_real_training_without_workflow_then_bind_existing_draft(real_compute):
    (client,app,p,settings),service=real_compute;pid=p['id'];base='/api/v1/projects/'+pid
    raw='x,y\n'+''.join(f'{i},{3*i+2}\n' for i in range(40))
    upload=client.post(base+'/datasets/upload',files={'file':('train.csv',raw.encode(),'text/csv')},data={'mapping':json.dumps({'target':'y'})})
    assert upload.status_code==201,upload.text
    dataset=upload.json()['id']
    study=client.post(base+'/modeling/studies',json={'dataset_id':dataset,'request_key':'study','budget':{'trials':2,'seconds':180,'trial_seconds':60}}).json()
    # Draft exists before any training, without a training block anywhere.
    graph(client,pid,**prediction_graph())
    client.put(base+'/models/quality',json={'name':'质量预测'})
    request={'request_key':'train','engine':'sklearn','models':['linear'],'batch_size':1}
    launched=client.post(base+f'/modeling/studies/{study["id"]}/train',json=request)
    assert launched.status_code==202,launched.text
    task=wait_task(client,base,launched.json())
    assert task['status']=='succeeded',task
    assert task['workflow_id']=='' and task['runs']==[]
    saved_study=client.get(base+'/modeling/studies/'+study['id']).json()
    assert saved_study['active_since'] is None
    assert client.post(base+f'/modeling/studies/{study["id"]}/train',json=request).json()['id']==task['id']
    candidate=task['outputs']['id']
    bind=client.put(base+'/models/quality',json={'name':'质量预测','expected_revision':1,'study_id':study['id'],'candidate_id':candidate,'slot':0})
    assert bind.status_code==200,bind.text
    data=client.post(base+'/datasets/upload',files={'file':('new.csv',b'x\n41\n42\n','text/csv')}).json()['id']
    direct=client.post(base+'/agent-tools',json={'name':'project_models','arguments':{
        'action':'predict','model_ref':'quality','dataset_id':data,'request_key':'direct-predict'}})
    assert direct.status_code==200,direct.text
    independent=direct.json()
    assert independent['status']=='succeeded' and independent['workflow_id']=='' and independent['runs']==[]
    assert independent['outputs']['model_version']['candidate_id']==candidate
    duplicate=client.post(base+'/models/quality/predict',json={'dataset_id':data,'request_key':'direct-predict'})
    assert duplicate.json()['id']==independent['id']
    run=wait_task(client,base,start(client,base,'prediction',inputs={'dataset_id':data}))
    assert run['status']=='succeeded',run
    output=run['outputs']['result']
    assert output['model_version']['candidate_id']==candidate
    download=client.get(base+'/'+output['artifact']);assert download.status_code==200 and b'prediction' in download.content
    assert client.put(base+'/models/quality',json={'name':'later','expected_revision':2}).status_code==200
    assert client.get(base+'/tasks/'+run['id']).json()['outputs']==run['outputs']
    again=wait_task(client,base,start(client,base,'unbound-again',inputs={'dataset_id':data}))
    assert again['status']=='failed' and '尚未绑定' in again['error']


def test_independent_training_stop_resume_and_idempotency(modeling, monkeypatch):
    (client, app, p, settings), service = modeling
    base = '/api/v1/projects/' + p['id']
    data = client.post(base+'/datasets/upload', files={'file':('train.csv', b'x,y\n1,2\n2,3\n3,4\n', 'text/csv')},
        data={'mapping':json.dumps({'target':'y'})}).json()
    study = client.post(base+'/modeling/studies', json={'dataset_id':data['id'],'request_key':'independent'}).json()
    entered, release = asyncio.Event(), asyncio.Event()
    calls=[]
    async def held(pid, sid, cid, task_id, run_id, **kwargs):
        calls.append((pid,sid,cid,task_id)); entered.set()
        await release.wait()
        return {'id':cid,'status':'completed'}
    monkeypatch.setattr(service,'run_candidate',held)
    request={'request_key':'same','engine':'sklearn','models':['linear'],'batch_size':1}
    url=base+f'/modeling/studies/{study["id"]}/train'
    task=client.post(url,json=request).json()
    client.portal.call(asyncio.wait_for,entered.wait(),2)
    assert client.post(url,json=request).json()['id']==task['id'] and len(calls)==1
    assert client.post(url,json={**request,'models':['forest']}).status_code==409
    assert client.post(base+'/tasks/'+task['id']+'/stop').json()['status']=='interrupted'
    client.portal.call(release.set)
    resumed=client.post(base+'/tasks/'+task['id']+'/resume',json={})
    assert resumed.status_code==202,resumed.text
    done=wait_task(client,base,resumed.json()); assert done['status']=='succeeded'
    assert len(calls)==2 and calls[0]==calls[1] and done['workflow_id']==''
    other=client.post('/api/v1/projects',json={'name':'隔离项目'}).json()['id']
    assert client.put(f'/api/v1/projects/{other}/models/foreign',json={'name':'拒绝外部候选','study_id':study['id'],'candidate_id':task['inputs']['candidate_id']}).status_code in {404,422}


def test_generation_conflict_preserves_edits_made_during_model_call(configured,monkeypatch):
    from agent_platform.models import StreamEvent
    from agent_platform.project_workflow_edit import save_workflow, SaveWorkflow
    client,app,p,settings=configured;pid=p['id'];base='/api/v1/projects/'+pid
    original=client.get('/api/v1/applications/'+pid+'/draft').json()
    manual={'nodes':[node('s','start'),node('e','end',outputs={'human':True})],'edges':[edge('s','e')]}
    class Provider:
        async def stream(self,**kwargs):
            await save_workflow(app.state.services,pid,pid,SaveWorkflow(expected_revision=original['revision'],workflow=manual))
            yield StreamEvent(type='content_block_start',data={'index':0,'content_block':{'type':'text','text':json.dumps({'workflow':prediction_graph()})}})
    monkeypatch.setattr(app.state.services.local_agents.connections,'provider',lambda *a,**k:Provider())
    result=client.post(base+'/workflow-generation',json={'workflow_id':pid,'expected_revision':original['revision'],'instruction':'创建预测流程'})
    assert result.status_code==409,result.text
    current=client.get('/api/v1/applications/'+pid+'/draft').json()
    assert current['snapshot']['workflow']['nodes'][1]['config']['outputs']=={'human':True}
    assert client.get(base+'/tasks').json()==[]


def test_code_executes_without_workflow_and_protects_materials(real_compute):
    (client, app, p, settings), service = real_compute
    # Uses the actual project Docker sandbox, independent of the ML worker.
    root=settings.workspace_root/p['id']; (root/'requirement-package').mkdir(exist_ok=True)
    (root/'requirement-package'/'input.txt').write_text('original')
    result=client.post('/api/v1/projects/'+p['id']+'/agent-tools',json={'name':'project_code','arguments':{'code':
        "from pathlib import Path\nprint(6*7)\ntry:\n Path('requirement-package/input.txt').write_text('changed')\nexcept OSError:\n print('protected')\nPath('results').mkdir(exist_ok=True)\nPath('results/code.txt').write_text('42')"}})
    assert result.status_code==200,result.text
    assert result.json()['exit_code']==0 and '42' in result.json()['stdout'] and 'protected' in result.json()['stdout']
    assert (root/'requirement-package'/'input.txt').read_text()=='original'
    assert (root/'results'/'code.txt').read_text()=='42'
    assert not app.state.services.sandboxes.sessions


def test_code_node_uses_form_inputs_and_current_draft_on_rerun(real_compute):
    (client, app, p, settings), service=real_compute
    pid=p['id'];base='/api/v1/projects/'+pid
    nodes=[node('s','start',inputs=[{'name':'quantity','type':'number','required':True}]),
        node('code','code',code="def main(inputs):\n    print('calculated')\n    return {'quantity': inputs['quantity'] * 2}",inputs={'quantity':ref('$inputs','quantity')}),
        node('e','end',outputs={'result':ref('code','output')})]
    graph(client,pid,nodes,[edge('s','code'),edge('code','e')])
    before=wait_task(client,base,start(client,base,'code-v1',inputs={'quantity':4}))
    assert before['status']=='succeeded' and before['outputs']=={'result':{'quantity':8}},before
    nodes[1]['config']['code']="def main(inputs):\n    return {'quantity': inputs['quantity'] * 3}"
    graph(client,pid,nodes,[edge('s','code'),edge('code','e')])
    after=wait_task(client,base,start(client,base,'code-v2',inputs={'quantity':4}))
    assert after['outputs']=={'result':{'quantity':12}}
    assert client.get(base+'/tasks/'+before['id']).json()['outputs']==before['outputs']
