"""One finding starts one repair, without sharing administrators' private chats."""
import asyncio
import subprocess
import sys
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import HTTPException

from agent_platform.db import connect
from agent_platform.official_agent import actor_id
from agent_platform.usage_learning import UsageLearning
from tests.test_official_agent import official, enable, FakeAgent, wait  # noqa: F401
from tests.test_usage_learning import ADMIN, BASE, failed_runs
from tests.test_users import platform, project, signup  # noqa: F401


def administrator(client, app, name):
    account, headers = signup(client, name)
    user = {**account['user'], 'role': 'admin'}
    with connect(app.state.services.usage_learning.db) as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (user['id'],))
    return user, headers


def finding(client):
    client.headers.update(ADMIN)
    pid = project(client, ADMIN, '共同处理同一故障')
    failed_runs(client, pid)
    item = client.post(BASE + '/scan').json()['items'][0]
    return pid, item['id']


@pytest.mark.parametrize('automatic_first', [True, False])
def test_two_administrators_manual_and_automatic_share_only_handling_metadata(official, automatic_first):
    client, app, _ = official
    first, first_headers = administrator(client, app, 'first-admin')
    second, second_headers = administrator(client, app, 'second-admin')
    pid, ident = finding(client)
    enable(client, pid)
    path = BASE + '/' + ident + '/start'
    automatic_headers = first_headers if automatic_first else second_headers
    assert client.put(BASE + '/settings', headers=automatic_headers,
                      json={'enabled': True, 'project_ids': [pid], 'daily_limit': 2}).status_code == 200
    learning = app.state.services.usage_learning
    if automatic_first:
        client.portal.call(learning.automate)
        cid = client.get(BASE, headers=first_headers).json()['items'][0]['handoff']['conversation_id']
    else:
        response = client.post(path, headers=first_headers)
        assert response.status_code == 200, response.text
        cid = response.json()['conversation_id']
    assert wait(client, f'/api/v1/projects/{pid}/conversations/{cid}', first_headers)['status'] == 'idle'
    assert len(FakeAgent.turns) == 1

    # A separate service object has no shared asyncio lock; the DB preserves ownership.
    restarted = UsageLearning(app.state.services)
    restarted.initialize()
    client.portal.call(restarted.automate)
    assert restarted.automatic_error == ''
    response = client.post(path, headers=second_headers)
    assert response.status_code == 409 and '其他管理员已有' in response.json()['detail']
    other_report = client.get(BASE, headers=second_headers)
    item = other_report.json()['items'][0]
    assert item['handled_elsewhere'] is True and item['handoff'] is None
    assert cid not in other_report.text and first['id'] not in other_report.text
    assert client.get(BASE + '/' + ident + '/result', headers=second_headers).status_code == 404
    assert client.get(f'/api/v1/projects/{pid}/conversations/{cid}', headers=second_headers).status_code == 404
    assert client.get(f'/api/v1/projects/{pid}/conversations', headers=second_headers).json() == []
    own = client.get(BASE, headers=first_headers).json()['items'][0]
    assert own['handled_elsewhere'] is False and own['handoff']['conversation_id'] == cid
    assert client.post(path, headers=first_headers).json()['conversation_id'] == cid
    assert len(FakeAgent.turns) == 1
    with connect(learning.db) as db:
        assert db.execute('SELECT COUNT(*) FROM usage_handoffs WHERE finding_id=?', (ident,)).fetchone()[0] == 1


@pytest.mark.parametrize('automatic_first', [True, False])
@pytest.mark.parametrize('same_administrator', [True, False])
def test_independent_instances_compete_atomically_before_creating_conversations(official, automatic_first, same_administrator):
    client, app, _ = official
    first, first_headers = administrator(client, app, 'first-admin')
    second, second_headers = administrator(client, app, 'second-admin')
    if same_administrator:
        second, second_headers = first, first_headers
    pid, ident = finding(client)
    enable(client, pid)
    assert client.put(BASE + '/settings', headers=first_headers,
                      json={'enabled': True, 'project_ids': [pid], 'daily_limit': 2}).status_code == 200
    learners = [app.state.services.usage_learning, UsageLearning(app.state.services)]
    barrier = Barrier(2)
    FakeAgent.hold = True

    async def race():
        async def start(learning, user, automatic):
            # Synchronize before taking the nonblocking process-wide guard.
            await asyncio.to_thread(barrier.wait, timeout=10)
            token = actor_id.set(user['id'])
            try:
                return await learning.start(ident, user, automatic=automatic)
            finally:
                actor_id.reset(token)
        # The service objects have no shared Python lock.
        return await asyncio.gather(start(learners[0], first, automatic_first),
                                    start(learners[1], second, False), return_exceptions=True)

    results = client.portal.call(race)
    successes = [(index, value) for index, value in enumerate(results) if isinstance(value, dict)]
    errors = [value for value in results if isinstance(value, HTTPException)]
    assert len(successes) == len(errors) == 1, results
    assert errors[0].status_code == 409 and '正在启动' in errors[0].detail
    winner, started = successes[0]
    assert started['status'] == 'started'
    headers = [first_headers, second_headers]
    cid = started['conversation_id']
    conversation_path = f'/api/v1/projects/{pid}/conversations/{cid}'
    assert wait(client, conversation_path, headers[winner], ('running',))['status'] == 'running'
    async def wait_for_model_turn():
        async with asyncio.timeout(5):
            while not FakeAgent.turns:
                await asyncio.sleep(.01)
    client.portal.call(wait_for_model_turn)
    assert len(FakeAgent.turns) == 1
    if not same_administrator:
        assert client.get(f'/api/v1/projects/{pid}/conversations', headers=headers[1-winner]).json() == []
        assert client.get(BASE, headers=headers[1-winner]).json()['items'][0]['handled_elsewhere'] is True
    # The startup guard is released even while the original model turn is running.
    assert client.post(BASE + '/' + ident + '/start', headers=headers[winner]).json()['conversation_id'] == cid
    assert len(FakeAgent.turns) == 1
    FakeAgent.hold = False
    assert wait(client, conversation_path, headers[winner])['status'] == 'idle'
    with connect(learners[0].db) as db:
        assert db.execute('SELECT COUNT(*) FROM project_conversations WHERE project_id=?', (pid,)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM usage_handoffs WHERE finding_id=?', (ident,)).fetchone()[0] == 1


def test_other_process_start_guard_blocks_without_orphans_and_releases_on_exit(official):
    client, app, _ = official
    _, headers = administrator(client, app, 'first-admin')
    pid, ident = finding(client)
    enable(client, pid)
    lock = Path(app.state.services.usage_learning.db).with_suffix('.usage-start.lock')
    child = subprocess.Popen([sys.executable, '-c',
        'import fcntl,sys; f=open(sys.argv[1],"a+b"); fcntl.flock(f,fcntl.LOCK_EX); '
        'print("locked",flush=True); sys.stdin.read()', str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        response = client.post(BASE + '/' + ident + '/start', headers=headers)
        assert response.status_code == 409 and '正在启动' in response.json()['detail']
        assert client.get(f'/api/v1/projects/{pid}/conversations', headers=headers).json() == []
        assert not FakeAgent.turns
    finally:
        child.communicate(timeout=10)
    response = client.post(BASE + '/' + ident + '/start', headers=headers)
    assert response.status_code == 200, response.text
    cid = response.json()['conversation_id']
    assert wait(client, f'/api/v1/projects/{pid}/conversations/{cid}', headers)['status'] == 'idle'
    assert len(FakeAgent.turns) == 1


def test_failed_start_reserves_private_retry_and_rejects_other_admin_before_new_session(official):
    client, app, _ = official
    _, first_headers = administrator(client, app, 'first-admin')
    _, second_headers = administrator(client, app, 'second-admin')
    pid, ident = finding(client)
    path = BASE + '/' + ident + '/start'
    failed = client.post(path, headers=first_headers)
    assert failed.status_code == 409 and '会话已保留' in failed.json()['detail']
    own = client.get(BASE, headers=first_headers).json()['items'][0]
    cid = own['handoff']['conversation_id']
    assert own['handoff']['status'] == 'prepared'
    rejected = client.post(path, headers=second_headers)
    assert rejected.status_code == 409 and '其他管理员已有' in rejected.json()['detail']
    assert client.get(f'/api/v1/projects/{pid}/conversations', headers=second_headers).json() == []
    assert not FakeAgent.turns
    enable(client, pid)
    retried = client.post(path, headers=first_headers)
    assert retried.status_code == 200 and retried.json()['conversation_id'] == cid
    assert wait(client, f'/api/v1/projects/{pid}/conversations/{cid}', first_headers)['status'] == 'idle'
    assert len(FakeAgent.turns) == 1


def test_existing_multiowner_handoffs_keep_private_continuation_without_new_dispatch(official):
    client, app, _ = official
    owners = [administrator(client, app, name) for name in ('first-admin', 'second-admin')]
    pid, ident = finding(client)
    conversations = []
    for user, headers in owners:
        response = client.post(f'/api/v1/projects/{pid}/conversations', headers=headers, json={'title': '已有私有处理'})
        assert response.status_code == 201
        cid = response.json()['id']
        conversations.append(cid)
        with connect(app.state.services.usage_learning.db) as db:
            db.execute("INSERT INTO usage_handoffs(finding_id,user_id,conversation_id,status,error) VALUES(?,?,?,'started','')",
                       (ident, user['id'], cid))
    app.state.services.usage_learning.initialize()
    for index, (_, headers) in enumerate(owners):
        response = client.post(BASE + '/' + ident + '/start', headers=headers)
        assert response.status_code == 200 and response.json()['conversation_id'] == conversations[index]
        report = client.get(BASE, headers=headers)
        assert report.json()['items'][0]['handoff']['conversation_id'] == conversations[index]
        assert conversations[1-index] not in report.text
        assert client.get(BASE + '/' + ident + '/result', headers=headers).status_code == 200
    # A third administrator can see the signal, but cannot create another handling chat.
    response = client.post(BASE + '/' + ident + '/start', headers=ADMIN)
    assert response.status_code == 409 and '其他管理员已有' in response.json()['detail']
    assert not FakeAgent.turns
    with connect(app.state.services.usage_learning.db) as db:
        assert db.execute('SELECT COUNT(*) FROM usage_handoffs WHERE finding_id=?', (ident,)).fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM project_conversations WHERE project_id=?', (pid,)).fetchone()[0] == 2
