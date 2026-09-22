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
