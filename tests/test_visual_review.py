import asyncio
import csv
import json
from pathlib import Path

import pytest
from agent_platform import visual_review as recipe,visual_review_code as code
from agent_platform.project_agent_tools import WorkspaceProjectTools
from tests.test_projects import configured,start,settled  # noqa:F401
from tests.test_example_projects import install
from tests.test_modeling import wait_task


@pytest.fixture
def files(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);root=Path('requirement-package');root.mkdir()
    for name,value in recipe.example_files().items():(root/name).write_bytes(value if isinstance(value,bytes) else value.encode())
    return {'source_path':str(root/'待复核.csv'),'criteria':'仅对演示线条检查连续且在边界内'}


def test_frozen_pictures_explicit_review_unknown_and_report_reuse(files):
    prepared=code.prepare(files);first={'prepared':prepared,'sample':prepared['samples'][0]}
    context=code.context(first);assert len(context['images'])==2
    assert '不合格' in context['markdown']
    old_image=Path(context['images'][0]['path']).read_bytes()
    Path('requirement-package/连续线条.png').write_bytes(recipe.diagram(True))
    assert Path(code.context(first)['images'][0]['path']).read_bytes()==old_image
    a=code.collect({**first,'answer':{'label':'合格','reason':'线条连续','method':'仅对演示线条检查缺口','reference_candidate':True}})
    b=code.collect({'prepared':prepared,'sample':prepared['samples'][1],'answer':{'label':'无法判断','reference_candidate':True}})
    result=code.finish({'prepared':prepared,'reviews':[a,b]})
    assert (result['reviewed'],result['unknown'],result['different'],result['candidates'])==(2,1,1,1)
    saved=json.loads(Path(result['source_result_path']).read_text())
    assert saved['reviews'][0]['machine_prediction']=='不合格' and saved['reviews'][0]['human_label']=='合格'
    assert saved['reviews'][1]['reference_candidate'] is False
    labeled=list(csv.DictReader(Path(result['labeled_path']).open(encoding='utf-8-sig')))
    assert [r['sample_id'] for r in labeled]==['s1']
    report=Path(result['artifacts'][0]['file_path']).read_bytes()
    replay=code.report({'source_result_path':result['source_result_path']})
    assert replay['candidates']==1 and Path(result['artifacts'][0]['file_path']).read_bytes()==report
    assert all(Path(a['file_path']).is_file() for a in result['artifacts'])
    Path(context['images'][0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='图片快照'):code.collect({**first,'answer':{'label':'合格'}})


def test_metadata_warnings_and_bounded_review_do_not_change_original_machine_values(files):
    p=Path(files['source_path']);p.write_text('sample_id,image_path,product_type,machine_prediction,reference_path,reference_product,reference_version,captured_at,reference_available_at\na,连续线条.png,X,1,连续线条.png,Y,v1,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\nb,连续线条.png,X,不合格,,,,,\nc,中间断开.png,X,合格,,,,,\n')
    prepared=code.prepare({**files,'sample_limit':2});data=code.frozen(prepared)
    assert data['total']==3 and data['not_selected']==1
    s=data['samples'][0];assert s['machine']=='1'
    assert any('形成于检测之后' in w for w in s['warnings'])
    assert any('产品类型不同' in w for w in s['warnings'])
    assert any('相同图片' in w for w in s['warnings'])
    item=code.collect({'prepared':prepared,'sample':prepared['samples'][0],'answer':{'label':'合格','reference_candidate':True}})
    record=json.loads(Path(item['path']).read_text());assert not record['reference_candidate']
    assert record['agreement']=='没有可比较机器判断'


@pytest.mark.parametrize('kind',['missing','duplicate_name','duplicate_id','outside','corrupt','noninteger'])
def test_bad_inputs_fail_before_questions_without_partial_snapshots(files,kind):
    p=Path(files['source_path'])
    if kind=='missing':Path('requirement-package/连续线条.png').unlink()
    if kind=='duplicate_name':
        other=Path('requirement-package/other');other.mkdir();(other/'连续线条.png').write_bytes(recipe.diagram())
    if kind=='duplicate_id':p.write_text(p.read_text().replace('s2,','s1,'))
    if kind=='outside':p.write_text(p.read_text().replace('连续线条.png','../private.png'))
    if kind=='corrupt':Path('requirement-package/连续线条.png').write_bytes(b'not a PNG')
    if kind=='noninteger':files['sample_limit']=1.2
    with pytest.raises(ValueError):code.prepare(files)
    assert not list(Path('results').glob('visual-review-*'))


def test_platform_two_waiting_steps_stop_resume_frozen_images_and_export_without_model(configured):
    client,app,_,settings=configured;pid=install(client,'visual-review');base='/api/v1/projects/'+pid
    guide=client.get(base+'/example').json()
    for f in guide['files']:
        if f['name'].endswith('.png'):
            r=client.get('/api/v1/applications/'+pid+'/workspace/files/'+f['path']);assert r.content==recipe.example_files()[f['name']]
    task=wait_task(client,base,start(client,base,'first',workflow_id=pid),seconds=40)
    assert task['status']=='waiting_input',task.get('error')
    run=task['runs'][0];first=run['waiting_input'];assert 's1' in first['context']['markdown']
    image=first['context']['images'][0]['path'];frozen=(settings.workspace_root/pid/image).read_bytes()
    original=next(f for f in guide['files'] if f['name']=='连续线条.png')
    (settings.workspace_root/pid/original['path']).write_bytes(recipe.diagram(True))
    assert client.get(base+'/tasks/'+task['id']).json()['runs'][0]['waiting_input']==first
    url=f'{base}/tasks/{task["id"]}/runs/{run["id"]}/input'
    assert client.post(url,json={'node_id':'expired','values':{},'resume':True}).status_code==409
    # Submit the employee's explicit answer via the existing agent tool.
    project_tools=WorkspaceProjectTools(app.state.services,pid,app.state.services.local_agents)
    async def answer():
        response=await project_tools.call('workflow_run',{'action':'respond','task_id':task['id'],'run_id':run['id'],'node_id':first['node_id'],
            'inputs':{'label':'合格','reason':'连续且在框内','reference_candidate':True}})
        await app.state.services.projects.active[task['id']]
        return response
    asyncio.run(answer())
    task=wait_task(client,base,task,seconds=40)
    assert task['status']=='waiting_input' and task['runs'][0]['waiting_input']['node_id']!=first['node_id']
    assert (settings.workspace_root/pid/image).read_bytes()==frozen
    assert client.post(url,json={'node_id':first['node_id'],'values':{'label':'合格'},'resume':True}).status_code==409
    second=task['runs'][0]['waiting_input'];assert 's2' in second['context']['markdown']
    client.post(base+'/tasks/'+task['id']+'/stop').raise_for_status()
    assert client.post(url,json={'node_id':second['node_id'],'values':{'label':'无法判断'},'resume':True}).status_code==409
    assert client.get(base+'/tasks/'+task['id']).json()['status']=='interrupted'
    client.post(base+'/tasks/'+task['id']+'/resume',json={}).raise_for_status();task=wait_task(client,base,task,seconds=40)
    assert task['runs'][0]['waiting_input']['node_id']==second['node_id']
    client.post(url,json={'node_id':second['node_id'],'values':{'label':'无法判断'},'resume':True}).raise_for_status()
    done=wait_task(client,base,task,seconds=40);assert done['status']=='succeeded',done.get('error')
    result=done['outputs']['result'];assert (result['reviewed'],result['unknown'],result['different'],result['candidates'])==(2,1,1,1)
    assert len(list((settings.workspace_root/pid/'results').glob('*/review-item.json')))==2
    for a in result['artifacts']:assert client.get('/api/v1/applications/'+pid+'/workspace/files/'+a['file_path']).status_code==200
    replay=wait_task(client,base,start(client,base,'export',workflow_id=pid,inputs={'source_result_path':result['source_result_path']}),seconds=40)
    assert replay['status']=='succeeded' and replay['outputs']['result']['reviewed']==2
    assert client.get(base+'/tasks/'+done['id']).json()['outputs']==done['outputs']
    # Handoff to the existing evaluation workflow without treating unknown as a class.
    feedback=client.post(base+'/space/official-workflows/prediction-feedback').json()['workflow_id']
    evaluated=wait_task(client,base,start(client,base,'evaluate-explicit-labels',workflow_id=feedback,inputs=result['feedback_inputs']),seconds=40)
    assert evaluated['status']=='succeeded',evaluated.get('error')
    assert evaluated['outputs']['result']['metrics']['n']==1
