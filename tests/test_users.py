"""Public accounts, device sessions and project authorization over real HTTP routes."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


@pytest.fixture
def platform(tmp_path):
    app = create_app(Settings(api_token='admin-boot', data_dir=tmp_path/'data',
                              workspace_root=tmp_path/'workspaces', model_egress_enabled=False,
                              scheduler_poll_seconds=3600))
    with TestClient(app) as client:
        yield client, app


def signup(client, name):
    response = client.post('/api/v1/auth/register', json={'name': name, 'password': 'password123'})
    assert response.status_code == 201, response.text
    assert response.json()['user']['role'] == 'member'
    return response.json(), {'Authorization': 'Bearer ' + response.json()['token']}


def project(client, headers, name='项目'):
    response = client.post('/api/v1/projects', headers=headers, json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_register_sessions_logout_password(platform):
    client, app = platform
    account, first = signup(client, '甲')
    assert client.get('/api/v1/me').status_code == 401
    assert client.get('/api/v1/projects', params={'token': account['token']}).status_code == 401
    assert client.post('/api/v1/auth/register', json={'name':'甲', 'password':'password123'}).status_code == 409
    second = client.post('/api/v1/auth/login', json={'name':'甲', 'password':'password123'})
    assert second.status_code == 200
    second_headers = {'Authorization': 'Bearer ' + second.json()['token']}
    assert client.get('/api/v1/me', headers=first).status_code == 200
    assert client.get('/api/v1/me', headers=second_headers).status_code == 200
    assert client.post('/api/v1/auth/logout', headers=first).status_code == 200
    assert client.get('/api/v1/me', headers=first).status_code == 401
    assert client.get('/api/v1/me', headers=second_headers).status_code == 200
    wrong = client.post('/api/v1/auth/password', headers=second_headers,
                        json={'current_password':'incorrect','new_password':'changed123'})
    assert wrong.status_code == 400
    changed = client.post('/api/v1/auth/password', headers=second_headers,
                          json={'current_password':'password123','new_password':'changed123'})
    assert changed.status_code == 200
    assert client.get('/api/v1/me', headers=second_headers).status_code == 401
    assert client.post('/api/v1/auth/login', json={'name':'甲','password':'password123'}).status_code == 401
    assert client.post('/api/v1/auth/login', json={'name':'甲','password':'changed123'}).status_code == 200


def test_project_isolation_members_and_legacy_file_routes(platform):
    client, app = platform
    alice, a = signup(client, '甲')
    bob, b = signup(client, '乙')
    pa, pb = project(client, a, '甲的项目'), project(client, b, '乙的项目')
    assert [p['id'] for p in client.get('/api/v1/projects', headers=a).json()] == [pa]
    for path in [f'/api/v1/projects/{pa}', f'/api/v1/projects/{pa}/records',
                 f'/api/v1/applications/{pa}/draft', f'/api/v1/applications/{pa}/workspace/files/private.txt',
                 f'/api/v1/use/{pa}/definition']:
        assert client.get(path, headers=b).status_code == 404, path
        assert client.get(path).status_code == 401, path
    assert client.get('/api/v1/users', headers=a).status_code == 403
    # A legacy run request permits a caller-selected workspace and global model
    # configuration; members execute through the project-bound task entrypoint.
    assert client.post(f'/api/v1/applications/{pa}/runs', headers=a,
                       json={'use_draft':True,'inputs':{},'workspace_path':str(app.state.services.settings.workspace_root/pb)}).status_code == 403
    assert client.post(f'/api/v1/applications/{pa}/draft/preview-patch', headers=a,
                       json={'instruction':'use a globally configured model'}).status_code == 403
    assert client.post(f'/api/v1/use/{pa}/runs', headers=a, json={'inputs':{}}).status_code == 403
    assert client.post('/api/v1/assistant/chat', headers=a, json={'messages':[{'role':'user','text':'test'}]}).status_code == 403
    assert client.put(f'/api/v1/projects/{pa}/capabilities', headers=a, json={'agent_modules_enabled':True}).status_code == 403
    base = f'/api/v1/projects/{pa}/access-members'
    assert client.post(base, headers=a, json={'name':'乙'}).status_code == 200
    assert client.get(f'/api/v1/projects/{pa}', headers=b).json()['access_role'] == 'collaborator'
    assert client.get(f'/api/v1/applications/{pa}/draft', headers=b).status_code == 200
    assert client.post(base, headers=b, json={'name':'乙','role':'owner'}).status_code == 403
    assert client.put(f'/api/v1/projects/{pa}/agent-session', headers=b, json={'provider':'api'}).status_code == 403
    # Nested workflow IDs must belong to the URL's project, even when both are accessible.
    assert client.put(f'/api/v1/projects/{pa}/workflows/{pb}/draft', headers=b,
                      json={'expected_revision':0,'workflow':{}}).status_code == 404
    assert client.delete(base+'/'+alice['user']['id'], headers=a).status_code == 409
    assert client.delete(base+'/'+bob['user']['id'], headers=a).status_code == 200
    assert client.get(f'/api/v1/applications/{pa}/draft', headers=b).status_code == 404


def test_copy_material_requires_source_membership(platform):
    client, app = platform
    _, a = signup(client, '甲')
    _, b = signup(client, '乙')
    pa, pb = project(client,a), project(client,b)
    uploaded = client.post(f'/api/v1/projects/{pa}/materials', headers=a,
                           files={'file':('design.txt',b'original bytes','text/plain')})
    assert uploaded.status_code == 201, uploaded.text
    request = {'source_project_id':pa,'source_path':uploaded.json()['path']}
    assert client.post(f'/api/v1/projects/{pb}/materials/copy', headers=b, json=request).status_code == 404
    client.post(f'/api/v1/projects/{pa}/access-members', headers=a,json={'name':'乙'})
    copied = client.post(f'/api/v1/projects/{pb}/materials/copy', headers=b, json=request)
    assert copied.status_code == 201, copied.text
    assert copied.json()['sha256'] == uploaded.json()['sha256']
    # Reading an authorized connection is different from copying its secret elsewhere.
    assert client.post(f'/api/v1/projects/{pb}/model-connection/copy', headers=b,
                       json={'source_project_id':pa}).status_code == 403


def test_admin_status_reset_and_legacy_projects(platform):
    client, app = platform
    root = {'Authorization':'Bearer admin-boot'}
    legacy = project(client,root)
    alice,a = signup(client,'甲')
    assert client.get('/api/v1/projects',headers=a).json() == []
    assigned = client.post(f'/api/v1/projects/{legacy}/access-members',headers=root,
                           json={'name':'甲','role':'owner'})
    assert assigned.status_code == 200
    assert client.get(f'/api/v1/projects/{legacy}',headers=a).status_code == 200
    uid=alice['user']['id']
    assert client.post(f'/api/v1/users/{uid}/status',headers=root,json={'status':'disabled'}).status_code == 200
    assert client.get('/api/v1/me',headers=a).status_code == 401
    assert client.post(f'/api/v1/users/{uid}/status',headers=root,json={'status':'active'}).status_code == 200
    assert client.get('/api/v1/me',headers=a).status_code == 401
    assert client.post(f'/api/v1/users/{uid}/password',headers=root,json={'password':'replacement123'}).status_code == 200
    assert client.post('/api/v1/auth/login',json={'name':'甲','password':'replacement123'}).status_code == 200
    administrator=asyncio.run(app.state.services.accounts.create('部署管理员','adminpassword',role='admin'))
    assert client.post(f'/api/v1/users/{administrator["id"]}/status',headers=root,json={'status':'disabled'}).status_code == 409


def test_session_expiry_and_login_rate_limit(platform):
    client, app = platform
    user, headers = signup(client,'甲')
    with app.state.services.storage._connect() as db:
        db.execute('UPDATE auth_sessions SET expires_at=0')
    assert client.get('/api/v1/me',headers=headers).status_code == 401
    for _ in range(10):
        assert client.post('/api/v1/auth/login',json={'name':'甲','password':'wrong'}).status_code == 401
    limited=client.post('/api/v1/auth/login',json={'name':'甲','password':'password123'})
    assert limited.status_code == 429
    assert int(limited.headers['retry-after']) > 0


def test_validation_never_echoes_password_or_promotes_signup(platform):
    client, _ = platform
    for body in ({'password':'do-not-echo-this'}, {'name':'甲','password':'do-not-echo-this','role':'admin'}):
        response=client.post('/api/v1/auth/register',json=body)
        assert response.status_code == 422
        assert 'do-not-echo-this' not in response.text


def test_member_removed_from_an_open_event_stream(platform):
    from types import SimpleNamespace
    from agent_platform.project_access import authorized_stream
    client, app = platform
    a, owner=signup(client,'stream-owner')
    b, member=signup(client,'stream-member')
    pid=project(client,owner)
    client.post(f'/api/v1/projects/{pid}/access-members',headers=owner,json={'name':'stream-member'})
    accounts=app.state.services.accounts
    closed=[]
    async def source():
        try:
            yield 'first event'
            await accounts.remove_member(pid,b['user']['id'])
            yield 'private event after removal'
        finally:
            closed.append(True)
    async def consume():
        request=SimpleNamespace(state=SimpleNamespace(auth_token=b['token'],project_access_ids={pid}))
        return [chunk async for chunk in authorized_stream(source(),request,accounts)]
    chunks=asyncio.run(consume())
    assert chunks[0]=='first event'
    assert 'access_revoked' in chunks[1]
    assert 'private event after removal' not in ''.join(chunks)
    assert closed==[True]
