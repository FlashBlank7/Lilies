"""Compact project tools, atomic edits and a customer-free repair loop."""
import json
from copy import deepcopy
import pytest

from tests.test_projects import configured, graph, node, edge, ref  # noqa: F401
from tests.test_project_tool_help import prepare
from tests.test_project_conversation import TestSession, configure_agent, put_progress, item, agent_settled
from agent_platform.project_metrics import payload_measurement, session_metrics


def tool(client, base, name, **arguments):
    return client.post(base+'/agent-tools', json={'name': name, 'arguments': arguments})


def batch(draft, *operations, key='change'):
    return {'expected_revision': draft['revision'], 'expected_content_hash': draft['content_hash'],
            'idempotency_key': key, 'operations': list(operations)}


def update(id, **changes):
    return {'op': 'update_node', 'data': {'node_id': id, 'changes': changes, 'merge_config': True}}


def test_summary_details_and_atomic_edit_preserve_layout_and_conflicts(configured):
    client, manager, pid, base = prepare(configured)
    graph(client, pid, [node('s', 'start'), {**node('e', 'end', outputs={'value': 'x'*15000}),
        'position': {'x': 381, 'y': 119}}], [edge('s', 'e')])
    before = client.get('/api/v1/applications/'+pid+'/draft').json()
    summary = tool(client, base, 'workflow_draft').json()
    assert summary['revision'] == before['revision'] and summary['content_hash'] == before['content_hash']
    assert 'snapshot' not in summary and 'x'*100 not in json.dumps(summary)
    assert tool(client, base, 'workflow_draft', view='full').json() == before
    assert tool(client, base, 'workflow_draft', view='nodes', node_ids=['e']).json()['nodes'] == before['snapshot']['workflow']['nodes'][1:]
    assert tool(client, base, 'workflow_draft', view='nodes', node_ids=['missing']).status_code == 422
    large = tool(client, base, 'workflow_run', action='start').json()
    assert large['outputs_truncated'] and len(json.dumps(large['outputs'])) < 2000
    complete = tool(client, base, 'workflow_run', action='inspect', task_id=large['id'], view='full').json()
    assert complete['outputs'] == {'value': 'x'*15000}
    invalid = batch(summary, update('e', title='must roll back'), update('missing', title='bad'))
    assert tool(client, base, 'workflow_draft', batch=invalid).status_code == 404
    assert client.get('/api/v1/applications/'+pid+'/draft').json() == before
    edits = batch(summary, update('e', title='业务结果'), update('e', config={'outputs': {'value': 2}}))
    saved = tool(client, base, 'workflow_draft', batch=edits)
    assert saved.status_code == 200, saved.text
    assert saved.json()['revision'] == summary['revision']+1 and saved.json()['operations_applied'] == 2
    after = client.get('/api/v1/applications/'+pid+'/draft').json()
    assert after['snapshot']['workflow']['nodes'][1]['position'] == {'x': 381, 'y': 119}
    assert tool(client, base, 'workflow_draft', batch=edits).json()['revision'] == saved.json()['revision']
    for changed in [dict(edits, idempotency_key='stale'),
                    dict(edits, operations=[update('e', title='collision')]),
                    dict(batch(saved.json(), update('e', title='hash')), expected_content_hash='stale')]:
        assert tool(client, base, 'workflow_draft', batch=changed).status_code == 422
    assert client.get('/api/v1/applications/'+pid+'/draft').json() == after
    run = tool(client, base, 'workflow_run', action='start').json()
    assert run['outputs'] == {'value': 2}
    assert tool(client, base, 'workflow_run', action='inspect', task_id=large['id'], view='full').json()['outputs'] == complete['outputs']
    assert tool(client, base, 'workflow_run', action='inspect', task_id=run['id'], view='full').json()['outputs'] == run['outputs']


def test_batch_rejects_foreign_members_but_allows_requirement_changes(configured):
    client, _, pid, base = prepare(configured)
    other = client.post('/api/v1/projects', json={'name': '其他项目'}).json()['id']
    draft = tool(client, base, 'workflow_draft').json()
    operations = [
        {'op': 'replace_workflow', 'data': {'workflow': {'nodes': [node('call', 'tool', tool_name='workflow:'+other)], 'edges': []}}},
        {'op': 'add_node', 'data': {'node': node('call', 'tool', tool_name='workflow:'+other)}}]
    for operation in operations:
        response = tool(client, base, 'workflow_draft', batch=batch(draft, operation))
        assert response.status_code == 422
        assert tool(client, base, 'workflow_draft').json()['revision'] == draft['revision']
    assert tool(client, base, 'workflow_draft', workflow_id=other).status_code == 422
    assert tool(client, base, 'workflow_draft', batch=batch(draft, {'op':'set_metadata', 'data':{'requirement':'更新用途'}})).status_code == 200


def test_item_patch_preserves_other_items_answers_and_requires_current_revision(configured):
    client, _, pid, base = prepare(configured)
    p = client.get(base+'/progress').json()
    p['value']['items'].append(item('supplier', status='waiting', questions=[{
        'id': 'endpoint', 'text': '供应商接口？', 'impact': '同步价格', 'next_action': '连接接口', 'answer': '下周提供'}]))
    client.put(base+'/progress', json={'expected_revision': p['revision'], 'value': p['value']})
    summary = tool(client, base, 'project_progress').json()
    assert 'value' not in summary and summary['view'] == 'summary'
    other = tool(client, base, 'project_progress', item_id='supplier').json()['item']
    result = tool(client, base, 'project_progress', action='patch', item_id='quantity', expected_revision=summary['revision'],
        changes={'deliverable': '返回修正数量', 'completion_criteria': ['真实输入返回2'], 'summary': '已修复'})
    assert result.status_code == 200, result.text
    assert tool(client, base, 'project_progress', item_id='supplier').json()['item'] == other
    assert tool(client, base, 'project_progress', action='patch', item_id='quantity',
        expected_revision=summary['revision'], changes={'summary': '过期覆盖'}).status_code == 409
    revision = result.json()['revision']
    answered = deepcopy(other['questions']); answered[0]['answer'] = 'Agent改写'
    assert tool(client, base, 'project_progress', action='patch', item_id='supplier', expected_revision=revision,
        changes={'questions': answered}).status_code == 200
    assert tool(client, base, 'project_progress', item_id='supplier').json()['item']['questions'][0]['answer'] == '下周提供'
    full = tool(client, base, 'project_progress', view='full').json()
    assert full == client.get(base+'/progress').json()
    created = tool(client, base, 'project_progress', action='patch', item_id='new', expected_revision=full['revision'],
        changes={'title': '后续优化', 'goal': '优化说明', 'next_action': '下次交办时改进', 'status': 'paused'})
    assert created.status_code == 200
    assert tool(client, base, 'project_progress', action='patch', expected_revision=created.json()['revision'], changes={'items': []}).status_code == 422


def test_native_loop_repairs_real_failure_and_regresses_without_customer_coaching(configured, monkeypatch):
    contexts, results = [], []
    class Repairing(TestSession):
        async def turn(self, message, on_event, on_tool, **kwargs):
            context = json.loads(message); contexts.append(context); self.turns += 1
            await on_tool('project_action', {'action': 'build', 'item_id': 'allocate',
                'deliverable': '返回申请数量', 'completion_criteria': ['数量为2', '原失败任务保留']})
            draft = await on_tool('workflow_draft', {})
            await on_tool('workflow_draft', {'batch': batch(draft,
                {'op': 'replace_workflow', 'data': {'workflow': {'nodes': [node('s','start'),
                    node('e','end',outputs={'quantity':ref('s','missing')})], 'edges':[edge('s','e')]}}},
                {'op':'add_test','data':{'test':{'id':'quantity','name':'数量','requirement':'返回2','inputs':{},
                    'assertions':[{'path':['quantity'],'operator':'equals','expected':2}]}}})})
            failed = await on_tool('workflow_run', {'action': 'start'})
            assert failed['status'] == 'failed' and failed['error']
            results.append(failed)
            draft = await on_tool('workflow_draft', {})
            exact = await on_tool('workflow_draft', {'view':'nodes','node_ids':['e']})
            assert exact['nodes'][0]['config']['outputs']['quantity']['$ref']['path'] == ['missing']
            await on_tool('workflow_draft', {'batch':batch(draft, update('e',config={'outputs':{'quantity':2}}),key='repair')})
            checks = await on_tool('workflow_run', {'action':'tests'})
            assert checks['passed'] and not checks['failed_tests']
            trial = await on_tool('project_action', {'action':'trial','item_id':'allocate','inputs':{},'feedback_task_id':results[0]['id']})
            assert trial['outputs'] == {'quantity':2}; results.append(trial)
            await on_tool('project_task_result', {'status':'succeeded','message':'数量为2，已修复实际错误'})
            progress = await on_tool('project_progress', {})
            await on_tool('project_progress', {'action':'patch','item_id':'allocate','expected_revision':progress['revision'],
                'changes':{'status':'done','availability':'trial','summary':'本次条件已满足','next_action':'',
                           'results':[{'label':'修复后的试用','task_id':trial['id']}]}})
            await on_tool('project_action', {'action':'finish'})
            return {'status':'completed'}
    client, app, project, _, base = configure_agent(configured, monkeypatch, Repairing)
    waiting = item('supplier', status='waiting', blocker={'kind':'data','owner':'供应商','reason':'缺接口','next_action':'补接口后连接'})
    put_progress(client, base, [item(workflow_ids=[project['id']]), waiting])
    irrelevant = client.post(base+'/members',json={'name':'无关测试','purpose':'test'}).json()['id']
    graph(client, irrelevant, [node('s','start'),node('e','end',outputs={'unrelated':'secret-detail'*1000})],[edge('s','e')])
    client.post(base+'/conversation/messages',json={'message':'完成数量处理；供应商同步等待接口','item_id':'allocate'})
    state = agent_settled(client,base)
    assert state['status'] == 'idle', state['error']
    assert len(contexts) == 1 and len([e for e in state['events'] if e['kind']=='user']) == 1
    assert 'secret-detail' not in json.dumps(contexts) and 'confirmed_requirements' not in contexts[0]
    assert len(contexts[0]['workflows']) == 1
    assert client.get(base+'/tasks/'+results[0]['id']).json()['status']=='failed'
    detail = client.get(base+'/tasks/'+results[1]['id']).json()
    assert detail['feedback_task_id']==results[0]['id'] and detail['outputs']=={'quantity':2}
    progress = client.get(base+'/progress').json()['value']['items']
    assert progress[0]['delivery_request_id']==state['request_id'] and progress[0]['status']=='done'
    assert progress[1]['status']=='waiting' and progress[1]['blocker']['reason']=='缺接口'
    metrics = client.get(base+'/conversation/metrics').json()['requests'][0]
    assert metrics['agent_turns']==1 and metrics['tool_failures']==1
    assert metrics['first_presented_task_id']==results[1]['id']


def test_metrics_measure_real_bytes_and_parallel_time_without_guessing_legacy_results():
    def event(kind, time, **kw):
        return {'id':str(time)+kind,'kind':kind,'request_id':'r','time':f'2026-09-14T00:00:{time:02d}+00:00',**kw}
    measured = payload_measurement({'中文':'相同资料'})
    rows=[event('user',0,text='开始'),event('tool_started',1,operation_id='a',text='project_file',input_bytes=10)]
    for id, start, end in [('a',1,5),('b',3,8)]:
        rows.append(event('tool',end,operation_id=id,text='project_file',started_at=f'2026-09-14T00:00:0{start}+00:00',
            ended_at=f'2026-09-14T00:00:0{end}+00:00',success=True,input_bytes=10,
            output_bytes=measured['bytes'],output_sha256=measured['sha256'],read_call=True))
    rows += [event('tool',9,text='old_tool',result='truncated...'),event('result',10,task_id='t',purpose='customer_trial')]
    summary=session_metrics(rows)['requests'][0]
    assert summary['tool_calls']==3 and summary['tool_wall_seconds']==7
    assert summary['identical_read_responses']==1 and summary['identical_read_bytes']==measured['bytes']
    assert summary['measured_output_bytes']==measured['bytes']*2 and summary['unmeasured_outputs']==1
    assert summary['first_presented_result_seconds']==10
    assert session_metrics(rows,'other')['requests']==[]
