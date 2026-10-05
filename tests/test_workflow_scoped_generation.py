"""One generated fragment applies inside the chosen draft scope without running it."""
import json
from copy import deepcopy
import pytest
from agent_platform.models import StreamEvent
from agent_platform.project_workflow_edit import scoped_workflow
from agent_platform.workflow_models import EdgeSpec, NodeSpec
from tests.test_projects import configured, graph, node, edge, ref, settled  # noqa: F401


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


NOTICE = '使用提示：请先查看数据质量报告，再使用设备汇总；本示例不会修改原始数据。'


def report_graph(client, pid):
    """Local child workflows provide the same output shape as the real report."""
    children = []
    for name in ('profile', 'summary'):
        child = client.post(f'/api/v1/projects/{pid}/members', json={'name': name}).json()['id']
        graph(client, child, [node('s', 'start'), node('e', 'end', outputs={
            'result': {'download': f'results/{name}.csv'}, 'markdown': '# 设备汇总\n\n完整正文'})], [edge('s', 'e')])
        children.append(node(name, 'tool', tool_name='workflow:'+child, input={}))
    nodes = [node('start', 'start'), *children, node('end', 'end', outputs={
        'profile': ref('profile', 'output', 'result'), 'summary': ref('summary', 'output', 'result'),
        'markdown': ref('summary', 'output', 'markdown')})]
    for index, item in enumerate(nodes):
        item['position'] = {'x': index * 220, 'y': 35}
    return {'nodes': nodes, 'edges': [edge('start', 'profile'), edge('profile', 'summary'), edge('summary', 'end')]}


def report_fragment(before):
    notice = NodeSpec.model_validate(node('summary_notice', 'template_transform',
        template=NOTICE+'\n\n{{summary_markdown}}',
        variables={'summary_markdown': ref('summary', 'output', 'markdown')})).model_dump(mode='json')
    end = deepcopy(next(n for n in before['nodes'] if n['id'] == 'end'))
    end['config']['outputs']['markdown'] = ref('summary_notice', 'text')
    return {'nodes': [notice, end], 'edges': [
        EdgeSpec.model_validate(edge('summary', 'summary_notice')).model_dump(mode='json'),
        EdgeSpec.model_validate({**edge('summary_notice', 'end'), 'source_port': 'text'}).model_dump(mode='json')]}


@pytest.mark.parametrize('path', [[], ['repeat'], ['repeat', 'nested']])
def test_report_fragment_keeps_boundary_and_neighbors_then_runs_and_undoes(configured, monkeypatch, path):
    client, app, project, _ = configured
    pid = project['id']; base = f'/api/v1/projects/{pid}'
    original = report_graph(client, pid)
    for item in reversed(path):
        original = {'nodes': [node('s', 'start'), node(item, 'iteration', items=[1], workflow=original,
                    output_node_id='end'), node('end', 'end', outputs={'outer': 'unchanged'})],
                    'edges': [edge('s', item), edge(item, 'end')]}
    graph(client, pid, **original)
    draft = client.get(f'/api/v1/applications/{pid}/draft').json()
    before = draft['snapshot']['workflow']; scope = scoped_workflow(before, path)
    fragment = report_fragment(scope); seen = []
    install_provider(app, monkeypatch, fragment, seen)
    result = client.post(base+'/workflow-generation', json={'workflow_id': pid,
        'expected_revision': draft['revision'], 'instruction': NOTICE, 'workflow_path': path, 'node_ids': ['end']})
    assert result.status_code == 200, result.text
    expected = deepcopy(before); changed = scoped_workflow(expected, path)
    changed['nodes'] = [n for n in changed['nodes'] if n['id'] != 'end'] + fragment['nodes']
    changed['edges'] += fragment['edges']
    assert result.json()['draft']['snapshot']['workflow'] == expected
    assert result.json()['previous_workflow'] == before
    assert len(seen) == 1 and seen[0]['scope']['node_ids'] == ['end']
    assert client.get(base+'/tasks').json() == []
    if not path:
        # The original summary -> end edge remains. The added predecessor must
        # still complete before end resolves the formatted report reference.
        response = client.post(base+'/tasks', json={'request_key': 'report-after-edit', 'mode': 'workflow',
                               'workflow_id': pid, 'inputs': {}})
        assert response.status_code == 202, response.text
        task = settled(client, base, response.json())
        assert task['status'] == 'succeeded', task
        assert task['outputs'] == {'profile': {'download': 'results/profile.csv'},
            'summary': {'download': 'results/summary.csv'}, 'markdown': NOTICE+'\n\n# 设备汇总\n\n完整正文'}
    undo = client.put(base+f'/workflows/{pid}/draft', json={
        'expected_revision': result.json()['draft']['revision'], 'workflow': result.json()['previous_workflow']})
    assert undo.status_code == 200 and undo.json()['draft']['snapshot']['workflow'] == before


@pytest.mark.parametrize('path', [[], ['repeat']])
@pytest.mark.parametrize('invalid', ['unknown', 'outside_edge', 'changed_boundary', 'outside_node', 'deleted_endpoint'])
def test_report_scope_rejects_invalid_edges_without_saving_or_running(configured, monkeypatch, path, invalid):
    client, app, project, _ = configured
    pid = project['id']; base = f'/api/v1/projects/{pid}'
    original = report_graph(client, pid)
    if path:
        original = {'nodes': [node('s', 'start'), node('repeat', 'iteration', items=[1], workflow=original,
                    output_node_id='end'), node('end', 'end')], 'edges': [edge('s', 'repeat'), edge('repeat', 'end')]}
    graph(client, pid, **original)
    draft = client.get(f'/api/v1/applications/{pid}/draft').json()
    scope = scoped_workflow(draft['snapshot']['workflow'], path)
    fragment = report_fragment(scope)
    if invalid == 'unknown':
        fragment['edges'][0]['source'] = 'not_in_this_scope'
    elif invalid == 'outside_edge':
        fragment['edges'].append(edge('start', 'summary'))
    elif invalid == 'changed_boundary':
        fragment['edges'].append({**edge('summary', 'end'), 'source_port': 'changed'})
    elif invalid == 'outside_node':
        fragment['nodes'].append(node('summary', 'end'))
    elif invalid == 'deleted_endpoint':
        fragment = {'nodes': [], 'edges': []}
    seen = []; install_provider(app, monkeypatch, fragment, seen)
    result = client.post(base+'/workflow-generation', json={'workflow_id': pid,
        'expected_revision': draft['revision'], 'instruction': NOTICE, 'workflow_path': path, 'node_ids': ['end']})
    assert result.status_code == 422, result.text
    assert len(seen) == 1
    assert client.get(f'/api/v1/applications/{pid}/draft').json() == draft
    assert client.get(base+'/tasks').json() == []


def test_echoed_boundary_edges_are_preserved_once(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = f'/api/v1/projects/{pid}'
    graph(client, pid, [node('s', 'start'), node('e', 'end')], [edge('s', 'e')])
    draft = client.get(f'/api/v1/applications/{pid}/draft').json()
    seen = []; install_provider(app, monkeypatch, {'nodes': [node('e', 'end', outputs={'updated': True})],
                                                'edges': [edge('s', 'e')]}, seen)
    result = client.post(base+'/workflow-generation', json={'workflow_id': pid,
        'expected_revision': draft['revision'], 'instruction': '修改结果', 'node_ids': ['e']})
    assert result.status_code == 200, result.text
    assert result.json()['draft']['snapshot']['workflow']['edges'] == draft['snapshot']['workflow']['edges']
    assert client.get(base+'/tasks').json() == []


def test_fragment_cannot_overwrite_concurrent_revision(configured, monkeypatch):
    from agent_platform.project_workflow_edit import SaveWorkflow, save_workflow
    from agent_platform.workflow_models import WorkflowSpec
    client, app, project, _ = configured
    pid = project['id']; base = f'/api/v1/projects/{pid}'
    graph(client, pid, **report_graph(client, pid))
    draft = client.get(f'/api/v1/applications/{pid}/draft').json()
    fragment = report_fragment(draft['snapshot']['workflow'])
    concurrent = deepcopy(draft['snapshot']['workflow']); concurrent['nodes'][0]['position']['x'] = 999
    class Provider:
        async def stream(self, **kwargs):
            await save_workflow(app.state.services, pid, pid, SaveWorkflow(
                expected_revision=draft['revision'], workflow=WorkflowSpec.model_validate(concurrent)))
            yield StreamEvent(type='content_block_start', data={'index': 0, 'content_block': {
                'type': 'text', 'text': json.dumps({'workflow': fragment})}})
    monkeypatch.setattr(app.state.services.local_agents.connections, 'provider', lambda *a, **k: Provider())
    result = client.post(base+'/workflow-generation', json={'workflow_id': pid,
        'expected_revision': draft['revision'], 'instruction': NOTICE, 'node_ids': ['end']})
    assert result.status_code == 409, result.text
    after = client.get(f'/api/v1/applications/{pid}/draft').json()
    assert after['revision'] == draft['revision'] + 1 and after['snapshot']['workflow'] == concurrent
    assert client.get(base+'/tasks').json() == []
