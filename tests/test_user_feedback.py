"""Feedback is a private conversation with maintainers, never a business run."""
import base64
import json

from tests.test_users import platform, signup, project  # noqa:F401
from agent_platform.db import connect
from agent_platform import user_feedback

ADMIN = {'Authorization': 'Bearer admin-boot'}
BASE = '/api/v1/feedback'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII=')


def submit(client, headers, body, path=BASE, image=None):
    return client.post(path, headers=headers, data={'payload': json.dumps(body)},
                       files={'image': ('screenshot.png', image, 'image/png')} if image is not None else None)


def test_private_feedback_replies_unread_and_reopen(platform):
    client, app = platform
    _, alice = signup(client, 'Alice'); _, bob = signup(client, 'Bob')
    body = {'request_key': 'one', 'text': '结果不容易理解', 'category': 'usability'}
    assert submit(client, {}, body).status_code == 401
    made = submit(client, alice, body, image=PNG); assert made.status_code == 201, made.text
    ident = made.json()['id']; path = BASE + '/' + ident
    assert submit(client, alice, body, image=PNG).json()['id'] == ident
    assert submit(client, alice, {**body, 'text': '不同内容'}, image=PNG).status_code == 409
    assert client.get(BASE, headers=alice).json()['total'] == 1
    assert client.get(BASE, headers=bob).json()['total'] == 0
    assert client.get(BASE+'?scope=all', headers=bob).status_code == 403
    assert client.get(path, headers=bob).status_code == 404
    assert submit(client, bob, {'request_key':'reply', 'text':'偷看'}, path+'/messages').status_code == 404
    assert client.get(BASE+'/unread', headers=ADMIN).json()['count'] == 1
    detail = client.get(path, headers=alice).json()
    image_url = path+'/messages/'+detail['messages'][0]['id']+'/image'
    assert client.get(image_url, headers=alice).content == PNG
    assert client.get(image_url, headers=ADMIN).content == PNG
    assert client.get(image_url, headers=bob).status_code == 404
    assert 'payload_hash' not in detail and 'image' not in detail['messages'][0]
    assert client.get(BASE+'/unread', headers=alice).json()['count'] == 0
    response = {'request_key':'answer', 'text':'已改进说明，请再试一次', 'status':'resolved', 'expected_revision':1}
    assert submit(client, ADMIN, response, path+'/messages').status_code == 200
    assert submit(client, ADMIN, response, path+'/messages').status_code == 200
    assert client.get(path, headers=alice).json()['revision'] == 2
    assert client.get(BASE+'/unread', headers=alice).json()['count'] == 1
    # Seeing an old revision must not consume a newer reply notification.
    assert client.post(path+'/read', headers=alice, json={'revision':1}).status_code == 200
    assert client.get(BASE+'/unread', headers=alice).json()['count'] == 1
    client.post(path+'/read', headers=alice, json={'revision':2})
    assert client.get(BASE+'/unread', headers=alice).json()['count'] == 0
    assert submit(client, alice, {'request_key':'reopen', 'text':'还有一个地方不清楚', 'status':'received', 'expected_revision':2}, path+'/messages').status_code == 200
    assert client.get(path, headers=alice).json()['status'] == 'received'
    assert client.get(BASE+'/unread', headers=ADMIN).json()['count'] == 1
    assert submit(client, alice, {'request_key':'wrong', 'text':'改成处理中', 'status':'working', 'expected_revision':3}, path+'/messages').status_code == 403
    assert submit(client, ADMIN, {**response, 'request_key':'stale', 'status':'declined'}, path+'/messages').status_code == 409
    assert submit(client, ADMIN, {'request_key':'empty', 'text':' ', 'status':'declined', 'expected_revision':3}, path+'/messages').status_code == 422
    assert len(client.get(path, headers=alice).json()['messages']) == 3
    assert not app.state.services.local_agents.tasks


def test_sources_validate_permissions_and_survive_project_removal(platform):
    client, app = platform
    a_user, alice = signup(client, 'Alice'); b_user, bob = signup(client, 'Bob')
    pid = project(client, alice); other = project(client, bob)
    body = {'request_key':'source', 'text':'这次操作的按钮不清楚',
            'source':{'project_id':pid, 'workflow_id':pid, 'page':'workflow'}}
    assert submit(client, bob, body).status_code == 404
    assert submit(client, alice, {**body, 'source':{'project_id':pid, 'workflow_id':other}}).status_code == 404
    assert submit(client, alice, {**body, 'source':{'project_id':pid, 'task_id':'absent'}}).status_code == 404
    convo = client.post('/api/v1/projects/'+pid+'/conversations', headers=alice, json={'title':'我的会话'}).json()['id']
    source = {'project_id':pid, 'conversation_id':convo, 'request_id':'absent'}
    assert submit(client, alice, {**body, 'source':source}).status_code == 404
    from agent_platform.conversation_scope import conversation_scope
    with conversation_scope(pid, convo):
        app.state.services.local_agents.event(pid, 'assistant', 'PRIVATE ENTIRE ANSWER', request_id='answer')
    source['request_id'] = 'answer'
    made = submit(client, alice, {**body, 'source':source, 'excerpt':'主动选择的一小段'})
    assert made.status_code == 201, made.text
    path = BASE+'/'+made.json()['id']
    detail = client.get(path, headers=alice).json()
    assert detail['source_available'] and detail['excerpt'] == '主动选择的一小段'
    assert 'PRIVATE ENTIRE ANSWER' not in json.dumps(detail)
    # A project collaborator does not acquire access to the author's feedback or chat.
    assert client.post('/api/v1/projects/'+pid+'/access-members', headers=alice, json={'name':'Bob'}).status_code == 200
    assert client.get(path, headers=bob).status_code == 404
    assert submit(client, bob, {**body, 'source':source}).status_code == 404
    with connect(app.state.services.projects.store.db_path) as db:
        db.execute('DELETE FROM project_access_members WHERE project_id=? AND user_id=?', (pid,a_user['user']['id']))
    assert client.get(path, headers=alice).json()['source_available'] is False
    assert submit(client, alice, {'request_key':'later','text':'已离开项目，反馈仍需跟进'}, path+'/messages').status_code == 200
    assert 'PRIVATE ENTIRE ANSWER' not in client.get('/api/v1/admin/usage', headers=ADMIN).text


def test_validation_filters_and_atomic_failure(platform, monkeypatch):
    client, app = platform
    _, alice = signup(client, 'Alice')
    body = {'request_key':'one','text':'希望更好找文件','category':'idea'}
    for bad in ({**body,'source':{'task_id':'x'}},{**body,'text':' '},{**body,'source':{'url':'https://secret/?token=private'}},{**body,'category':'invalid'}):
        assert submit(client, alice, bad).status_code == 422
    assert submit(client, alice, body, image=b'<svg onload="alert(1)"/>').status_code == 422
    assert submit(client, alice, body, image=PNG+b'X'*(3*1024*1024)).status_code == 413
    original = user_feedback.add_message
    def fail(*args):
        original(*args)
        raise RuntimeError('storage failure')
    monkeypatch.setattr(user_feedback, 'add_message', fail)
    import pytest
    with pytest.raises(RuntimeError, match='storage failure'):
        submit(client, alice, body, image=PNG)
    assert client.get(BASE, headers=alice).json()['total'] == 0
    monkeypatch.setattr(user_feedback, 'add_message', original)
    ident = submit(client, alice, body, image=PNG).json()['id']
    submit(client, alice, {'request_key':'two','text':'运行卡住了','category':'runtime'})
    assert client.get(BASE+'?category=idea&q=文件', headers=alice).json()['items'][0]['id'] == ident
    assert client.get(BASE+'?status=resolved', headers=alice).json()['total'] == 0
    assert client.get(BASE+'?page=run', headers=alice).json()['total'] == 0
    assert client.get(BASE+'?page=general', headers=alice).json()['total'] == 2
    assert len(client.get(BASE+'?limit=1&offset=1', headers=alice).json()['items']) == 1
    # Initialization is additive and does not remove existing feedback.
    user_feedback.initialize(app.state.services.projects.store.db_path)
    assert client.get(BASE, headers=alice).json()['total'] == 2


def test_reply_does_not_consume_an_unseen_incoming_message(platform):
    client, _ = platform
    _, alice = signup(client, 'Alice')
    ident = submit(client, alice, {'request_key':'first','text':'如何查看结果？'}).json()['id']
    path = BASE+'/'+ident
    submit(client, ADMIN, {'request_key':'answer','text':'请打开运行记录。'}, path+'/messages').raise_for_status()
    # The author supplements the old screen without having loaded the new reply.
    submit(client, alice, {'request_key':'more','text':'补充：我是在手机上操作。'}, path+'/messages').raise_for_status()
    assert client.get(BASE+'/unread', headers=alice).json()['count'] == 1
    assert len(client.get(path, headers=alice).json()['messages']) == 3
    client.post(path+'/read', headers=alice, json={'revision':3}).raise_for_status()
    assert client.get(BASE+'/unread', headers=alice).json()['count'] == 0
