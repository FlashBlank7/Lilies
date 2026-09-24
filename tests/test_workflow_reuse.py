"""New drafts reuse eligible results without mutating an old run."""
import pytest
from tests.test_projects import configured, graph, node, edge, ref, start, settled  # noqa: F401


@pytest.fixture
def recipe(configured, monkeypatch):
    client, app, project, settings = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    calls = []
    async def image(requested=''):
        return 'test-image'
    monkeypatch.setattr(app.state.services.modeling, 'image', image)
    async def execute(sandboxes, workspace, config, inputs):
        calls.append(config.code)
        namespace = {}; exec(config.code, namespace)
        return {'output': namespace['main'](inputs)}
    monkeypatch.setattr('agent_platform.python_execution.execute_function', execute)
    source = settings.workspace_root / pid / 'requirement-package/input.csv'
    source.parent.mkdir(exist_ok=True); source.write_text('original')
    nodes = [node('start', 'start', inputs=[{'name':'file','type':'string','required':True}]),
        node('prepare', 'code', code='def main(inputs):\n return {"value": 7}',
             inputs={'file':ref('$inputs','file')}, reuse_completed=True),
        node('report', 'code', code='def main(inputs):\n return {"text": "old", **inputs}',
             inputs={'value':ref('prepare','output','value')}),
        node('end', 'end', outputs={'result':ref('report','output')})]
    edges = [edge('start','prepare'),edge('prepare','report'),edge('report','end')]
    graph(client, pid, nodes, edges)
    def run(key, old=None, **extra):
        return settled(client, base, start(client, base, key, inputs={'file':'requirement-package/input.csv'},
            **({'reuse_task_id':old['id']} if old else {}), **extra))
    return client, app, pid, base, nodes, edges, run, calls, source


def test_new_report_keeps_preparation_and_old_snapshot(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    old = run('old'); assert old['status']=='succeeded', old['error']
    nodes[1]['position'] = {'x':100, 'y':200}
    nodes[2]['config']['code'] = 'def main(inputs):\n return {"text": "new", **inputs}'
    graph(client, pid, nodes, edges)
    new = run('new', old)
    assert new['status']=='succeeded', new['error']
    assert new['outputs']['result']['text']=='new'
    assert len(calls)==3
    assert new['runs'][0]['reuse']['nodes']==['start','prepare']
    assert new['runs'][0]['id'] != old['runs'][0]['id']
    assert client.get(base+'/tasks/'+old['id']).json()['outputs']['result']['text']=='old'
    assert run('new', old)['id']==new['id']  # request idempotency
    assert len(calls)==3
    source.write_text('different bytes, same path')
    changed = run('changed', new)
    assert changed['status']=='succeeded'
    assert changed['runs'][0]['reuse']['nodes']==[]
    assert len(calls)==5


def test_changed_code_or_removed_artifact_reexecutes(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    artifact = source.parent.parent/'results/prepared.csv'; artifact.parent.mkdir(exist_ok=True)
    code = f'from pathlib import Path\ndef main(inputs):\n Path({str(artifact)!r}).write_text("data")\n return {{"value": 7, "file": "results/prepared.csv"}}'
    nodes[1]['config']['code']=code; graph(client,pid,nodes,edges)
    old=run('artifact'); assert old['status']=='succeeded',old['error']
    artifact.unlink()
    new=run('missing',old)
    assert new['runs'][0]['reuse']['nodes']==['start']
    assert artifact.exists() and len(calls)==4
    nodes[1]['config']['code']=code.replace('"value": 7','"value": 8')
    graph(client,pid,nodes,edges)
    changed=run('new-code',new)
    assert changed['outputs']['result']['value']==8 and len(calls)==6


def test_reuse_source_must_belong_to_project(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    old=run('old')
    other=client.post('/api/v1/projects',json={'name':'Other'}).json()['id']
    response=client.post('/api/v1/projects/'+other+'/tasks',json={'request_key':'cross', 'reuse_task_id':old['id']})
    assert response.status_code in {400,404}


def test_failed_step_is_rerun_with_current_code(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    nodes[2]['config']['code']='def main(inputs):\n raise ValueError("repair me")'
    graph(client,pid,nodes,edges)
    old=run('failed'); assert old['status']=='failed'
    nodes[2]['config']['code']='def main(inputs):\n return inputs'
    graph(client,pid,nodes,edges)
    fixed=run('fixed',old)
    assert fixed['status']=='succeeded'
    assert fixed['runs'][0]['reuse']['nodes']==['start','prepare']
    assert len(calls)==3


def test_legacy_run_without_checkpoint_executes_normally(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    old=run('old')
    import asyncio
    async def remove():
        store=app.state.services.workflow_store
        state=(await store.get_run(old['runs'][0]['id']))['state']
        state.reuse_checkpoints={}
        await store.update_run(state.run_id,status='succeeded',state=state)
    asyncio.run(remove())
    new=run('legacy',old)
    assert new['status']=='succeeded' and new['runs'][0]['reuse']['nodes']==[] and len(calls)==4


def test_environment_change_reexecutes_code(recipe, monkeypatch):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    old=run('old')
    async def image(requested=''):
        return 'different-image'
    monkeypatch.setattr(app.state.services.modeling, 'image', image)
    new=run('changed-environment',old)
    assert new['status']=='succeeded'
    assert new['runs'][0]['reuse']['nodes']==['start'] and len(calls)==4


def test_run_metadata_is_never_reused(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    nodes[1]['config']['inputs']['run']=ref('$run','run_id')
    graph(client,pid,nodes,edges)
    old=run('old'); new=run('new',old)
    assert new['status']=='succeeded'
    assert new['runs'][0]['reuse']['nodes']==['start'] and len(calls)==4


@pytest.mark.parametrize('via_start', [True, False])
def test_changed_input_only_reexecutes_consumers(recipe, via_start):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    def input_ref(name):
        return ref('start', 'output', name) if via_start else ref('$inputs', name)
    nodes = [node('start', 'start', inputs=[{'name':'value','type':'number'}, {'name':'title','type':'string'}]),
        node('prepare', 'code', code='def main(inputs):\n return {"value": inputs["value"] * 2}',
             inputs={'value': input_ref('value')}, reuse_completed=True),
        node('report', 'template_transform', template='{{ title }}: {{ value }}',
             variables={'title':input_ref('title'), 'value':ref('prepare','output','value')}),
        node('end','end',outputs={'text':ref('report','text')})]
    graph(client,pid,nodes,edges)
    def calculate(key, inputs, old=None):
        return settled(client,base,start(client,base,key,inputs=inputs,**({'reuse_task_id':old['id']} if old else {})))
    old=calculate('first',{'value':3,'title':'旧报告'});assert old['status']=='succeeded',old['error']
    new=calculate('title-only',{'value':3,'title':'新报告'},old)
    assert new['status']=='succeeded' and new['outputs']=={'text':'新报告: 6'}
    assert new['runs'][0]['reuse']['nodes']==['prepare'] and len(calls)==1
    unchanged=calculate('unused-field',{'value':3,'title':'新报告','unrelated':42},new)
    assert unchanged['runs'][0]['reuse']['nodes']==['start','prepare','report','end'] and len(calls)==1
    changed=calculate('data-changed',{'value':5,'title':'新报告'},unchanged)
    assert changed['outputs']=={'text':'新报告: 10'} and len(calls)==2
    assert changed['runs'][0]['reuse']['nodes']==[]
    assert client.get(base+'/tasks/'+old['id']).json()['outputs']=={'text':'旧报告: 6'}


def test_unused_file_change_does_not_invalidate_independent_branch(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    nodes[0]['config']['inputs'].append({'name':'other','type':'string'})
    nodes[1]['config']['inputs']={'file':ref('start','output','file')}
    graph(client,pid,nodes,edges)
    other=source.with_name('other.csv');other.write_text('v1')
    inputs={'file':'requirement-package/input.csv','other':'requirement-package/other.csv'}
    old=settled(client,base,start(client,base,'files-v1',inputs=inputs))
    other.write_text('v2')
    new=settled(client,base,start(client,base,'files-v2',inputs=inputs,reuse_task_id=old['id']))
    assert new['status']=='succeeded' and new['runs'][0]['reuse']['nodes']==['prepare']
    assert len(calls)==3
    source.write_text('changed dependency')
    latest=settled(client,base,start(client,base,'files-v3',inputs=inputs,reuse_task_id=new['id']))
    assert latest['status']=='succeeded' and latest['runs'][0]['reuse']['nodes']==[] and len(calls)==5


def test_legacy_v1_fingerprints_recompute_instead_of_assuming_field_dependencies(recipe):
    client, app, pid, base, nodes, edges, run, calls, source = recipe
    old=run('old')
    async def downgrade():
        store=app.state.services.workflow_store
        state=(await store.get_run(old['runs'][0]['id']))['state']
        for value in state.reuse_checkpoints.values():
            value['version']=1;value.pop('arguments',None)
        await store.update_run(state.run_id,status='succeeded',state=state)
    client.portal.call(downgrade)
    new=run('legacy-v1',old)
    assert new['status']=='succeeded' and new['runs'][0]['reuse']['nodes']==[] and len(calls)==4


def test_existing_scalar_environment_fingerprints_still_reuse(recipe):
    client,app,pid,base,nodes,edges,run,calls,source=recipe
    old=run('before-environment-update')
    async def existing_format():
        store=app.state.services.workflow_store;state=(await store.get_run(old['runs'][0]['id']))['state']
        state.reuse_checkpoints['start']['environment']=''
        state.reuse_checkpoints['prepare']['environment']='test-image'
        await store.update_run(state.run_id,status='succeeded',state=state)
    client.portal.call(existing_format)
    new=run('after-environment-update',old)
    assert new['status']=='succeeded' and new['runs'][0]['reuse']['nodes']==['start','prepare'] and len(calls)==3


def test_completed_iteration_reuse_checks_inner_code_input_and_artifacts(recipe):
    client,app,pid,base,_,_,run,calls,source=recipe
    artifact=source.parent.parent/'results/inner.csv';artifact.parent.mkdir(exist_ok=True)
    inner_code=f'from pathlib import Path\ndef main(inputs):\n Path({str(artifact)!r}).write_text("data")\n return {{"n":inputs["item"],"file":"results/inner.csv"}}'
    inner_nodes=[node('begin','start',inputs=[{'name':'item'},{'name':'file'}]),
        node('work','code',code=inner_code,reuse_completed=True,inputs={'item':ref('begin','item'),'file':ref('begin','file')}),
        node('done','end',outputs={'value':ref('work','output')})]
    nodes=[node('start','start',inputs=[{'name':'file'}]),
        node('loop','iteration',items=[1,2],variables={'file':ref('start','file')},workflow={'nodes':inner_nodes,'edges':[edge('begin','work'),edge('work','done')]},
             item_name='item',output_node_id='done',output_path=['value'],parallelism=1,reuse_completed=True),
        node('report','code',code='def main(inputs):\n return inputs',inputs={'values':ref('loop','items')}),
        node('end','end',outputs={'result':ref('report','output')})]
    edges=[edge('start','loop'),edge('loop','report'),edge('report','end')];graph(client,pid,nodes,edges)
    old=run('first-iteration');assert old['status']=='succeeded',old['error'];assert len(calls)==3
    nodes[2]['config']['code']='def main(inputs):\n return {**inputs,"note":"new"}'
    graph(client,pid,nodes,edges);new=run('new-report',old)
    assert new['status']=='succeeded' and new['outputs']['result']['note']=='new'
    assert new['runs'][0]['reuse']['nodes']==['start','loop'] and len(calls)==4
    artifact.unlink();missing=run('missing-output',new)
    assert missing['status']=='succeeded' and len(calls)==7
    inner_nodes[1]['config']['code']=inner_code.replace('inputs["item"]','inputs["item"]*10')
    graph(client,pid,nodes,edges);changed=run('changed-inner',missing)
    assert changed['outputs']['result']['values'][0]['n']==10 and len(calls)==10
    source.write_text('changed source');latest=run('changed-source',changed)
    assert latest['status']=='succeeded' and len(calls)==13
    literal=source.with_name('constant.csv');literal.write_text('v1')
    inner_nodes[1]['config']['inputs']['literal']='requirement-package/constant.csv'
    graph(client,pid,nodes,edges);declared=run('literal-dependency',latest)
    assert declared['status']=='succeeded' and len(calls)==16
    literal.write_text('v2');mutated=run('changed-literal',declared)
    assert mutated['status']=='succeeded' and len(calls)==19
    nodes[1]['config']['reuse_completed']=False;graph(client,pid,nodes,edges)
    disabled=run('explicitly-disabled',mutated)
    assert disabled['status']=='succeeded' and len(calls)==22


def test_iteration_waiting_keeps_completed_work_per_occurrence(recipe):
    client,app,pid,base,_,_,run,calls,source=recipe
    inner_nodes=[node('begin','start',inputs=[{'name':'item'}]),
        node('work','code',code='def main(inputs):\n return inputs',inputs={'item':ref('begin','item')}),
        node('ask','human_input',fields=[{'name':'answer','label':'补充回答','type':'string','required':True}]),
        node('done','end',outputs={'value':ref('work','output'),'answer':ref('ask','answer')})]
    nodes=[node('start','start'),node('loop','iteration',items=[1,2],workflow={'nodes':inner_nodes,
        'edges':[edge('begin','work'),edge('work','ask'),edge('ask','done')]},item_name='item',output_node_id='done',parallelism=1),
        node('end','end',outputs={'items':ref('loop','items')})]
    graph(client,pid,nodes,[edge('start','loop'),edge('loop','end')]);task=run('wait-iteration')
    for count,answer in [(1,'first'),(2,'second')]:
        assert task['status']=='waiting_input',task['error'];assert len(calls)==count
        run_id=task['runs'][0]['id']
        response=client.post(base+'/tasks/'+task['id']+'/runs/'+run_id+'/input',json={'values':{'answer':answer}})
        assert response.status_code==200,response.text
        client.post(base+'/tasks/'+task['id']+'/resume',json={}).raise_for_status()
        task=settled(client,base,task)
    assert task['status']=='succeeded' and len(calls)==2
    assert [r['answer'] for r in task['outputs']['items']]==['first','second']
