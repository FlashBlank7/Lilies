"""Optional tutorial progress is private, persistent and independent of business work."""
import asyncio

import pytest

from agent_platform.db import connect
from test_users import platform, signup, project  # noqa: F401

URL = '/api/v1/me/onboarding'


def test_new_accounts_and_admin_created_users_start_with_welcome(platform):
    client, _ = platform
    _, headers = signup(client, '员工')
    assert client.get(URL).status_code == 401
    state = client.get(URL, headers=headers).json()
    assert state == dict(status='new', step='project', completed_steps=[], project_id=None)
    created = client.post('/api/v1/users', headers={'Authorization': 'Bearer admin-boot'},
                          json={'name': '新同事', 'password': 'password123'})
    assert created.status_code == 200
    login = client.post('/api/v1/auth/login', json={'name': '新同事', 'password': 'password123'}).json()
    assert client.get(URL, headers={'Authorization': 'Bearer ' + login['token']}).json()['status'] == 'new'


def test_progress_persists_across_sessions_and_never_creates_tasks(platform):
    client, _ = platform
    _, first = signup(client, '甲')
    _, other = signup(client, '乙')
    pid = project(client, first)
    r = client.patch(URL, headers=first, json={'status': 'active', 'step': 'materials', 'project_id': pid, 'completed_steps': ['project']})
    assert r.status_code == 200, r.text
    second = client.post('/api/v1/auth/login', json={'name': '甲', 'password': 'password123'}).json()
    second = {'Authorization': 'Bearer ' + second['token']}
    assert client.get(URL, headers=second).json() == r.json()
    assert client.get(URL, headers=other).json()['completed_steps'] == []
    # Additive action updates from two devices cannot erase earlier completions.
    client.patch(URL, headers=second, json={'completed_steps': ['materials']})
    state = client.patch(URL, headers=first, json={'step': 'results'}).json()
    assert state['completed_steps'] == ['project', 'materials']
    assert client.get(f'/api/v1/projects/{pid}/tasks', headers=first).json() == []
    for status in ['skipped', 'active', 'finished']:
        assert client.patch(URL, headers=first, json={'status': status}).json()['status'] == status
    reset = client.patch(URL, headers=second, json={'restart': True}).json()
    assert reset == dict(status='active', step='project', completed_steps=[], project_id=None)


def test_project_permissions_and_revocation(platform):
    client, app = platform
    account, owner = signup(client, '甲')
    collaborator, second = signup(client, '乙')
    pid = project(client, owner)
    assert client.patch(URL, headers=second, json={'project_id': pid}).status_code == 404
    assert client.patch(URL, headers=owner, json={'project_id': 'missing'}).status_code == 404
    added = client.post(f'/api/v1/projects/{pid}/access-members', headers=owner, json={'name': '乙', 'role': 'collaborator'})
    assert added.status_code == 200
    assert client.patch(URL, headers=second, json={'project_id': pid, 'step': 'results', 'status': 'active'}).status_code == 200
    client.delete(f'/api/v1/projects/{pid}/access-members/{collaborator["user"]["id"]}', headers=owner)
    state = client.get(URL, headers=second).json()
    assert state['project_id'] is None and state['step'] == 'project'
    # Even the legacy administrator cannot associate a nonexistent project.
    assert client.patch(URL, headers={'Authorization': 'Bearer admin-boot'}, json={'project_id': 'missing'}).status_code == 404


def test_legacy_migration_keeps_accounts_and_does_not_autostart(platform):
    client, app = platform
    account, headers = signup(client, '原账号')
    storage = app.state.services.storage
    with connect(storage.db_path) as db:
        before = list(db.execute('SELECT id,name,role,password_hash FROM users'))
        db.execute('DROP TABLE user_onboarding')
    asyncio.run(storage.initialize())
    with connect(storage.db_path) as db:
        after = list(db.execute('SELECT id,name,role,password_hash FROM users'))
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert client.get(URL, headers=headers).json()['status'] == 'skipped'
    assert client.get(URL, headers={'Authorization': 'Bearer admin-boot'}).json()['status'] == 'skipped'
    assert client.patch(URL, headers=headers, json={'restart': True}).json()['status'] == 'active'


@pytest.mark.parametrize('patch', [
    {'user_id': 'someone-else'}, {'status': 'invalid'}, {'step': 'train'},
    {'completed_steps': ['fake']}, {'project_id': '../private'},
])
def test_invalid_progress_cannot_change_another_account(platform, patch):
    client, _ = platform
    _, headers = signup(client, '甲')
    assert client.patch(URL, headers=headers, json=patch).status_code in {404, 422}
    assert client.get(URL, headers=headers).json()['status'] == 'new'
