"""Passive observations are grounded in real tasks, not model verdicts or click counts."""
import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid5, NAMESPACE_URL

import pytest

from agent_platform.connected_model import completion_events
from agent_platform.conversation_scope import conversation_scope
from agent_platform.db import connect
from agent_platform.usage_learning import patterns, digest
from agent_platform.usage_learning import AutomaticSettings
from tests.test_projects import configured, graph, node, edge, ref, start, settled  # noqa:F401
from tests.test_project_sessions import settled as session_settled
from tests.test_users import platform, signup, project  # noqa:F401
from tests.test_official_agent import official, enable, FakeAgent, wait  # noqa:F401

BASE = '/api/v1/admin/improvements'
ADMIN = {'Authorization': 'Bearer admin-boot'}


def test_result_tracks_stop_failure_and_followup_without_reusing_old_reply(official):
    client, app, service = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '处理进展')
    failed_runs(client, pid)
    item = client.post(BASE+'/scan').json()['items'][0]
    result_path = BASE+'/'+item['id']+'/result'
    assert client.get(result_path).status_code == 404
    enable(client, pid)
    FakeAgent.hold = True
    started = client.post(BASE+'/'+item['id']+'/start').json()
    cid = started['conversation_id']
    convo = f'/api/v1/projects/{pid}/conversations/{cid}'
    wait(client, convo, ADMIN, ('running',))
    running = client.get(result_path).json()
    # Official dispatch may enter the queue between the two HTTP reads.
    assert running['status'] in {'running', 'queued'} and running['reply'] is None
    assert client.post(convo+'/stop').status_code == 200
    assert client.get(result_path).json()['status'] == 'interrupted'
    FakeAgent.hold = False
    assert client.post(convo+'/messages', json={'message':'说明目前情况', 'request_key':'after-stop'}).status_code == 202
    wait(client, convo, ADMIN)
    completed = client.get(result_path).json()
    assert completed['status'] == 'idle' and completed['reply']
    # A later failed turn must not present the prior answer as this turn's result.
    async def fail(*args, **kwargs):
        raise ValueError('本轮连接失败，测试错误')
    with conversation_scope(pid, cid):
        app.state.services.local_agents.clients[app.state.services.local_agents.key(pid)].turn = fail
    assert client.post(convo+'/messages', json={'message':'再检查', 'request_key':'followup-error'}).status_code == 202
    wait(client, convo, ADMIN)
    failed = client.get(result_path).json()
    assert failed['status'] == 'error' and '本轮连接失败' in failed['error']
    assert failed['reply'] is None
    assert client.get(BASE).json()['items'][0]['status'] == 'working'
    # Lost/corrupt session data is a retryable read failure, never an empty success.
    with conversation_scope(pid, cid):
        state_file = app.state.services.local_agents.folder(pid)/'session.json'
    saved = state_file.read_text()
    state_file.write_text('{broken')
    try:
        assert client.get(result_path).status_code == 503
        assert client.get(BASE).status_code == 200
        state_file.unlink()
        assert client.get(result_path).status_code == 503
    finally:
        state_file.write_text(saved)


def failed_runs(client, pid):
    base = '/api/v1/projects/' + pid
    graph(client, pid, [node('start', 'start', inputs=[{'name': 'quantity', 'type': 'number', 'required': True}]),
                       node('end', 'end', outputs={'quantity': ref('$inputs', 'quantity')})], [edge('start', 'end')])
    tasks = [settled(client, base, start(client, base, key)) for key in ('one', 'two')]
    assert all(t['status'] == 'failed' for t in tasks)
    return tasks


def test_prepared_handoff_can_continue_from_project_chat_without_resending_brief(official):
    client, _, _ = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '直接接续保留的会话')
    failed_runs(client, pid)
    finding = client.post(BASE+'/scan').json()['items'][0]
    path = BASE+'/'+finding['id']
    assert client.post(path+'/start').status_code == 409
    handoff = client.get(BASE).json()['items'][0]['handoff']
    cid = handoff['conversation_id']
    convo = f'/api/v1/projects/{pid}/conversations/{cid}'
    assert client.get(path+'/result').json()['status'] == 'prepared'
    enable(client, pid)
    FakeAgent.hold = True
    assert client.post(convo+'/messages', json={
        'message': '连接已经配置，先查看失败原因', 'request_key': 'direct-project-message'}).status_code == 202
    wait(client, convo, ADMIN, ('running',))
    progress = client.get(path+'/result').json()
    assert progress['status'] in {'queued', 'running'} and not progress['error']
    # A stale retry button must not append the original brief to this live turn.
    for _ in range(2):
        resumed = client.post(path+'/start')
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()['conversation_id'] == cid
    state = client.get(convo).json()
    assert [e['text'] for e in state['events'] if e['kind'] == 'user'] == ['连接已经配置，先查看失败原因']
    assert client.post(convo+'/stop').status_code == 200
    assert client.get(path+'/result').json()['status'] == 'interrupted'
    FakeAgent.hold = False
    assert client.post(convo+'/messages', json={
        'message': '继续说明', 'request_key': 'continue-after-stop'}).status_code == 202
    wait(client, convo, ADMIN)
    progress = client.get(path+'/result').json()
    assert progress['status'] == 'idle' and progress['reply']
    turns = len(FakeAgent.turns)
    assert client.post(path+'/start').json()['conversation_id'] == cid
    assert len(FakeAgent.turns) == turns
    assert len(client.get(f'/api/v1/projects/{pid}/conversations').json()) == 1


def test_actual_failures_repair_lineage_reuse_and_stable_ids(configured):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    failures = failed_runs(client, pid)
    first = client.post(BASE+'/scan').json()
    error = next(x for x in first['items'] if x['kind'] == 'repeated_failure')
    assert error['count'] == 2 and len(error['tasks']) == 2
    assert all(t['revision'] == 1 for t in error['tasks'])
    # Duplicate HTTP delivery reuses the task, not an additional failure.
    assert start(client, base, 'one')['id'] == failures[0]['id']
    client.patch(BASE+'/'+error['id'], json={'status': 'dismissed'})
    assert client.post(BASE+'/scan').json()['items'][0]['status'] == 'dismissed'
    graph(client, pid, [node('start', 'start', inputs=[{'name':'quantity', 'type':'number', 'default':1}]),
                       node('end', 'end', outputs={'quantity':ref('start','quantity')})], [edge('start','end')])
    success = settled(client, base, start(client, base, 'fixed', feedback_task_id=failures[0]['id']))
    assert success['status'] == 'succeeded' and success['outputs'] == {'quantity':1}
    for quantity in (2, 3):
        assert settled(client, base, start(client, base, 'new-'+str(quantity), inputs={'quantity':quantity}, purpose='business'))['status'] == 'succeeded'
    result = client.post(BASE+'/scan').json()
    kinds = {r['kind'] for r in result['items']}
    assert kinds == {'repeated_failure', 'recovery', 'reusable_method'}
    repair = next(r for r in result['items'] if r['kind'] == 'recovery')
    assert repair['workflow_changed'] and not repair['inputs_changed']
    assert [t['revision'] for t in repair['tasks']] == [1, 2]
    assert client.get(base+'/tasks/'+failures[0]['id']).json()['status'] == 'failed'
    brief = client.get(BASE+'/'+repair['id']+'/brief')
    assert brief.status_code == 200 and failures[0]['id'] in brief.text and '因果关系' in brief.text
    assert 'attachment;' in brief.headers['content-disposition']
    assert not app.state.services.local_agents.tasks


def test_same_inputs_tests_stops_and_unrelated_success_do_not_imply_reuse_or_repair():
    base = dict(project_id='p', workflow_id='w', project_name='P', workflow_name='W',
                created_at='2026-09-28', updated_at='2026-09-28', revision=1,
                content_hash='v1', input_hash='one', error_hash='error', error_kind='错误', feedback_task_id='')
    rows = [{**base, 'id':str(i), 'status':'succeeded', 'purpose':'business'} for i in range(4)]
    assert patterns(rows, []) == []
    for r in rows:
        r.update(purpose='build_test', input_hash=r['id'])
    assert patterns(rows, []) == []
    rows += [{**base, 'id':'stopped', 'status':'interrupted', 'purpose':'business'},
             {**base, 'id':'waiting', 'status':'waiting_input', 'purpose':'business'},
             {**base, 'id':'failed', 'status':'failed', 'purpose':'business'}]
    assert patterns(rows, []) == []


def nested_quantity(client, pid):
    child = client.post('/api/v1/projects/'+pid+'/members', json={'name': '数量子流程'}).json()['id']
    graph(client, child, [node('start', 'start', inputs=[{'name':'quantity', 'type':'number', 'required':True}]),
        node('end', 'end', outputs={'quantity':ref('start', 'quantity')})], [edge('start', 'end')])
    graph(client, pid, [node('start', 'start'), node('call', 'tool', tool_name='workflow:'+child, input={}),
        node('end', 'end', outputs={'quantity':ref('call', 'output', 'quantity')})], [edge('start', 'call'), edge('call', 'end')])
    return child


def test_child_fix_marks_old_failures_historical_and_records_real_change(configured):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/'+pid
    child = nested_quantity(client, pid)
    before = client.get('/api/v1/applications/'+pid+'/draft').json()['content_hash']
    failures = [settled(client, base, start(client, base, 'nested-'+str(i))) for i in range(2)]
    assert all(t['status'] == 'failed' for t in failures)
    item = client.post(BASE+'/scan').json()['items'][0]
    assert item['automatic_eligible']
    graph(client, child, [node('start', 'start'), node('end', 'end', outputs={'quantity':1})], [edge('start', 'end')])
    fixed = settled(client, base, start(client, base, 'fixed-child', feedback_task_id=failures[0]['id']))
    assert fixed['status'] == 'succeeded' and fixed['outputs'] == {'quantity':1}
    assert client.get('/api/v1/applications/'+pid+'/draft').json()['content_hash'] == before
    items = client.post(BASE+'/scan').json()['items']
    original = next(x for x in items if x['id'] == item['id'])
    assert not original['automatic_eligible'] and '旧版本' in original['limitation']
    recovery = next(x for x in items if x['kind'] == 'recovery')
    assert recovery['workflow_changed'] and not recovery['inputs_changed']
    assert '子流程' in recovery['explanation']


def test_reuse_separates_child_versions_but_ignores_unreferenced_members(configured):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/'+pid
    child = nested_quantity(client, pid)
    graph(client, child, [node('start', 'start'), node('end', 'end', outputs={'quantity':1})], [edge('start', 'end')])
    for i in range(2):
        task = settled(client, base, start(client, base, 'old-'+str(i), inputs={'batch':i}))
        assert task['status'] == 'succeeded'
    unused = client.post(base+'/members', json={'name':'无关流程'}).json()['id']
    graph(client, unused, [node('start', 'start'), node('end', 'end', outputs={'quantity':999})], [edge('start', 'end')])
    assert settled(client, base, start(client, base, 'old-2', inputs={'batch':2}))['status'] == 'succeeded'
    old = next(x for x in client.post(BASE+'/scan').json()['items'] if x['kind'] == 'reusable_method')
    assert old['count'] == 3
    graph(client, child, [node('start', 'start'), node('end', 'end', outputs={'quantity':2})], [edge('start', 'end')])
    for i in range(3):
        assert settled(client, base, start(client, base, 'new-'+str(i), inputs={'batch':i}))['outputs'] == {'quantity':2}
    items = [x for x in client.post(BASE+'/scan').json()['items'] if x['kind'] == 'reusable_method' and x['active']]
    assert len(items) == 2 and {x['count'] for x in items} == {3}
    assert old['id'] in {x['id'] for x in items}


@pytest.mark.parametrize('kind', ['tool', 'tool_executor', 'soft_block'])
@pytest.mark.parametrize('container', ['iteration', 'loop'])
def test_nested_graph_calls_are_versioned_without_reading_config_prose(configured, kind, container):
    client, _, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/'+pid
    child = nested_quantity(client, pid)
    unrelated = client.post(base+'/members', json={'name':'仅在说明中提及'}).json()['id']
    if kind == 'tool':
        call = node('call', kind, tool_name='workflow:'+child, input={})
    else:
        call = node('call', kind, settings={'tool_name':'workflow:'+child, 'tool_input':{}},
                    **({'strategy':'tool_execute'} if kind == 'soft_block' else {}))
    inner = {'nodes':[node('s','start'), call, node('e','end')], 'edges':[edge('s','call'),edge('call','e')]}
    settings = {'items':[1]} if container == 'iteration' else {
        'max_iterations':1, 'break_value':True, 'break_condition':{'value':True,'operator':'equals','expected':True}}
    graph(client, pid, [node('s','start'), node('repeat',container,workflow=inner,output_node_id='e',**settings),
        node('e','end',outputs={'documentation':'workflow:'+unrelated})], [edge('s','repeat'),edge('repeat','e')])
    for i in range(2):
        task = settled(client, base, start(client, base, 'nested-'+str(i)))
        assert task['status'] == 'failed' and 'quantity' in task['error']
    first = client.post(BASE+'/scan').json()['items'][0]
    assert set(first['workflow_versions']) == {pid, child} and first['automatic_eligible']
    graph(client, unrelated, [node('s','start'),node('e','end')], [edge('s','e')])
    assert client.post(BASE+'/scan').json()['items'][0]['automatic_eligible']
    graph(client, child, [node('s','start'),node('e','end')], [edge('s','e')])
    assert not client.post(BASE+'/scan').json()['items'][0]['automatic_eligible']


def test_dynamic_target_uses_available_member_versions_and_rechecks_before_dispatch(official):
    client, app, _ = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '动态子流程')
    base = '/api/v1/projects/'+pid
    child = nested_quantity(client, pid)
    graph(client, pid, [node('s','start'),node('call','tool_executor',input=ref('$inputs','request')),
        node('e','end')], [edge('s','call'),edge('call','e')])
    inputs = {'request':{'tool_calls':[{'tool_name':'workflow:'+child,'tool_input':{}}]}}
    for i in range(2):
        task = settled(client, base, start(client, base, 'routed-'+str(i), inputs=inputs))
        assert task['status'] == 'failed'
    item = client.post(BASE+'/scan').json()['items'][0]
    assert set(item['workflow_versions']) == {pid, child} and '动态' in item['limitation']
    enable(client, pid)
    assert client.put(BASE+'/settings', json={'enabled':True,'project_ids':[pid],'daily_limit':1}).status_code == 200
    graph(client, child, [node('s','start'),node('e','end')], [edge('s','e')])
    # Do not scan again: dispatch must check changes since the last observation.
    client.portal.call(app.state.services.usage_learning.automate)
    assert client.get(base+'/conversations').json() == []
    assert not FakeAgent.turns


@pytest.mark.parametrize('status', ['resolved', 'dismissed', 'new'])
def test_old_combined_signal_keeps_handling_history_when_split_and_future_versions_are_new(official, status):
    client, app, _ = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '旧线索升级')
    base = '/api/v1/projects/'+pid
    child = nested_quantity(client, pid)
    failures = []
    for version in range(2):
        graph(client, child, [node('s','start',inputs=[{'name':'quantity','type':'number','required':True}]),
            node('e','end',outputs={'version':version})], [edge('s','e')])
        for i in range(2):
            failures.append(settled(client, base, start(client, base, f'old-{version}-{i}')))
    assert all(t['status']=='failed' for t in failures)
    assert len({t['error'] for t in failures}) == 1
    items = client.post(BASE+'/scan').json()['items']
    assert len(items) == 2
    root_hash = client.get('/api/v1/applications/'+pid+'/draft').json()['content_hash']
    legacy_id = str(uuid5(NAMESPACE_URL, 'lilies:usage:failure:'+pid+':'+pid+':'+root_hash+':'+digest(failures[0]['error'])))
    legacy = {**items[0], 'id':legacy_id, 'last_seen':max(t['updated_at'] for t in failures)}
    legacy.pop('workflow_versions', None)
    response = client.post(base+'/conversations',json={'title':'旧处理会话'})
    assert response.status_code == 201, response.text
    cid = response.json()['id']
    with connect(app.state.services.usage_learning.db) as db:
        db.execute('DELETE FROM usage_findings')
        db.execute('INSERT INTO usage_findings VALUES(?,?,?,?,?,?,?,?)',
            (legacy_id,pid,'repeated_failure',status,json.dumps(legacy),time.time(),time.time(),1))
        db.execute('INSERT INTO usage_handoffs VALUES(?,?,?,?,?,?,?)',
            (legacy_id,'root',cid,'started','',1,time.time()))
    # A fresh failure of an already observed child version is still that same
    # signal. It must not erase its previous handling during the first upgrade scan.
    assert settled(client, base, start(client, base, 'same-version-again'))['status']=='failed'
    migrated = [x for x in client.post(BASE+'/scan').json()['items'] if x['active']]
    assert len(migrated)==2 and {x['status'] for x in migrated}=={status}
    assert {x['handoff']['conversation_id'] for x in migrated}=={cid}
    enable(client, pid)
    assert client.put(BASE+'/settings', json={'enabled':True,'project_ids':[pid],'daily_limit':2}).status_code == 200
    client.portal.call(app.state.services.usage_learning.automate)
    assert len(client.get(base+'/conversations').json()) == 1 and not FakeAgent.turns
    # A later genuine child version must not inherit a past dismissal or attempt.
    graph(client, child, [node('s','start',inputs=[{'name':'quantity','type':'number','required':True}]),
        node('e','end',outputs={'version':3})], [edge('s','e')])
    for i in range(2):
        assert settled(client, base, start(client, base, 'future-'+str(i)))['status']=='failed'
    current = next(x for x in client.post(BASE+'/scan').json()['items'] if x['active'] and x['automatic_eligible'])
    assert current['status']=='new' and current['handoff'] is None
    client.portal.call(app.state.services.usage_learning.automate)
    # The shared old handling conversation counts only once against the daily limit.
    assert len(client.get(base+'/conversations').json()) == 2
    started = next(x for x in client.get(BASE).json()['items'] if x['id']==current['id'])
    assert started['handoff']['conversation_id'] != cid


def test_http_errors_permissions_and_no_request_or_conversation_bodies(platform):
    client, app = platform
    _, owner = signup(client, 'Alice'); _, other = signup(client, 'Bob')
    pid = project(client, owner)
    url = f'/api/v1/applications/{pid}/draft'
    revision = client.get(url, headers=owner).json()['revision']
    body = {'expected_revision':revision, 'idempotency_key':'initial', 'op':'set_metadata',
            'data':{'description':'PRIVATE REQUEST BODY'}}
    assert client.post(url, headers=owner, json=body).status_code == 200
    for i in range(2):
        rejected = client.post(url, headers=owner, json={**body, 'idempotency_key':'stale-'+str(i), 'data':{'description':'PRIVATE REVISED BODY'}})
        assert rejected.status_code == 409, rejected.text
    assert client.post(url, headers=other, json=body).status_code == 404
    app.state.services.local_agents.event(pid, 'user', 'PRIVATE CONVERSATION BODY')
    result = client.post(BASE+'/scan', headers=ADMIN)
    assert result.status_code == 200
    item = result.json()['items'][0]
    assert item['kind'] == 'operation_error' and item['count'] == 2
    assert item['operations'][0]['resource_id'] == pid
    assert 'PRIVATE' not in result.text
    for path, method in [(BASE,'get'),(BASE+'/scan','post'),(BASE+'/'+item['id']+'/brief','get'),
                         (BASE+'/'+item['id']+'/start','post')]:
        assert getattr(client, method)(path, headers=owner).status_code == 403
        assert getattr(client, method)(path).status_code == 401
    assert client.patch(BASE+'/'+item['id'], headers=ADMIN, json={'status':'fixed'}).status_code == 422
    assert client.post(BASE+'/nonexistent/start', headers=ADMIN).status_code == 404


def test_scanner_failure_never_blocks_user_work_and_old_counter_migration(platform, monkeypatch):
    client, app = platform
    _, owner = signup(client, 'Alice'); pid = project(client, owner)
    learning = app.state.services.usage_learning
    def fail():
        raise RuntimeError('PRIVATE SQL ERROR')
    monkeypatch.setattr(learning, '_scan', fail)
    response = client.post(BASE+'/scan', headers=ADMIN)
    assert response.status_code == 200 and response.json()['error'] and 'PRIVATE' not in response.text
    assert client.get('/api/v1/projects/'+pid, headers=owner).status_code == 200
    with connect(learning.db) as db:
        db.execute('ALTER TABLE product_usage DROP COLUMN resource_id')
    app.state.services.product_usage.initialize()
    app.state.services.product_usage.initialize()
    with connect(learning.db) as db:
        assert 'resource_id' in {r['name'] for r in db.execute('PRAGMA table_info(product_usage)')}


def test_handoff_retries_same_private_conversation_without_duplicate_model_work(configured, monkeypatch):
    client, app, project, _ = configured
    pid = project['id']; base = '/api/v1/projects/' + pid
    failed_runs(client, pid)
    item = client.post(BASE+'/scan').json()['items'][0]
    path = BASE+'/'+item['id']+'/start'
    assert client.post(path).status_code == 409  # No project model yet; keep one prepared conversation.
    first = client.get(BASE).json()['items'][0]['handoff']
    assert first['status'] == 'prepared'
    progress = client.get(BASE+'/'+item['id']+'/result').json()
    assert progress['status'] == 'prepared' and progress['error']
    assert progress['reply'] is None and progress['total_tasks'] == 0
    assert client.post(path).status_code == 409
    assert len(client.get(base+'/conversations').json()) == 1
    calls = []
    async def stream(**kwargs):
        calls.append(1)
        for event in completion_events([{'type':'text', 'text':'已检查线索；这是离线验证回答。'}], stop_reason='end_turn'):
            yield event
    assert client.put(base+'/agent-session', json={'provider':'api', 'model':'test',
               'base_url':'https://example.test/v1', 'api_key':'PRIVATE KEY'}).status_code == 200
    monkeypatch.setattr(app.state.services.local_agents.connections, 'provider', lambda *a, **kw: SimpleNamespace(stream=stream))
    sent = client.post(path)
    assert sent.status_code == 200, sent.text
    cid = sent.json()['conversation_id']
    state = session_settled(client, base+'/conversations/'+cid)
    assert len(calls) == 1
    assert any(e['kind'] == 'assistant' for e in state['events'])
    assert client.post(path).json()['conversation_id'] == cid
    assert len(calls) == 1 and len(client.get(base+'/conversations').json()) == 1
    # A restart/lost final response is detected by the actual sent request, not a second model turn.
    with connect(app.state.services.usage_learning.db) as db:
        db.execute("UPDATE usage_handoffs SET status='prepared'")
    assert client.get(BASE+'/'+item['id']+'/result').json()['status'] == 'idle'
    assert client.post(path).status_code == 200
    assert len(calls) == 1
    assert client.get(BASE).json()['items'][0]['status'] == 'working'
    progress = client.get(BASE+'/'+item['id']+'/result').json()
    assert progress['status'] == 'idle'
    assert progress['reply']['text'] == '已检查线索；这是离线验证回答。'
    assert progress['tasks'] == [] and len(calls) == 1
    assert '已检查线索' not in client.get(BASE).text  # No transcript in passive report.
    assert client.patch(BASE+'/'+item['id'], json={'status':'resolved'}).status_code == 200
    assert client.post(BASE+'/scan').json()['items'][0]['status'] == 'resolved'
    assert len(calls) == 1  # Scanning/marking completed never calls the model.
    # Another administrator has an independent conversation; regular users cannot access this one.
    _, member = signup(client, 'employee')
    client.post(base+'/access-members', json={'name':'employee'})
    assert client.get(base+'/conversations/'+cid, headers=member).status_code == 404
    assert client.get(BASE+'/'+item['id']+'/result', headers=member).status_code == 403
    # Even another administrator cannot read this private handling conversation.
    with connect(app.state.services.usage_learning.db) as db:
        db.execute("UPDATE users SET role='admin' WHERE name='employee'")
    assert client.get(BASE+'/'+item['id']+'/result', headers=member).status_code == 404
    assert client.get(BASE, headers=member).json()['items'][0]['handoff'] is None


def test_official_agent_handoff_can_create_fix_and_run_copy(official):
    """The agent is scripted offline; copies, edits and both calculations are real."""
    client, app, service = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '使用改进验收')
    failures = failed_runs(client, pid)
    original = client.get(f'/api/v1/applications/{pid}/draft').json()
    item = client.post(BASE+'/scan').json()['items'][0]
    performed = []

    class RepairAgent(FakeAgent):
        async def turn(self, message, on_event, on_tool, **kwargs):
            assert failures[0]['id'] in message
            before = await on_tool('workflow_draft', {'workflow_id':pid})
            copy = await on_tool('project_workflows', {'action':'create', 'name':'修复试用副本', 'purpose':'test'})
            wid = copy['id']
            blank = await on_tool('workflow_draft', {'workflow_id':wid})
            workflow = {'nodes':[node('start','start',inputs=[{'name':'quantity','type':'number','default':1}]),
                                  node('end','end',outputs={'quantity':ref('start','quantity')})],
                        'edges':[edge('start','end')]}
            edited = await on_tool('workflow_draft', {'workflow_id':wid, 'operation':{
                'op':'replace_workflow','expected_revision':blank['revision'],'idempotency_key':'repair-copy',
                'data':{'workflow':workflow}}})
            for values in ({}, {'quantity':4}):
                run = await on_tool('workflow_run', {'action':'start','workflow_id':wid,'inputs':values,'wait':True})
                assert run['status'] == 'succeeded', run
                performed.append(run['outputs'])
            assert before['revision'] == original['revision']
            await on_event('item/completed', {'item':{'type':'agentMessage','text':'独立副本已修复，并通过原输入和变化输入。原流程保持。'}})
            return {'status':'completed'}

    service.client_factory = RepairAgent
    enable(client, pid)
    result = client.post(BASE+'/'+item['id']+'/start')
    assert result.status_code == 200, result.text
    cid = result.json()['conversation_id']
    state = wait(client, f'/api/v1/projects/{pid}/conversations/{cid}', ADMIN)
    assert state['status'] == 'idle', state.get('error')
    assert performed == [{'quantity':1}, {'quantity':4}]
    assert client.get(f'/api/v1/applications/{pid}/draft').json()['content_hash'] == original['content_hash']
    assert client.get(f'/api/v1/projects/{pid}/tasks/'+failures[0]['id']).json()['status'] == 'failed'
    assert client.post(BASE+'/'+item['id']+'/start').json()['conversation_id'] == cid
    assert len(performed) == 2
    progress = client.get(BASE+'/'+item['id']+'/result').json()
    assert progress['status'] == 'idle' and '独立副本' in progress['reply']['text']
    assert progress['total_tasks'] == 2 and len(progress['tasks']) == 2
    assert {t['status'] for t in progress['tasks']} == {'succeeded'}
    assert all(t['mode'] == 'workflow' and t['workflow_available'] is True and t['workflow_name'] == '修复试用副本'
               for t in progress['tasks'])
    assert not {t['id'] for t in progress['tasks']} & {t['id'] for t in failures}
    assert client.get(BASE).json()['items'][0]['status'] == 'working'
    # Membership, not the continued existence of an application, makes the editor link valid.
    wid = progress['tasks'][0]['workflow_id']
    other_pid = project(client, ADMIN, '其他项目')
    with connect(app.state.services.usage_learning.db) as db:
        db.execute('UPDATE project_members SET project_id=? WHERE application_id=?', (other_pid, wid))
    historical = client.get(BASE+'/'+item['id']+'/result').json()
    assert [t['id'] for t in historical['tasks']] == [t['id'] for t in progress['tasks']]
    assert all(t['workflow_available'] is False and t['workflow_name'] == '' for t in historical['tasks'])
    for task in historical['tasks']:
        saved = client.get(f'/api/v1/projects/{pid}/tasks/'+task['id']).json()
        assert saved['status'] == 'succeeded' and saved['outputs']['quantity'] in (1, 4)


def test_automatic_dispatch_is_opt_in_official_only_bounded_and_persistent(official, monkeypatch):
    client, app, service = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '自动处理验收')
    failed_runs(client, pid)
    learning = app.state.services.usage_learning
    client.post(BASE+'/scan')
    assert not client.get(BASE+'/settings').json()['enabled']
    asyncio.run(learning.automate())
    assert not client.get(f'/api/v1/projects/{pid}/conversations').json()
    body = {'enabled':True, 'project_ids':[pid], 'daily_limit':1}
    assert client.put(BASE+'/settings', json=body).status_code == 409
    assert client.put(BASE+'/settings', json={**body,'project_ids':[]}).status_code == 422
    enable(client, pid)
    assert client.put(BASE+'/settings', json=body).status_code == 200
    # Employee work takes priority over proactive maintenance.
    monkeypatch.setattr(service, 'active_jobs', lambda: [{'status':'running'}])
    asyncio.run(learning.automate())
    assert not client.get(f'/api/v1/projects/{pid}/conversations').json()
    monkeypatch.setattr(service, 'active_jobs', lambda: [])
    calls = []
    async def reject(*args, **kwargs):
        calls.append(1)
        raise ValueError('offline connection failure')
    monkeypatch.setattr(app.state.services.projects.conversation, 'send', reject)
    asyncio.run(learning.automate())
    asyncio.run(learning.automate())
    learning.initialize()  # Restart preserves the consumed attempt; no endless paid retries.
    asyncio.run(learning.automate())
    assert len(calls) == 1
    item = client.get(BASE).json()['items'][0]
    assert item['handoff']['automatic'] == 1 and item['handoff']['error']
    assert client.put(BASE+'/settings', json={**body,'enabled':False}).status_code == 200
    assert len(client.get(f'/api/v1/projects/{pid}/conversations').json()) == 1


def test_automation_uses_existing_official_agent_and_excludes_its_own_tasks(official):
    client, app, service = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '后台自动处理')
    failed_runs(client, pid)
    enable(client, pid)
    client.post(BASE+'/scan')
    assert client.put(BASE+'/settings', json={'enabled':True,'project_ids':[pid],'daily_limit':1}).status_code == 200
    learning = app.state.services.usage_learning
    # TestClient portal keeps the existing worker/event loop alive until the fake agent finishes.
    client.portal.call(learning.automate)
    item = client.get(BASE).json()['items'][0]
    assert item['handoff']['status'] == 'started'
    cid = item['handoff']['conversation_id']
    state = wait(client, f'/api/v1/projects/{pid}/conversations/{cid}', ADMIN)
    assert state['status'] == 'idle'
    assert len(FakeAgent.turns) == 1
    with conversation_scope(pid, cid):
        for i in range(2):
            task, _ = asyncio.run(app.state.services.projects.store.create_task('self-'+str(i), pid,
                'self-key-'+str(i), 'workflow', pid, {}, '', asyncio.run(app.state.services.projects.freeze(pid))))
            asyncio.run(app.state.services.projects.store.update_task(task['id'], status='failed', error='a different error'))
    assert len(client.post(BASE+'/scan').json()['items']) == 1
    client.portal.call(learning.automate)
    assert len(FakeAgent.turns) == 1


def test_resumed_failure_is_historical_and_disabling_while_waiting_stops_dispatch(official):
    client, app, service = official
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '接续与关闭')
    failures = failed_runs(client, pid)
    enable(client, pid)
    item = client.post(BASE+'/scan').json()['items'][0]
    assert client.put(BASE+'/settings', json={'enabled':True,'project_ids':[pid],'daily_limit':1}).status_code == 200
    learning = app.state.services.usage_learning
    async def race():
        await learning.handoff_lock.acquire()
        waiting = asyncio.create_task(learning.automate())
        await asyncio.sleep(.02)
        with connect(learning.db) as db:
            value = learning.config(); value['enabled'] = False
            db.execute('UPDATE usage_learning_settings SET value=? WHERE id=1', (json.dumps(value),))
        learning.handoff_lock.release()
        await waiting
    client.portal.call(race)
    assert not FakeAgent.turns
    assert not client.get(f'/api/v1/projects/{pid}/conversations').json()
    # Existing stores mutate the status on resumption; obsolete observations must not stay actionable.
    asyncio.run(app.state.services.projects.store.update_task(failures[0]['id'], status='succeeded'))
    updated = client.post(BASE+'/scan').json()['items'][0]
    assert updated['id'] == item['id'] and not updated['active']
    assert client.put(BASE+'/settings', json={'enabled':True,'project_ids':[pid],'daily_limit':1}).status_code == 200
    client.portal.call(learning.automate)
    assert not FakeAgent.turns


def test_revoked_project_does_not_starve_another_and_api_is_never_automatic(official):
    client, app, service = official
    client.headers.update(ADMIN)
    projects = [project(client, ADMIN, name) for name in ('较早失效项目','仍有效项目')]
    for pid in projects:
        failed_runs(client, pid)
        enable(client, pid)
    client.post(BASE+'/scan')
    assert client.put(BASE+'/settings', json={'enabled':True,'project_ids':projects,'daily_limit':1}).status_code == 200
    client.put('/api/v1/projects/'+projects[0]+'/assistant-source', json={'allowed':True,'task':'api'})
    learning = app.state.services.usage_learning
    client.portal.call(learning.automate)
    rows = client.get(BASE).json()['items']
    absent = next(r for r in rows if r['project_id'] == projects[0])
    running = next(r for r in rows if r['project_id'] == projects[1])
    assert not absent['handoff'] and running['handoff']['status'] == 'started'
    wait(client, f'/api/v1/projects/{projects[1]}/conversations/'+running['handoff']['conversation_id'], ADMIN)
    assert len(FakeAgent.turns) == 1
