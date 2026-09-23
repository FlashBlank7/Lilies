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
