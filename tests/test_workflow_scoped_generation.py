"""One generated fragment applies inside the chosen draft scope without running it."""
import json
from copy import deepcopy
import pytest
from agent_platform.models import StreamEvent
from tests.test_projects import configured, graph, node, edge  # noqa: F401


def install_provider(app, monkeypatch, result, seen):
    class Provider:
        async def stream(self, **kwargs):
            seen.append(json.loads(kwargs['messages'][0].content[0].text))
            assert kwargs['tools'] == []
            yield StreamEvent(type='content_block_start', data={'index':0, 'content_block':{
                'type':'text', 'text':json.dumps({'workflow':result})}})
    def provider(project_id, role):
        assert role == 'generation'
        return Provider()
    monkeypatch.setattr(app.state.services.local_agents.connections, 'provider', provider)


@pytest.mark.parametrize('path', [[], ['repeat'], ['repeat', 'nested']])
def test_one_response_scope_preserves_neighbors_and_undo(configured, monkeypatch, path):
    client, app, p, _ = configured; pid=p['id']; base=f'/api/v1/projects/{pid}'
    inner = {'nodes':[node('s','start'),node('e','end',outputs={'old':True})], 'edges':[edge('s','e')]}
    original = deepcopy(inner)
    for item in reversed(path):
        original = {'nodes':[node('s','start'), node(item, 'iteration', items=[1], workflow=original, output_node_id='e'),
                             node('e','end',outputs={'outer':'unchanged'})], 'edges':[edge('s',item),edge(item,'e')]}
    graph(client, pid, **original)
    draft=client.get(f'/api/v1/applications/{pid}/draft').json()
    old=draft['snapshot']['workflow']; fragment={'nodes':[node('e','end',outputs={'new':42})], 'edges':[]}
    seen=[];install_provider(app,monkeypatch,fragment,seen)
    result=client.post(base+'/workflow-generation', json={'workflow_id':pid,'expected_revision':draft['revision'],
                      'instruction':'把结果改为42','workflow_path':path,'node_ids':['e']})
    assert result.status_code == 200, result.text
    assert len(seen)==1 and [n['id'] for n in seen[0]['workflow']['nodes']]==['e']
    assert len(seen[0]['read_only_context']['nodes'])==2
    expected=deepcopy(old); cursor=expected
    for item in path: cursor=next(n for n in cursor['nodes'] if n['id']==item)['config']['workflow']
    next(n for n in cursor['nodes'] if n['id']=='e')['config']['outputs']={'new':42}
    assert result.json()['draft']['snapshot']['workflow']==expected
    assert result.json()['previous_workflow']==old
    assert client.get(base+'/tasks').json()==[]
    undo=client.put(base+f'/workflows/{pid}/draft',json={'expected_revision':result.json()['draft']['revision'],'workflow':old})
    assert undo.status_code==200 and undo.json()['draft']['snapshot']['workflow']==old


def test_edit_entire_iteration_body_without_touching_parent(configured, monkeypatch):
    client,app,p,_=configured;pid=p['id'];base=f'/api/v1/projects/{pid}'
    inner={'nodes':[node('s','start'),node('e','end')], 'edges':[edge('s','e')]}
    graph(client,pid,[node('s','start'),node('repeat','iteration',items=[1,2],workflow=inner,output_node_id='e'),node('e','end')],
          [edge('s','repeat'),edge('repeat','e')])
    draft=client.get(f'/api/v1/applications/{pid}/draft').json()
    changed=deepcopy(inner);changed['nodes'][1]['config']['outputs']={'new':'body'}
    seen=[];install_provider(app,monkeypatch,changed,seen)
    result=client.post(base+'/workflow-generation',json={'workflow_id':pid,'expected_revision':draft['revision'],
                      'instruction':'修改循环输出','workflow_path':['repeat']})
    assert result.status_code==200,result.text
    after=result.json()['draft']['snapshot']['workflow'];before=draft['snapshot']['workflow']
    assert after['nodes'][0]==before['nodes'][0] and after['nodes'][2]==before['nodes'][2]
    assert after['edges']==before['edges']
    assert after['nodes'][1]['config']['items']==[1,2]
    assert after['nodes'][1]['config']['workflow']['nodes'][1]['config']['outputs']=={'new':'body'}


def test_rejects_stale_scope_and_outside_changes_without_mutating_draft(configured,monkeypatch):
    client,app,p,_=configured;pid=p['id'];base=f'/api/v1/projects/{pid}'
    graph(client,pid,[node('s','start'),node('e','end')],[edge('s','e')])
    draft=client.get(f'/api/v1/applications/{pid}/draft').json(); seen=[]
    install_provider(app,monkeypatch,{'nodes':[node('s','start'),node('e','end',outputs={'wrong':True})],'edges':[]},seen)
    request={'workflow_id':pid,'expected_revision':draft['revision'],'instruction':'修改结果'}
    for scope in [{'node_ids':['missing']},{'workflow_path':['s']}]:
        response=client.post(base+'/workflow-generation',json={**request,**scope})
        assert response.status_code==422,response.text
    assert seen==[]
    response=client.post(base+'/workflow-generation',json={**request,'node_ids':['e']})
    assert response.status_code==422 and '选区外' in response.text
    assert client.get(f'/api/v1/applications/{pid}/draft').json()['revision']==draft['revision']
