"""Passive observations are grounded in real tasks, not model verdicts or click counts."""
import asyncio
import json
from types import SimpleNamespace

from agent_platform.connected_model import completion_events
from agent_platform.conversation_scope import conversation_scope
from agent_platform.db import connect
from agent_platform.usage_learning import patterns
from agent_platform.usage_learning import AutomaticSettings
from tests.test_projects import configured, graph, node, edge, ref, start, settled  # noqa:F401
from tests.test_project_sessions import settled as session_settled
from tests.test_users import platform, signup, project  # noqa:F401
from tests.test_official_agent import official, enable, FakeAgent, wait  # noqa:F401

BASE = '/api/v1/admin/improvements'
ADMIN = {'Authorization': 'Bearer admin-boot'}


def failed_runs(client, pid):
    base = '/api/v1/projects/' + pid
    graph(client, pid, [node('start', 'start', inputs=[{'name': 'quantity', 'type': 'number', 'required': True}]),
                       node('end', 'end', outputs={'quantity': ref('$inputs', 'quantity')})], [edge('start', 'end')])
    tasks = [settled(client, base, start(client, base, key)) for key in ('one', 'two')]
    assert all(t['status'] == 'failed' for t in tasks)
    return tasks


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
    assert client.post(path).status_code == 200
    assert len(calls) == 1
    assert client.get(BASE).json()['items'][0]['status'] == 'working'
    # Another administrator has an independent conversation; regular users cannot access this one.
    _, member = signup(client, 'employee')
    client.post(base+'/access-members', json={'name':'employee'})
    assert client.get(base+'/conversations/'+cid, headers=member).status_code == 404


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
