import time
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
