import time
import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from tests.test_users import platform, signup, project  # noqa:F401
from agent_platform.project_store import connect

ADMIN={'Authorization':'Bearer admin-boot'}

def test_counters_deduplicate_separate_tools_and_keep_content_out(platform):
    client,app=platform
    user,a=signup(client,'Alice');pid=project(client,a)
    counter=app.state.services.product_usage
    values=dict(key='root',user_id=user['user']['id'],project_id=pid,root_id='request',feature='chat')
    counter.record(**values);counter.record(**values)
    counter.record(**{**values,'key':'tool','actor':'agent','feature':'project_file'})
    assert client.get('/api/v1/admin/usage',headers=a).status_code==403
    report=client.get('/api/v1/admin/usage',headers=ADMIN).json()
    assert report['active_users']==1
    assert len(report['features'])==2
    assert all(x['count']==1 and x['tokens'] is None for x in report['features'])
    with connect(counter.db) as db:
        db.execute('UPDATE product_usage SET created=?',(time.time()-31*86400,))
    report=client.get('/api/v1/admin/usage',headers=ADMIN).json()
    assert len(report['features'])==2
    with connect(counter.db) as db:
        assert db.execute('SELECT COUNT(*) FROM product_usage').fetchone()[0]==0


def test_saved_method_is_counted_without_its_body_and_feedback_is_private(platform):
    client,app=platform
    user,a=signup(client,'Alice');_,b=signup(client,'Bob');pid=project(client,a)
    base='/api/v1/projects/'+pid
    assert client.put(base+'/skills/method',headers=a,json={'name':'Safe method','content':'PRIVATE TEST BODY'}).status_code==200
    report=client.get('/api/v1/admin/usage',headers=ADMIN)
    assert 'PRIVATE TEST BODY' not in report.text
    assert any(r['feature']=='save_method' for r in report.json()['features'])
    assert client.put(base+'/conversations/legacy/feedback/absent',headers=a,json={'helpful':True}).status_code==404
    assert client.put(base+'/conversations/legacy/feedback/absent',headers=b,json={'helpful':True}).status_code==404


def test_workspace_download_and_application_edits_use_the_owning_project(platform):
    client,app=platform
    user,a=signup(client,'Alice');_,b=signup(client,'Bob');pid=project(client,a)
    uploaded=client.post(f'/api/v1/projects/{pid}/materials',headers=a,
                        files={'file':('private-note.txt',b'PRIVATE FILE BODY','text/plain')})
    assert uploaded.status_code==201,uploaded.text
    url=f'/api/v1/applications/{pid}/workspace/files/'+uploaded.json()['path']
    # File-reader fetches remain previews; explicit downloads are separate requests.
    assert client.get(url,headers=a).status_code==200
    assert client.get(url+'?download=1',headers=a).status_code==200
    assert client.get(url+'?download=1',headers=b).status_code==404
    assert client.get(f'/api/v1/applications/{pid}/workspace/files/absent?download=1',headers=a).status_code==404
    added=client.post(f'/api/v1/projects/{pid}/members',headers=a,json={'name':'Member flow'})
    assert added.status_code==201,added.text
    wid=added.json()['id'];draft_url=f'/api/v1/applications/{wid}/draft'
    revision=client.get(draft_url,headers=a).json()['revision']
    edit={'expected_revision':revision,'idempotency_key':'edit-usage','op':'set_metadata','data':{'description':'PRIVATE EDIT BODY'}}
    assert client.post(draft_url,headers=a,json=edit).status_code==200
    assert client.post(draft_url,headers=a,json={**edit,'idempotency_key':'stale-edit','data':{'description':'another edit'}}).status_code==409
    with connect(app.state.services.product_usage.db) as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM product_usage WHERE feature IN ('download','edit')")]
    assert len(rows)==2
    assert {r['feature'] for r in rows}=={'download','edit'}
    assert all(r['project_id']==pid and r['user_id']==user['user']['id'] and r['actor']=='employee' for r in rows)
    report=client.get('/api/v1/admin/usage',headers=ADMIN)
    assert 'PRIVATE FILE BODY' not in report.text and 'PRIVATE EDIT BODY' not in report.text


def test_counter_failure_does_not_break_download_or_save(platform,monkeypatch):
    client,app=platform
    _,a=signup(client,'Alice');pid=project(client,a)
    uploaded=client.post(f'/api/v1/projects/{pid}/materials',headers=a,files={'file':('note.txt',b'hello')}).json()
    def fail(**kwargs):raise RuntimeError('unavailable counters')
    monkeypatch.setattr(app.state.services.product_usage,'record',fail)
    r=client.get(f'/api/v1/applications/{pid}/workspace/files/'+uploaded['path']+'?download=1',headers=a)
    assert r.status_code==200 and r.content==b'hello'
    url=f'/api/v1/applications/{pid}/draft';draft=client.get(url,headers=a).json()
    assert client.post(url,headers=a,json={'expected_revision':draft['revision'],'idempotency_key':'edit-while-counter-down',
                       'op':'set_metadata','data':{'description':'still saved'}}).status_code==200
    assert client.get(url,headers=a).json()['snapshot']['description']=='still saved'


def test_unhandled_failures_become_content_free_improvement_signals(platform, monkeypatch):
    client, app = platform
    user, auth = signup(client, 'Alice'); _, outsider = signup(client, 'Bob')
    pid = project(client, auth)
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path, headers=auth).json()
    body = {'expected_revision':draft['revision'], 'idempotency_key':'failed-edit',
            'op':'set_metadata', 'data':{'description':'PRIVATE REQUEST BODY'}}

    async def fail(*args, **kwargs):
        raise RuntimeError('PRIVATE SERVER EXCEPTION')

    monkeypatch.setattr(app.state.services.applications, 'apply_operation', fail)
    # Use the already initialized app, preserving the real outer 500 handler.
    failed_client = TestClient(app, raise_server_exceptions=False)
    try:
        assert failed_client.post(path, headers=outsider, json=body).status_code == 404
        for _ in range(2):
            response = failed_client.post(path, headers=auth, json=body)
            assert response.status_code == 500 and 'PRIVATE' not in response.text
    finally:
        failed_client.close()
    with connect(app.state.services.product_usage.db) as db:
        events = [dict(r) for r in db.execute("SELECT * FROM product_usage WHERE feature='edit_error'")]
    assert len(events) == 2
    assert all(r['outcome'] == 'HTTP 500' and r['project_id'] == pid and r['resource_id'] == pid
               and r['user_id'] == user['user']['id'] for r in events)
    assert 'PRIVATE' not in json.dumps(events)
    report = client.post('/api/v1/admin/improvements/scan', headers=ADMIN)
    assert report.status_code == 200
    item = next(r for r in report.json()['items'] if r['kind'] == 'operation_error')
    assert item['count'] == 2 and len(item['operations']) == 2
    assert item['title'] == '同一操作多次返回服务错误'
    assert '数据可能已经保存' in item['limitation']
    assert not item.get('automatic_eligible')
    assert 'PRIVATE' not in report.text
    assert client.get(path, headers=auth).json()['content_hash'] == draft['content_hash']


def test_counter_failure_preserves_original_unhandled_exception(platform, monkeypatch):
    client, app = platform
    _, auth = signup(client, 'Alice'); pid = project(client, auth)
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path, headers=auth).json()
    original = RuntimeError('original failure')
    async def fail(*args, **kwargs): raise original
    def fail_counter(**kwargs): raise ValueError('counter failure')
    monkeypatch.setattr(app.state.services.applications, 'apply_operation', fail)
    monkeypatch.setattr(app.state.services.product_usage, 'record', fail_counter)
    with pytest.raises(RuntimeError, match='original failure') as error:
        client.post(path, headers=auth, json={'expected_revision':draft['revision'], 'idempotency_key':'failure',
                                            'op':'set_metadata','data':{'description':'unsaved'}})
    assert error.value is original


def test_unhandled_response_failure_does_not_replay_a_committed_edit(platform, monkeypatch):
    client, app = platform
    _, auth = signup(client, 'Alice'); pid = project(client, auth)
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path, headers=auth).json()
    apply = app.state.services.applications.apply_operation
    calls = []
    async def commit_then_fail(*args, **kwargs):
        calls.append(1)
        await apply(*args, **kwargs)
        raise RuntimeError('Response formatting failed after save')
    monkeypatch.setattr(app.state.services.applications, 'apply_operation', commit_then_fail)
    failed_client = TestClient(app, raise_server_exceptions=False)
    try:
        response = failed_client.post(path, headers=auth, json={
            'expected_revision':draft['revision'],'idempotency_key':'already-committed',
            'op':'set_metadata','data':{'description':'already saved'}})
        assert response.status_code == 500
    finally:
        failed_client.close()
    assert calls == [1]
    current = client.get(path, headers=auth).json()
    assert current['revision'] == draft['revision'] + 1
    assert current['snapshot']['description'] == 'already saved'
    with connect(app.state.services.product_usage.db) as db:
        assert db.execute("SELECT COUNT(*) FROM product_usage WHERE feature='edit_error' AND outcome='HTTP 500'").fetchone()[0] == 1


def test_cancelled_request_is_not_a_server_failure(platform, monkeypatch):
    client, app = platform
    _, auth = signup(client, 'Alice'); pid = project(client, auth)
    path = f'/api/v1/applications/{pid}/draft'
    draft = client.get(path, headers=auth).json()

    async def cancel_in_flight():
        began = asyncio.Event()
        async def blocked(*args, **kwargs):
            began.set()
            await asyncio.Future()
        monkeypatch.setattr(app.state.services.applications, 'apply_operation', blocked)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test', headers=auth) as http:
            pending = asyncio.create_task(http.post(path, json={'expected_revision':draft['revision'], 'idempotency_key':'cancelled',
                                                              'op':'set_metadata', 'data':{'description':'cancelled'}}))
            await asyncio.wait_for(began.wait(), timeout=2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    asyncio.run(cancel_in_flight())
    with connect(app.state.services.product_usage.db) as db:
        assert db.execute("SELECT COUNT(*) FROM product_usage WHERE feature='edit_error'").fetchone()[0] == 0
